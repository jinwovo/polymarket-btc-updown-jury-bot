"""
Main bot loop - orchestrates Binance price feed, Polymarket real-time odds,
jury deliberation, and trade execution for BTC Up/Down 5-minute markets.

Core strategy: Speed arbitrage.
Binance price moves first, Polymarket odds lag, so we buy the cheap side before odds adjust.
"""
import asyncio
import time
import signal
import logging
import sys
from typing import Optional

from config import config
from binance_ws import BinancePriceFeed
from polymarket_client import PolymarketClient, MarketInfo, compute_market_timestamps
from judges import Jury, MarketContext, Vote
from risk_manager import RiskManager, TradeRecord
from trade_gate import evaluate_entry_gate

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize_position_mode(raw: str) -> str:
    mode = str(raw or "BOTH").strip().upper()
    if mode in ("UP_ONLY", "DOWN_ONLY", "BOTH"):
        return mode
    return "BOTH"


def _safe_prob(value: float | None) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        if 0.0 < v < 1.0:
            return v
    except Exception:
        return None
    return None


def _recent_move_pct(
    prices: list[float],
    timestamps: list[float],
    now_ts: float,
    lookback_sec: float,
) -> float | None:
    n = min(len(prices), len(timestamps))
    if n <= 1:
        return None
    lo_ts = now_ts - max(1.0, float(lookback_sec))
    p0 = None
    p1 = None
    for i in range(n):
        try:
            ts = float(timestamps[i])
            px = float(prices[i])
        except Exception:
            continue
        if ts < lo_ts:
            continue
        if px <= 0.0:
            continue
        if p0 is None:
            p0 = px
        p1 = px
    if p0 is None or p1 is None or p0 <= 0.0:
        return None
    return ((p1 - p0) / p0) * 100.0


class TradingBot:
    def __init__(self):
        self.price_feed = BinancePriceFeed()
        self.poly_client = PolymarketClient()
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager()
        self.position_mode = _normalize_position_mode(config.trading.position_mode)

        self.current_market: Optional[MarketInfo] = None
        self.current_trade: Optional[TradeRecord] = None
        self.market_start_price: Optional[float] = None
        self.recent_results: list[str] = []

        self._running = False
        self._check_interval = 0.5  # 500ms - fast enough to catch odds lag
        self._odds_task: Optional[asyncio.Task] = None
        self._last_odds_fetch: float = 0.0

    async def start(self):
        logger.info("=" * 60)
        logger.info("Polymarket BTC Up/Down 5m Speed Arbitrage Bot")
        logger.info(f"Mode: {'DRY RUN' if config.trading.dry_run else '*** LIVE TRADING ***'}")
        logger.info(f"Max bet: ${config.trading.max_bet_size} | Min edge: {config.trading.min_edge}")
        logger.info(
            "Entry gate: fee=%s%% | min_expected_roi=%s%%",
            round(config.trading.fee_rate * 100.0, 3),
            round(config.trading.min_expected_roi * 100.0, 3),
        )
        logger.info(
            "Jury: %s/%s | Check interval: %ss",
            config.trading.jury_threshold,
            self.jury.size,
            self._check_interval,
        )
        logger.info("Position mode: %s", self.position_mode)
        logger.info(
            "Entry execution: mode=%s | timeout=%.2fs | poll=%.2fs | drift_abs=%.4f | drift_ratio=%.2f%%",
            config.trading.entry_order_mode,
            float(config.trading.limit_order_timeout_seconds),
            float(config.trading.order_poll_interval_seconds),
            float(config.trading.max_entry_price_drift_abs),
            float(config.trading.max_entry_price_drift_ratio) * 100.0,
        )
        logger.info(
            "Live guards: entry_start=%.0fs support>=%.0f%% unanim=%s move>=%.4f%%(lookback=%.0fs) "
            "implied(side>=%.2f opp<=%.2f) down_block>=%.4f%%",
            float(config.trading.live_entry_start_seconds),
            float(config.trading.live_min_support_ratio) * 100.0,
            config.trading.live_require_unanimous,
            float(config.trading.live_min_recent_move_pct),
            float(config.trading.live_recent_move_lookback_sec),
            float(config.trading.live_min_entry_side_implied),
            float(config.trading.live_max_opposite_implied),
            float(config.trading.live_down_above_start_block_pct),
        )
        logger.info("=" * 60)

        self._running = True
        price_task = asyncio.create_task(self.price_feed.connect())

        logger.info("Waiting for Binance price data...")
        for _ in range(30):
            if self.price_feed.current_price is not None:
                break
            await asyncio.sleep(1)

        if self.price_feed.current_price is None:
            logger.error("Failed to get initial price data, exiting")
            self._running = False
            price_task.cancel()
            return

        logger.info(f"BTC price: ${self.price_feed.current_price:,.2f}")

        try:
            await self._trading_loop()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            self._running = False
            self.price_feed.stop()
            self.poly_client.stop_odds_polling()
            if self._odds_task:
                self._odds_task.cancel()
            await self.poly_client.close()
            price_task.cancel()

            stats = self.risk_mgr.get_stats()
            logger.info("=" * 60)
            logger.info("FINAL STATS:")
            logger.info(f"  Total trades: {stats['total_trades']}")
            logger.info(f"  Wins: {stats['wins']} | Losses: {stats['losses']}")
            logger.info(f"  Win rate: {stats['win_rate']:.1%}")
            logger.info(f"  Total PnL: ${stats['total_pnl']:+.2f}")
            logger.info("=" * 60)

    async def _trading_loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _tick(self):
        now = time.time()
        ts = compute_market_timestamps(now)

        current_start = ts["current"]["start"]
        seconds_elapsed = ts["seconds_elapsed"]
        seconds_remaining = ts["seconds_remaining"]

        # ---- New 5-min window? ----
        if self.current_market is None or self.current_market.start_timestamp != current_start:
            await self._on_new_market(current_start, seconds_elapsed)

        if self.current_market is None:
            return

        # ---- Resolve previous trade ----
        if self.current_trade and self.current_trade.result == "PENDING":
            if self.current_trade.timestamp < (self.current_market.start_timestamp - 10):
                await self._resolve_previous_trade()

        # ---- Already traded this window? ----
        if self.current_trade and self.current_trade.result == "PENDING":
            return

        # ---- Timing filters ----
        if seconds_remaining < config.trading.cutoff_before_close_seconds:
            return
        if seconds_elapsed < float(config.trading.live_entry_start_seconds):
            return

        # ---- Risk check ----
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            return

        # ---- Refresh Polymarket odds (high-frequency) ----
        # Only fetch if stale (>1s old) to avoid hammering API
        if self.current_market.last_odds_update < now - 1.0:
            if self.current_market.up_token_id and self.current_market.down_token_id:
                await self.poly_client.refresh_odds(self.current_market)
            else:
                # Try to find market again
                market = await self.poly_client.find_market(current_start)
                if market:
                    self.current_market = market

        # ---- Build context ----
        ctx = self._build_context(seconds_elapsed, seconds_remaining)
        if ctx is None:
            return

        # ---- Quick divergence check BEFORE full jury (save CPU) ----
        if self.market_start_price and self.market_start_price > 0:
            btc_change_pct = abs(
                (self.price_feed.current_price - self.market_start_price)
                / self.market_start_price * 100
            )
            # If BTC hasn't moved much, no opportunity
            if btc_change_pct < 0.02 and seconds_elapsed < 120:
                return

        # ---- Jury deliberation ----
        decision = self.jury.deliberate(ctx)

        if decision.direction == "NO_TRADE":
            return

        if decision.avg_confidence < config.trading.min_edge:
            return

        if self.position_mode == "UP_ONLY" and decision.direction != "UP":
            return
        if self.position_mode == "DOWN_ONLY" and decision.direction != "DOWN":
            return

        support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
        support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
        if support_ratio < float(config.trading.live_min_support_ratio):
            return
        if config.trading.live_require_unanimous and not decision.unanimous:
            return

        # ---- Size and execute (FAST) ----
        bet_size = self.risk_mgr.compute_bet_size(
            decision.avg_confidence, decision.max_edge
        )
        if bet_size < config.trading.min_bet_size:
            return

        if decision.direction == "UP":
            token_id = self.current_market.up_token_id
            # Buy at the ask price (taker)
            price = (
                _safe_prob(self.current_market.up_best_ask)
                or _safe_prob(self.current_market.up_price)
            )
        else:
            token_id = self.current_market.down_token_id
            price = (
                _safe_prob(self.current_market.down_best_ask)
                or _safe_prob(self.current_market.down_price)
            )

        if price is None or price <= 0.01 or price >= 0.99 or not token_id:
            return

        up_ask = (
            _safe_prob(self.current_market.up_best_ask)
            or _safe_prob(self.current_market.up_price)
        )
        down_ask = (
            _safe_prob(self.current_market.down_best_ask)
            or _safe_prob(self.current_market.down_price)
        )
        side_ask = up_ask if decision.direction == "UP" else down_ask
        opposite_ask = down_ask if decision.direction == "UP" else up_ask

        if side_ask is not None and side_ask < float(config.trading.live_min_entry_side_implied):
            logger.info(
                "Skip live implied-side guard: dir=%s side_ask=%.3f < %.3f",
                decision.direction,
                side_ask,
                float(config.trading.live_min_entry_side_implied),
            )
            return
        if opposite_ask is not None and opposite_ask > float(config.trading.live_max_opposite_implied):
            logger.info(
                "Skip live opposite-implied guard: dir=%s opp_ask=%.3f > %.3f",
                decision.direction,
                opposite_ask,
                float(config.trading.live_max_opposite_implied),
            )
            return

        btc_move_from_start_pct = (
            ((float(ctx.current_binance_price) - float(ctx.market_start_price)) / float(ctx.market_start_price)) * 100.0
            if ctx.market_start_price > 0
            else 0.0
        )
        recent_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=now,
            lookback_sec=float(config.trading.live_recent_move_lookback_sec),
        )
        if recent_move is None:
            return
        base_move_thr = float(config.trading.live_min_recent_move_pct)
        if decision.direction == "UP" and recent_move < base_move_thr:
            logger.info(
                "Skip live momentum guard: dir=UP move=%.4f%% < +%.4f%%",
                recent_move,
                base_move_thr,
            )
            return
        down_move_thr = base_move_thr
        if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
            down_move_thr += float(config.trading.live_down_above_start_momentum_extra)
        if decision.direction == "DOWN" and recent_move > -down_move_thr:
            logger.info(
                "Skip live momentum guard: dir=DOWN move=%.4f%% > -%.4f%% (btc_vs_start=%+.4f%%)",
                recent_move,
                down_move_thr,
                btc_move_from_start_pct,
            )
            return

        dynamic_min_roi = float(config.trading.min_expected_roi)
        if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
            block_thr = float(config.trading.live_down_above_start_block_pct)
            if btc_move_from_start_pct >= block_thr:
                logger.info(
                    "Skip live DOWN-above-start hard block: btc_vs_start=%+.4f%% >= %.4f%%",
                    btc_move_from_start_pct,
                    block_thr,
                )
                return
            ratio = btc_move_from_start_pct / max(block_thr, 1e-9)
            dynamic_min_roi += float(config.trading.live_down_above_start_ev_penalty) * _clamp(ratio, 0.0, 1.0)

        gate = evaluate_entry_gate(
            direction=decision.direction,
            entry_price=float(price),
            current_price=float(ctx.current_binance_price),
            start_price=float(ctx.market_start_price),
            seconds_elapsed=float(seconds_elapsed),
            jury_confidence=float(decision.avg_confidence),
            support_ratio=float(support_ratio),
            seconds_remaining=float(seconds_remaining),
            recent_prices=list(ctx.recent_prices),
            recent_timestamps=list(ctx.recent_timestamps),
            poly_up_ask=ctx.poly_up_ask,
            poly_down_ask=ctx.poly_down_ask,
            recent_results=list(ctx.recent_results or []),
        )
        if not gate.allow:
            logger.info("Skip trade by entry gate: %s", gate.reason)
            return
        if gate.expected_roi < dynamic_min_roi:
            logger.info(
                "Skip live dynamic EV guard: net_ev=%+.3f%% < %.3f%%",
                gate.expected_roi * 100.0,
                dynamic_min_roi * 100.0,
            )
            return

        logger.info(
            f">>> TRADE: {decision.direction} | ${bet_size:.2f} @ {price:.4f} | "
            f"conf={decision.avg_confidence:.3f} | unan={decision.unanimous} | "
            f"net_ev={gate.expected_roi:+.3%} | "
            f"BTC_chg={ctx.current_binance_price - ctx.market_start_price:+.2f} | "
            f"poly_up={ctx.poly_up_price:.3f} poly_down={ctx.poly_down_price:.3f}"
        )

        result = await self.poly_client.place_entry_order(
            token_id=token_id,
            side=decision.direction,
            amount=bet_size,
            reference_ask=float(price),
        )

        if result is not None:
            if not bool(result.get("accepted", True)):
                logger.info(
                    "Order blocked/rejected: mode=%s status=%s reason=%s",
                    result.get("mode"),
                    result.get("status"),
                    result.get("reason"),
                )
                return

            if not bool(result.get("filled", False)):
                logger.info(
                    "Order not filled: mode=%s status=%s reason=%s",
                    result.get("mode"),
                    result.get("status"),
                    result.get("reason"),
                )
                return

            executed_notional = float(result.get("executed_notional") or 0.0)
            executed_price = float(result.get("executed_price") or 0.0)
            if executed_notional <= 0.0:
                executed_notional = float(bet_size)
            if not (0.0 < executed_price < 1.0):
                executed_price = float(price)

            self.current_trade = self.risk_mgr.record_trade(
                direction=decision.direction,
                amount=executed_notional,
                price=executed_price,
            )

    async def _on_new_market(self, start_timestamp: int, seconds_elapsed: float):
        if self.current_trade and self.current_trade.result == "PENDING":
            await self._resolve_previous_trade()

        # Stop polling old market
        self.poly_client.stop_odds_polling()
        if self._odds_task:
            self._odds_task.cancel()
            self._odds_task = None

        self.market_start_price = self.price_feed.get_price_at(float(start_timestamp))
        if self.market_start_price is None:
            self.market_start_price = self.price_feed.current_price

        self.current_market = await self.poly_client.find_market(start_timestamp)

        if self.current_market:
            logger.info(
                f"New market: {self.current_market.slug} | "
                f"UP={self.current_market.up_price:.3f} DOWN={self.current_market.down_price:.3f} | "
                f"BTC start=${self.market_start_price:,.2f}"
            )
            # Start background odds polling for this market
            if self.current_market.up_token_id and self.current_market.down_token_id:
                self._odds_task = asyncio.create_task(
                    self.poly_client.start_odds_polling(self.current_market, interval=1.0)
                )
        else:
            logger.warning(f"Market not found for ts={start_timestamp}, creating stub")
            from polymarket_client import MarketInfo, market_slug_for_timestamp
            self.current_market = MarketInfo(
                condition_id="", question="",
                slug=market_slug_for_timestamp(start_timestamp),
                start_timestamp=start_timestamp,
                end_timestamp=start_timestamp + config.polymarket.interval_seconds,
                up_token_id="", down_token_id="",
                up_price=0.5, down_price=0.5, active=True,
            )

        self.current_trade = None

    async def _resolve_previous_trade(self):
        if not self.current_trade or self.current_trade.result != "PENDING":
            return

        if self.market_start_price and self.price_feed.current_price:
            went_up = self.price_feed.current_price >= self.market_start_price
            actual_direction = "UP" if went_up else "DOWN"
            won = self.current_trade.direction == actual_direction

            self.risk_mgr.resolve_trade(self.current_trade, won)
            self.recent_results.append(actual_direction)
            if len(self.recent_results) > 50:
                self.recent_results = self.recent_results[-50:]

            logger.info(
                f"Market resolved: {actual_direction} | "
                f"Trade={self.current_trade.direction} -> {'WIN' if won else 'LOSS'}"
            )

    def _build_context(self, seconds_elapsed: float, seconds_remaining: float) -> Optional[MarketContext]:
        if self.price_feed.current_price is None or self.market_start_price is None:
            return None
        if self.current_market is None:
            return None

        recent = self.price_feed.get_recent_prices(600)

        return MarketContext(
            current_binance_price=self.price_feed.current_price,
            market_start_price=self.market_start_price,
            recent_prices=[t.price for t in recent],
            recent_timestamps=[t.timestamp for t in recent],
            poly_up_price=self.current_market.up_price,
            poly_down_price=self.current_market.down_price,
            seconds_elapsed=seconds_elapsed,
            seconds_remaining=seconds_remaining,
            poly_up_bid=(
                self.current_market.up_best_bid
                if 0.0 < self.current_market.up_best_bid < 1.0
                else None
            ),
            poly_up_ask=(
                self.current_market.up_best_ask
                if 0.0 < self.current_market.up_best_ask < 1.0
                else None
            ),
            poly_down_bid=(
                self.current_market.down_best_bid
                if 0.0 < self.current_market.down_best_bid < 1.0
                else None
            ),
            poly_down_ask=(
                self.current_market.down_best_ask
                if 0.0 < self.current_market.down_best_ask < 1.0
                else None
            ),
            recent_results=self.recent_results[-20:],
        )


async def main():
    bot = TradingBot()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: setattr(bot, '_running', False))
        except NotImplementedError:
            pass

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
