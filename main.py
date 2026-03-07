"""
Main bot loop - orchestrates Binance price feed, Polymarket real-time odds,
jury deliberation, and trade execution for BTC Up/Down 5-minute markets.

Core strategy: Speed arbitrage.
Binance price moves → Polymarket odds lag → we buy the cheap side before odds adjust.
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


class TradingBot:
    def __init__(self):
        self.price_feed = BinancePriceFeed()
        self.poly_client = PolymarketClient()
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager()

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
        if seconds_elapsed < 10:  # Reduced from 30s - we want speed
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

        # ---- Size and execute (FAST) ----
        bet_size = self.risk_mgr.compute_bet_size(
            decision.avg_confidence, decision.max_edge
        )
        if bet_size < config.trading.min_bet_size:
            return

        if decision.direction == "UP":
            token_id = self.current_market.up_token_id
            # Buy at the ask price (taker)
            price = self.current_market.up_best_ask
            if price <= 0 or price >= 1:
                price = self.current_market.up_price
        else:
            token_id = self.current_market.down_token_id
            price = self.current_market.down_best_ask
            if price <= 0 or price >= 1:
                price = self.current_market.down_price

        if price <= 0.01 or price >= 0.99 or not token_id:
            return

        support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
        support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
        gate = evaluate_entry_gate(
            direction=decision.direction,
            entry_price=float(price),
            current_price=float(ctx.current_binance_price),
            start_price=float(ctx.market_start_price),
            seconds_elapsed=float(seconds_elapsed),
            jury_confidence=float(decision.avg_confidence),
            support_ratio=float(support_ratio),
        )
        if not gate.allow:
            logger.info("Skip trade by entry gate: %s", gate.reason)
            return

        logger.info(
            f">>> TRADE: {decision.direction} | ${bet_size:.2f} @ {price:.4f} | "
            f"conf={decision.avg_confidence:.3f} | unan={decision.unanimous} | "
            f"net_ev={gate.expected_roi:+.3%} | "
            f"BTC_chg={ctx.current_binance_price - ctx.market_start_price:+.2f} | "
            f"poly_up={ctx.poly_up_price:.3f} poly_down={ctx.poly_down_price:.3f}"
        )

        result = await self.poly_client.place_market_order(
            token_id=token_id, side=decision.direction, amount=bet_size,
        )

        if result is not None:
            self.current_trade = self.risk_mgr.record_trade(
                direction=decision.direction, amount=bet_size, price=price,
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
                f"Trade={self.current_trade.direction} -> {'WIN ✓' if won else 'LOSS ✗'}"
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
