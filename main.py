"""
Main bot loop - orchestrates Binance price feed, Polymarket real-time odds,
jury deliberation, and trade execution for BTC Up/Down 5-minute markets.

Core strategy: Speed arbitrage.
Binance price moves first, Polymarket odds lag, so we buy the cheap side before odds adjust.
"""
import asyncio
import json
import time
import signal
import logging
import os
import sys
import math
from typing import Any, Optional

from config import config
from db_config import connect_db, execute_write, fetch_one_dict, init_market_schema, is_sqlite_backend
from binance_ws import BinancePriceFeed
from polymarket_client import PolymarketClient, MarketInfo, compute_market_timestamps
from judges import Jury, MarketContext, Vote
from risk_manager import RiskManager, TradeRecord
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate

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
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize_position_mode(raw: str) -> str:
    mode = str(raw or "BOTH").strip().upper()
    if mode in ("UP_ONLY", "DOWN_ONLY", "BOTH"):
        return mode
    return "BOTH"


def _normalize_sizing_mode(raw: str) -> str:
    mode = str(raw or "ADAPTIVE").strip().upper()
    if mode in ("ADAPTIVE", "FIXED"):
        return mode
    return "ADAPTIVE"


def _normalize_profit_mode(raw: str) -> str:
    mode = str(raw or "BALANCED").strip().upper()
    if mode in ("AGGRESSIVE", "BALANCED"):
        return mode
    return "BALANCED"


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


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def _resample_ticks_fixed_interval(
    ticks: list,
    interval_sec: float,
    max_points: int,
) -> tuple[list[float], list[float]]:
    """
    Convert irregular tick timestamps into a fixed-interval close series.
    Missing buckets are forward-filled from the latest observed tick.
    """
    if not ticks:
        return ([], [])
    interval = max(0.2, float(interval_sec))
    max_keep = max(10, int(max_points))

    # Bucket by integer index to avoid float-key precision issues.
    bucket_last_price: dict[int, float] = {}
    for t in ticks:
        try:
            ts = float(getattr(t, "timestamp"))
            px = float(getattr(t, "price"))
        except Exception:
            continue
        if px <= 0.0:
            continue
        idx = int(math.floor(ts / interval))
        bucket_last_price[idx] = px

    if not bucket_last_price:
        return ([], [])

    start_idx = min(bucket_last_price.keys())
    end_idx = max(bucket_last_price.keys())
    prices: list[float] = []
    timestamps: list[float] = []
    last_px: float | None = None

    for idx in range(start_idx, end_idx + 1):
        px = bucket_last_price.get(idx)
        if px is not None:
            last_px = float(px)
        if last_px is None:
            continue
        timestamps.append(float(idx) * interval)
        prices.append(last_px)

    if len(prices) > max_keep:
        prices = prices[-max_keep:]
        timestamps = timestamps[-max_keep:]

    return (prices, timestamps)


class TradingBot:
    def __init__(self):
        self.price_feed = BinancePriceFeed()
        self.poly_client = PolymarketClient()
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager()
        self.position_mode = _normalize_position_mode(config.trading.position_mode)
        self.live_sizing_mode = _normalize_sizing_mode(config.trading.live_sizing_mode)
        self.live_profit_mode = _normalize_profit_mode(config.trading.live_profit_mode)

        self.current_market: Optional[MarketInfo] = None
        self.current_trade: Optional[TradeRecord] = None
        self.current_trade_window_start: Optional[int] = None
        self.current_trade_signal_confidence: Optional[float] = None
        self.current_trade_signal_reason: Optional[str] = None
        self.current_trade_entry_source: Optional[str] = None
        self._trade_locked_window_start: Optional[int] = None
        self.market_start_price: Optional[float] = None
        self.recent_results: list[str] = []
        self._kill_switch_reason: Optional[str] = None

        self._running = False
        self._check_interval = 0.5  # 500ms - fast enough to catch odds lag
        self._odds_task: Optional[asyncio.Task] = None
        self._last_odds_fetch: float = 0.0
        self._state_conn = None
        self._adaptive_balance_cap: Optional[float] = None
        self._last_balance_refresh_ts: float = 0.0
        self._balance_refresh_sec = max(
            10.0,
            float(config.trading.live_balance_refresh_seconds),
        )
        self._last_auto_claim_ts: float = 0.0
        self._early_exit_opposite_hits: dict[int, int] = {}
        self._pending_settlement_exit: Optional[dict[str, Any]] = None

    def _current_live_cap(self) -> float:
        if self.live_sizing_mode == "ADAPTIVE" and not config.trading.dry_run:
            if self._adaptive_balance_cap is not None:
                return max(0.0, float(self._adaptive_balance_cap))
        return max(0.0, float(config.trading.max_bet_size))

    async def _refresh_adaptive_balance_cap(
        self,
        *,
        force: bool = False,
        reason: str = "periodic",
    ):
        if config.trading.dry_run:
            return
        if self.live_sizing_mode != "ADAPTIVE":
            return
        now = float(time.time())
        if not force and (now - self._last_balance_refresh_ts) < self._balance_refresh_sec:
            return

        balance = await self.poly_client.get_collateral_balance()
        self._last_balance_refresh_ts = now
        if balance is None:
            return

        balance = max(0.0, float(balance))
        prev = self._adaptive_balance_cap
        self._adaptive_balance_cap = balance
        if prev is None:
            logger.info(
                "Adaptive live balance cap initialized (%s): $%.2f",
                reason,
                balance,
            )
            return

        delta = abs(balance - float(prev))
        rel = delta / max(float(prev), 1e-9)
        if force or delta >= 0.5 or rel >= 0.05:
            logger.info(
                "Adaptive live balance cap refresh (%s): $%.2f -> $%.2f",
                reason,
                float(prev),
                balance,
            )

    def _compute_entry_bet_size(
        self,
        confidence: float,
        edge: float,
        *,
        expected_roi: float | None = None,
        model_prob: float | None = None,
        entry_price: float | None = None,
    ) -> float:
        if self.live_sizing_mode == "FIXED":
            fixed = float(config.trading.max_bet_size)
            if fixed <= 0.0:
                return 0.0
            return round(
                max(float(config.trading.min_bet_size), fixed),
                2,
            )

        cap = self._current_live_cap()
        if cap <= 0.0:
            return 0.0

        conf = _clamp(float(confidence), 0.0, 1.0)
        edge_v = _clamp(float(edge), 0.0, 0.30)
        base_frac = _clamp(float(config.trading.live_adaptive_base_frac), 0.005, 0.80)
        min_frac = _clamp(float(config.trading.live_adaptive_min_frac), 0.005, 0.80)
        max_frac = _clamp(float(config.trading.live_adaptive_max_frac), min_frac, 0.80)
        edge_boost = max(0.0, float(config.trading.live_adaptive_edge_boost))
        conf_boost = max(0.0, float(config.trading.live_adaptive_conf_boost))

        frac = base_frac + (edge_v * edge_boost) + (max(0.0, conf - 0.50) * conf_boost)
        frac = _clamp(frac, min_frac, max_frac)
        bet_frac = frac

        if self.live_profit_mode == "AGGRESSIVE":
            fee_rate = max(0.0, float(config.trading.fee_rate))
            p = _clamp(float(model_prob or 0.0), 0.0, 1.0)
            px = float(entry_price or 0.0)
            gain = ((1.0 / px) - 1.0 - fee_rate) if 0.0 < px < 1.0 else 0.0
            loss = 1.0 + fee_rate
            kelly = 0.0
            if gain > 1e-9:
                kelly = ((p * gain) - ((1.0 - p) * loss)) / (gain * loss)
                kelly = _clamp(kelly, 0.0, 1.0)

            kelly_frac = _clamp(float(config.trading.live_aggressive_kelly_frac), 0.0, 1.0)
            growth_frac = kelly_frac * kelly * (1.0 + 0.45 * _clamp((conf - 0.50) / 0.50, 0.0, 1.0))
            growth_cap = _clamp(float(config.trading.live_aggressive_max_frac), max_frac, 0.40)
            growth_frac = min(growth_cap, growth_frac)

            if expected_roi is not None and expected_roi > 0.0:
                roi_frac = min(growth_cap, base_frac * _clamp(float(expected_roi) / 0.08, 0.6, 2.2))
                growth_frac = max(growth_frac, roi_frac)

            bet_frac = max(bet_frac, growth_frac)

        bet = cap * bet_frac

        if self.risk_mgr.consecutive_losses > 0:
            if self.live_profit_mode == "AGGRESSIVE":
                deboost = _clamp(float(config.trading.live_aggressive_loss_deboost), 0.70, 0.95)
            else:
                deboost = 0.80
            bet *= deboost ** min(self.risk_mgr.consecutive_losses, 3)

        bet = _clamp(bet, float(config.trading.min_bet_size), cap)
        return round(float(bet), 2)

    def _estimate_fast_lane_prob_up(self, ctx: MarketContext) -> float | None:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 6:
            return None
        if ctx.market_start_price <= 0.0 or ctx.current_binance_price <= 0.0:
            return None

        now_ts = float(ctx.recent_timestamps[n - 1])
        lookback = max(15.0, float(config.trading.fast_lane_vol_lookback_sec))
        min_ts = now_ts - lookback

        prices: list[float] = []
        timestamps: list[float] = []
        for i in range(n):
            try:
                ts = float(ctx.recent_timestamps[i])
                px = float(ctx.recent_prices[i])
            except Exception:
                continue
            if ts < min_ts or px <= 0.0:
                continue
            prices.append(px)
            timestamps.append(ts)

        if len(prices) < 6:
            return None

        dlogs: list[float] = []
        dts: list[float] = []
        for i in range(1, len(prices)):
            dt = float(timestamps[i] - timestamps[i - 1])
            if dt <= 1e-6:
                continue
            ratio = float(prices[i] / max(prices[i - 1], 1e-12))
            if ratio <= 0.0:
                continue
            dlogs.append(math.log(ratio))
            dts.append(dt)

        if len(dlogs) < 4:
            return None

        total_dt = float(sum(dts))
        if total_dt <= 1e-6:
            return None

        mu = float(sum(dlogs) / total_dt)
        resid_sq = 0.0
        for i in range(len(dlogs)):
            dt = max(dts[i], 1e-6)
            err = float(dlogs[i] - (mu * dt))
            resid_sq += (err * err) / dt
        var = float(resid_sq / max(len(dlogs), 1))
        sigma = math.sqrt(max(var, 1e-12))

        t = max(1.0, float(ctx.seconds_remaining))
        x = math.log(float(ctx.current_binance_price) / float(ctx.market_start_price))
        drift_weight = _clamp(float(config.trading.fast_lane_drift_weight), 0.0, 2.0)
        drift = _clamp((mu - 0.5 * sigma * sigma) * t * drift_weight, -0.0035, 0.0035)
        denom = max(sigma * math.sqrt(t), 1e-8)
        z = _clamp((x + drift) / denom, -8.0, 8.0)
        return _clamp(_normal_cdf(z), 0.001, 0.999)

    def _evaluate_fast_lane_signal(
        self,
        ctx: MarketContext,
        now_ts: float,
    ) -> dict[str, float | str] | None:
        if not bool(config.trading.fast_lane_enabled):
            return None

        elapsed = float(ctx.seconds_elapsed)
        remaining = float(ctx.seconds_remaining)
        if elapsed < float(config.trading.fast_lane_min_seconds_elapsed):
            return None
        if elapsed > float(config.trading.fast_lane_max_seconds_elapsed):
            return None
        if remaining < float(config.trading.fast_lane_min_seconds_remaining):
            return None

        start_price = float(ctx.market_start_price)
        current_price = float(ctx.current_binance_price)
        if start_price <= 0.0 or current_price <= 0.0:
            return None

        move_pct = ((current_price - start_price) / start_price) * 100.0
        abs_move_pct = abs(move_pct)
        if abs_move_pct < float(config.trading.fast_lane_min_move_pct):
            return None
        if abs_move_pct > float(config.trading.fast_lane_max_move_pct):
            return None

        direction = "UP" if move_pct > 0.0 else "DOWN"
        recent_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=float(now_ts),
            lookback_sec=float(config.trading.fast_lane_recent_lookback_sec),
        )
        if recent_move is None:
            return None
        min_recent = float(config.trading.fast_lane_min_recent_move_pct)
        if direction == "UP" and recent_move < min_recent:
            return None
        if direction == "DOWN" and recent_move > -min_recent:
            return None
        trend_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=float(now_ts),
            lookback_sec=float(config.trading.live_trend_align_lookback_sec),
        )
        if trend_move is None:
            return None
        trend_thr = abs(float(config.trading.live_trend_align_max_opposing_move_pct))
        if direction == "UP" and trend_move < -trend_thr:
            return None
        if direction == "DOWN" and trend_move > trend_thr:
            return None

        up_ask = _safe_prob(ctx.poly_up_ask) or _safe_prob(ctx.poly_up_price)
        down_ask = _safe_prob(ctx.poly_down_ask) or _safe_prob(ctx.poly_down_price)
        side_ask = up_ask if direction == "UP" else down_ask
        if side_ask is None or not (0.01 < side_ask < 0.99):
            return None
        if side_ask > float(config.trading.fast_lane_max_entry_price):
            return None
        opposite_ask = down_ask if direction == "UP" else up_ask

        p_up = self._estimate_fast_lane_prob_up(ctx)
        if p_up is None:
            move_scale = max(float(config.trading.fast_lane_min_move_pct), 1e-6)
            z = _clamp((move_pct / move_scale) * 0.60, -8.0, 8.0)
            p_up = _clamp(_normal_cdf(z), 0.001, 0.999)
        p_dir = p_up if direction == "UP" else (1.0 - p_up)
        p_dir = _clamp(float(p_dir), 0.001, 0.999)
        if p_dir < float(config.trading.fast_lane_min_direction_prob):
            return None

        prob_edge = float(p_dir - side_ask)
        if prob_edge < float(config.trading.fast_lane_min_prob_edge):
            return None

        fee_rate = max(0.0, float(config.trading.fee_rate))
        expected_roi = float((p_dir / side_ask) - 1.0 - fee_rate)
        if expected_roi < float(config.trading.fast_lane_min_expected_roi):
            return None

        confidence = _clamp(
            0.45 + (p_dir - 0.5) * 1.20 + prob_edge * 0.50,
            0.0,
            1.0,
        )
        if side_ask is not None and opposite_ask is not None:
            contra_gap = float(opposite_ask) - float(side_ask)
            if contra_gap > float(config.trading.live_max_contra_gap):
                if not (
                    p_dir >= float(config.trading.live_contra_override_min_model_prob)
                    and confidence >= float(config.trading.live_contra_override_min_conf)
                ):
                    return None
        reason = (
            f"fast_lane: move={move_pct:+.4f}% recent={recent_move:+.4f}% "
            f"p={p_dir:.3f} ask={side_ask:.3f} net_ev={expected_roi:+.3%}"
        )

        return {
            "direction": direction,
            "confidence": float(confidence),
            "prob_edge": float(prob_edge),
            "entry_price": float(side_ask),
            "expected_roi": float(expected_roi),
            "direction_prob": float(p_dir),
            "move_pct": float(move_pct),
            "recent_move_pct": float(recent_move),
            "reason": reason,
        }

    def _ensure_state_conn(self):
        if self._state_conn is not None:
            return self._state_conn
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
        self._state_conn = conn
        return conn

    def _close_state_conn(self):
        if self._state_conn is None:
            return
        try:
            self._state_conn.close()
        except Exception:
            pass
        self._state_conn = None

    def _persist_runtime_state(self):
        try:
            conn = self._ensure_state_conn()
            risk_payload = {
                "daily_pnl": float(self.risk_mgr.daily_pnl),
                "consecutive_losses": int(self.risk_mgr.consecutive_losses),
                "cooldown_until": float(self.risk_mgr.cooldown_until),
                "daily_reset_time": float(self.risk_mgr.daily_reset_time),
            }
            trade_payload = None
            if self.current_trade is not None and self.current_trade.result == "PENDING":
                trade_payload = {
                    "timestamp": float(self.current_trade.timestamp),
                    "direction": str(self.current_trade.direction),
                    "amount": float(self.current_trade.amount),
                    "price": float(self.current_trade.price),
                    "result": str(self.current_trade.result),
                    "window_start": int(self.current_trade_window_start or 0),
                    "market_start_price": float(self.market_start_price or 0.0),
                    "signal_confidence": (
                        float(self.current_trade_signal_confidence)
                        if self.current_trade_signal_confidence is not None
                        else None
                    ),
                    "signal_reason": (
                        str(self.current_trade_signal_reason)
                        if self.current_trade_signal_reason
                        else None
                    ),
                    "entry_source": (
                        str(self.current_trade_entry_source)
                        if self.current_trade_entry_source
                        else None
                    ),
                }
            now_ts = float(time.time())
            risk_json = json.dumps(risk_payload, ensure_ascii=True)
            trade_json = json.dumps(trade_payload, ensure_ascii=True) if trade_payload else None
            kill_switch = 1 if self._kill_switch_reason else 0
            kill_reason = str(self._kill_switch_reason or "")
            if is_sqlite_backend():
                sql = (
                    "INSERT OR REPLACE INTO bot_runtime_state "
                    "(id, updated_at, risk_json, trade_json, kill_switch, kill_reason) "
                    "VALUES (1, ?, ?, ?, ?, ?)"
                )
            else:
                sql = (
                    "INSERT INTO bot_runtime_state "
                    "(id, updated_at, risk_json, trade_json, kill_switch, kill_reason) "
                    "VALUES (1, ?, ?, ?, ?, ?) "
                    "ON DUPLICATE KEY UPDATE "
                    "updated_at=VALUES(updated_at), "
                    "risk_json=VALUES(risk_json), "
                    "trade_json=VALUES(trade_json), "
                    "kill_switch=VALUES(kill_switch), "
                    "kill_reason=VALUES(kill_reason)"
                )
            execute_write(
                conn,
                sql,
                (now_ts, risk_json, trade_json, kill_switch, kill_reason),
            )
            conn.commit()
        except Exception as e:
            logger.warning("runtime state persist failed: %s", e)

    def _load_runtime_state(self):
        try:
            conn = self._ensure_state_conn()
            row = fetch_one_dict(
                conn,
                "SELECT risk_json, trade_json, kill_switch, kill_reason FROM bot_runtime_state WHERE id=1",
            )
            if not row:
                return

            try:
                risk_payload = json.loads(str(row.get("risk_json") or "{}"))
                self.risk_mgr.daily_pnl = float(risk_payload.get("daily_pnl") or 0.0)
                self.risk_mgr.consecutive_losses = int(risk_payload.get("consecutive_losses") or 0)
                self.risk_mgr.cooldown_until = float(risk_payload.get("cooldown_until") or 0.0)
                reset_ts = float(risk_payload.get("daily_reset_time") or 0.0)
                if reset_ts > 0.0:
                    self.risk_mgr.daily_reset_time = reset_ts
            except Exception as e:
                logger.warning("runtime risk state load failed: %s", e)

            kill_flag = bool(int(row.get("kill_switch") or 0))
            if kill_flag:
                reason = str(row.get("kill_reason") or "").strip()
                self._kill_switch_reason = reason or "latched kill-switch"

            trade_raw = row.get("trade_json")
            if trade_raw:
                try:
                    trade_payload = json.loads(str(trade_raw))
                except Exception:
                    trade_payload = None
                if isinstance(trade_payload, dict):
                    direction = str(trade_payload.get("direction") or "").upper()
                    amount = float(trade_payload.get("amount") or 0.0)
                    price = float(trade_payload.get("price") or 0.0)
                    if direction in {"UP", "DOWN"} and amount > 0.0 and 0.0 < price < 1.0:
                        self.current_trade = TradeRecord(
                            timestamp=float(trade_payload.get("timestamp") or time.time()),
                            direction=direction,
                            amount=amount,
                            price=price,
                            result="PENDING",
                            pnl=0.0,
                        )
                        ws = int(trade_payload.get("window_start") or 0)
                        self.current_trade_window_start = ws if ws > 0 else None
                        self._trade_locked_window_start = self.current_trade_window_start
                        conf = trade_payload.get("signal_confidence")
                        self.current_trade_signal_confidence = (
                            float(conf)
                            if conf is not None
                            else None
                        )
                        self.current_trade_signal_reason = (
                            str(trade_payload.get("signal_reason") or "").strip() or None
                        )
                        self.current_trade_entry_source = (
                            str(trade_payload.get("entry_source") or "").strip() or None
                        )
                        saved_start_px = float(trade_payload.get("market_start_price") or 0.0)
                        if saved_start_px > 0.0:
                            self.market_start_price = saved_start_px
                        logger.warning(
                            "Recovered pending local runtime trade: %s amount=$%.2f @ %.4f ws=%s",
                            direction,
                            amount,
                            price,
                            self.current_trade_window_start,
                        )
                        self._upsert_live_trade_open(
                            trade=self.current_trade,
                            window_start=self.current_trade_window_start,
                            signal_confidence=None,
                            signal_reason="Recovered pending trade from runtime state",
                            entry_source="runtime_recover",
                        )
        except Exception as e:
            logger.warning("runtime state load failed: %s", e)

    def _set_kill_switch(self, reason: str):
        msg = str(reason or "unknown kill-switch").strip()
        if not msg:
            msg = "unknown kill-switch"
        self._kill_switch_reason = msg
        self._running = False
        logger.error("KILL-SWITCH TRIGGERED: %s", msg)
        self._persist_runtime_state()

    def _clear_kill_switch(self):
        self._kill_switch_reason = None
        self._persist_runtime_state()

    def _resolve_window_bounds(
        self,
        *,
        window_start: Optional[int],
        trade_timestamp: Optional[float] = None,
    ) -> tuple[int, int]:
        interval = max(1, int(config.polymarket.interval_seconds))
        ws = int(window_start or 0)
        if ws <= 0 and trade_timestamp is not None and float(trade_timestamp) > 0.0:
            ws = int(float(trade_timestamp) // interval) * interval
        if ws <= 0 and self.current_market is not None:
            ws = int(self.current_market.start_timestamp)
        if ws <= 0:
            now = time.time()
            ws = int(now // interval) * interval

        if self.current_market is not None and int(self.current_market.start_timestamp) == ws:
            we = int(self.current_market.end_timestamp)
        else:
            we = int(ws + interval)
        return ws, we

    def _upsert_live_trade_open(
        self,
        *,
        trade: TradeRecord,
        window_start: Optional[int],
        signal_confidence: Optional[float],
        signal_reason: Optional[str],
        entry_source: str,
    ):
        try:
            entry_price = float(trade.price)
            stake = float(trade.amount)
            if not (0.0 < entry_price < 1.0) or stake <= 0.0:
                return
            ws, we = self._resolve_window_bounds(
                window_start=window_start,
                trade_timestamp=float(trade.timestamp),
            )
            shares = float(stake / entry_price)
            payout_multiple = float(1.0 / entry_price)
            potential_win_pnl = float(shares - stake)
            opened_at = float(trade.timestamp or time.time())
            conf = (
                max(0.0, min(1.0, float(signal_confidence)))
                if signal_confidence is not None
                else None
            )
            reason = str(signal_reason or "").strip() or None
            source = str(entry_source or "").strip() or None

            conn = self._ensure_state_conn()
            if is_sqlite_backend():
                sql = (
                    "INSERT INTO live_trades "
                    "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                    "potential_win_pnl, signal_confidence, signal_reason, entry_source, status, opened_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?) "
                    "ON CONFLICT(window_start) DO UPDATE SET "
                    "window_end=excluded.window_end, "
                    "direction=excluded.direction, "
                    "stake=excluded.stake, "
                    "entry_price=excluded.entry_price, "
                    "payout_multiple=excluded.payout_multiple, "
                    "shares=excluded.shares, "
                    "potential_win_pnl=excluded.potential_win_pnl, "
                    "signal_confidence=COALESCE(excluded.signal_confidence, live_trades.signal_confidence), "
                    "signal_reason=COALESCE(excluded.signal_reason, live_trades.signal_reason), "
                    "entry_source=COALESCE(excluded.entry_source, live_trades.entry_source), "
                    "status='OPEN', "
                    "opened_at=excluded.opened_at, "
                    "closed_at=NULL, "
                    "actual_outcome=NULL, "
                    "won=NULL, "
                    "pnl=NULL, "
                    "roi_pct=NULL, "
                    "close_reason=NULL"
                )
            else:
                sql = (
                    "INSERT INTO live_trades "
                    "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                    "potential_win_pnl, signal_confidence, signal_reason, entry_source, status, opened_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?) "
                    "ON DUPLICATE KEY UPDATE "
                    "window_end=VALUES(window_end), "
                    "direction=VALUES(direction), "
                    "stake=VALUES(stake), "
                    "entry_price=VALUES(entry_price), "
                    "payout_multiple=VALUES(payout_multiple), "
                    "shares=VALUES(shares), "
                    "potential_win_pnl=VALUES(potential_win_pnl), "
                    "signal_confidence=COALESCE(VALUES(signal_confidence), signal_confidence), "
                    "signal_reason=COALESCE(VALUES(signal_reason), signal_reason), "
                    "entry_source=COALESCE(VALUES(entry_source), entry_source), "
                    "status='OPEN', "
                    "opened_at=VALUES(opened_at), "
                    "closed_at=NULL, "
                    "actual_outcome=NULL, "
                    "won=NULL, "
                    "pnl=NULL, "
                    "roi_pct=NULL, "
                    "close_reason=NULL"
                )
            execute_write(
                conn,
                sql,
                (
                    int(ws),
                    int(we),
                    str(trade.direction),
                    float(stake),
                    float(entry_price),
                    float(payout_multiple),
                    float(shares),
                    float(potential_win_pnl),
                    conf,
                    reason,
                    source,
                    float(opened_at),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning("live trade OPEN upsert failed: %s", e)

    def _upsert_live_trade_closed(
        self,
        *,
        trade: TradeRecord,
        window_start: Optional[int],
        actual_outcome: str,
        close_reason: str,
    ):
        try:
            entry_price = float(trade.price)
            stake = float(trade.amount)
            if not (0.0 < entry_price < 1.0) or stake <= 0.0:
                return
            ws, we = self._resolve_window_bounds(
                window_start=window_start,
                trade_timestamp=float(trade.timestamp),
            )
            shares = float(stake / entry_price)
            payout_multiple = float(1.0 / entry_price)
            potential_win_pnl = float(shares - stake)
            opened_at = float(trade.timestamp or time.time())
            closed_at = float(time.time())
            pnl = float(trade.pnl)
            roi_pct = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0
            won = 1 if str(trade.result or "").upper() == "WIN" else 0
            actual = str(actual_outcome or "").upper() or None
            close = str(close_reason or "").strip() or None

            conn = self._ensure_state_conn()
            if is_sqlite_backend():
                sql = (
                    "INSERT INTO live_trades "
                    "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                    "potential_win_pnl, status, opened_at, closed_at, actual_outcome, won, pnl, roi_pct, close_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(window_start) DO UPDATE SET "
                    "window_end=excluded.window_end, "
                    "direction=excluded.direction, "
                    "stake=excluded.stake, "
                    "entry_price=excluded.entry_price, "
                    "payout_multiple=excluded.payout_multiple, "
                    "shares=excluded.shares, "
                    "potential_win_pnl=excluded.potential_win_pnl, "
                    "status='CLOSED', "
                    "closed_at=excluded.closed_at, "
                    "actual_outcome=excluded.actual_outcome, "
                    "won=excluded.won, "
                    "pnl=excluded.pnl, "
                    "roi_pct=excluded.roi_pct, "
                    "close_reason=excluded.close_reason"
                )
            else:
                sql = (
                    "INSERT INTO live_trades "
                    "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                    "potential_win_pnl, status, opened_at, closed_at, actual_outcome, won, pnl, roi_pct, close_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?) "
                    "ON DUPLICATE KEY UPDATE "
                    "window_end=VALUES(window_end), "
                    "direction=VALUES(direction), "
                    "stake=VALUES(stake), "
                    "entry_price=VALUES(entry_price), "
                    "payout_multiple=VALUES(payout_multiple), "
                    "shares=VALUES(shares), "
                    "potential_win_pnl=VALUES(potential_win_pnl), "
                    "status='CLOSED', "
                    "closed_at=VALUES(closed_at), "
                    "actual_outcome=VALUES(actual_outcome), "
                    "won=VALUES(won), "
                    "pnl=VALUES(pnl), "
                    "roi_pct=VALUES(roi_pct), "
                    "close_reason=VALUES(close_reason)"
                )
            execute_write(
                conn,
                sql,
                (
                    int(ws),
                    int(we),
                    str(trade.direction),
                    float(stake),
                    float(entry_price),
                    float(payout_multiple),
                    float(shares),
                    float(potential_win_pnl),
                    float(opened_at),
                    float(closed_at),
                    actual,
                    int(won),
                    float(pnl),
                    float(roi_pct),
                    close,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning("live trade CLOSED upsert failed: %s", e)

    async def _reconcile_exchange_for_current_market(self, phase: str) -> bool:
        if config.trading.dry_run:
            return True
        if self.current_market is None:
            return True

        exposure = await self.poly_client.inspect_market_exposure(self.current_market)
        if not bool(exposure.get("ok")):
            self._set_kill_switch(
                f"{phase} reconcile failed: {exposure.get('error') or 'unknown exchange error'}"
            )
            return False

        open_orders_total = int(exposure.get("open_orders_total") or 0)
        if open_orders_total > 0:
            logger.warning(
                "%s reconcile: found %s open orders; attempting cancel",
                phase,
                open_orders_total,
            )
            cancel_res = await self.poly_client.cancel_market_orders(self.current_market)
            if not bool(cancel_res.get("ok", False)):
                logger.warning("%s reconcile: cancel errors=%s", phase, cancel_res.get("errors"))
            await asyncio.sleep(0.2)
            exposure = await self.poly_client.inspect_market_exposure(self.current_market)
            open_orders_total = int(exposure.get("open_orders_total") or 0)
            if open_orders_total > 0:
                self._set_kill_switch(
                    f"{phase} reconcile blocked: {open_orders_total} open orders remain after cancel"
                )
                return False

        up_balance = float(exposure.get("up_balance") or 0.0)
        down_balance = float(exposure.get("down_balance") or 0.0)
        eps = 1e-9
        if up_balance > eps and down_balance > eps:
            self._set_kill_switch(
                f"{phase} reconcile blocked: both-side balances detected (UP={up_balance:.6f}, DOWN={down_balance:.6f})"
            )
            return False

        if self.current_trade is not None and self.current_trade.result == "PENDING":
            expected = str(self.current_trade.direction or "").upper()
            live_side_balance = up_balance if expected == "UP" else down_balance
            opposite_balance = down_balance if expected == "UP" else up_balance
            if opposite_balance > eps:
                self._set_kill_switch(
                    f"{phase} reconcile mismatch: opposite-side balance detected ({opposite_balance:.6f})"
                )
                return False
            if live_side_balance <= eps:
                self._set_kill_switch(
                    f"{phase} reconcile mismatch: local pending trade exists but on-exchange balance is zero"
                )
                return False
            return True

        if up_balance > eps or down_balance > eps:
            recovered_direction = "UP" if up_balance >= down_balance else "DOWN"
            recovered_shares = up_balance if recovered_direction == "UP" else down_balance
            ref_ask = (
                _safe_prob(self.current_market.up_best_ask)
                if recovered_direction == "UP"
                else _safe_prob(self.current_market.down_best_ask)
            )
            if ref_ask is None:
                ref_ask = (
                    _safe_prob(self.current_market.up_price)
                    if recovered_direction == "UP"
                    else _safe_prob(self.current_market.down_price)
                )
            if ref_ask is None:
                ref_ask = 0.5
            recovered_amount = max(
                float(config.trading.min_bet_size),
                float(recovered_shares * ref_ask),
            )
            self.current_trade = TradeRecord(
                timestamp=float(time.time()),
                direction=recovered_direction,
                amount=float(recovered_amount),
                price=float(ref_ask),
                result="PENDING",
                pnl=0.0,
            )
            self.current_trade_window_start = int(self.current_market.start_timestamp)
            self.current_trade_signal_confidence = None
            self.current_trade_signal_reason = None
            self.current_trade_entry_source = "reconcile"
            self._trade_locked_window_start = self.current_trade_window_start
            if self.market_start_price is None or self.market_start_price <= 0.0:
                self.market_start_price = self.price_feed.current_price
            logger.warning(
                "%s reconcile: recovered exchange position as pending trade "
                "(dir=%s shares=%.6f est_notional=$%.2f)",
                phase,
                recovered_direction,
                recovered_shares,
                recovered_amount,
            )
            self._upsert_live_trade_open(
                trade=self.current_trade,
                window_start=self.current_trade_window_start,
                signal_confidence=None,
                signal_reason=f"Recovered exchange position during {phase}",
                entry_source="reconcile",
            )
            self._persist_runtime_state()
        return True

    def _handle_order_result(
        self,
        result: Optional[dict],
        *,
        direction: str,
        fallback_amount: float,
        fallback_price: float,
        source: str,
        signal_confidence: Optional[float] = None,
        signal_reason: Optional[str] = None,
    ) -> bool:
        if result is None:
            self._set_kill_switch(f"{source}: order result missing (possible unknown fill)")
            return False

        if bool(result.get("uncertain_fill", False)):
            reason = str(result.get("reason") or "unknown-fill error")
            self._set_kill_switch(f"{source}: uncertain fill detected -> {reason}")
            return False

        if not bool(result.get("accepted", True)):
            logger.info(
                "%s order blocked/rejected: mode=%s status=%s reason=%s",
                source,
                result.get("mode"),
                result.get("status"),
                result.get("reason"),
            )
            return False

        if not bool(result.get("filled", False)):
            logger.info(
                "%s order not filled: mode=%s status=%s reason=%s",
                source,
                result.get("mode"),
                result.get("status"),
                result.get("reason"),
            )
            return False

        executed_notional = float(result.get("executed_notional") or 0.0)
        executed_price = float(result.get("executed_price") or 0.0)
        if executed_notional <= 0.0:
            executed_notional = float(fallback_amount)
        if not (0.0 < executed_price < 1.0):
            executed_price = float(fallback_price)

        self.current_trade = self.risk_mgr.record_trade(
            direction=direction,
            amount=executed_notional,
            price=executed_price,
        )
        self.current_trade_window_start = (
            int(self.current_market.start_timestamp) if self.current_market is not None else None
        )
        self.current_trade_signal_confidence = (
            max(0.0, min(1.0, float(signal_confidence)))
            if signal_confidence is not None
            else None
        )
        self.current_trade_signal_reason = str(signal_reason or "").strip() or None
        self.current_trade_entry_source = str(source or "").strip() or None
        self._trade_locked_window_start = self.current_trade_window_start
        self._upsert_live_trade_open(
            trade=self.current_trade,
            window_start=self.current_trade_window_start,
            signal_confidence=self.current_trade_signal_confidence,
            signal_reason=self.current_trade_signal_reason,
            entry_source=source,
        )
        self._persist_runtime_state()
        return True

    def _resolve_trade_quotes(self, direction: str) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
        if self.current_market is None:
            return None, None, None, None
        d = str(direction or "").upper()
        if d == "UP":
            token_id = str(self.current_market.up_token_id or "")
            side_bid = _safe_prob(self.current_market.up_best_bid) or _safe_prob(self.current_market.up_price)
            side_ask = _safe_prob(self.current_market.up_best_ask) or _safe_prob(self.current_market.up_price)
            opposite_ask = _safe_prob(self.current_market.down_best_ask) or _safe_prob(self.current_market.down_price)
            return token_id, side_bid, side_ask, opposite_ask
        if d == "DOWN":
            token_id = str(self.current_market.down_token_id or "")
            side_bid = _safe_prob(self.current_market.down_best_bid) or _safe_prob(self.current_market.down_price)
            side_ask = _safe_prob(self.current_market.down_best_ask) or _safe_prob(self.current_market.down_price)
            opposite_ask = _safe_prob(self.current_market.up_best_ask) or _safe_prob(self.current_market.up_price)
            return token_id, side_bid, side_ask, opposite_ask
        return None, None, None, None

    def _mark_to_market_trade(self, trade: TradeRecord, side_bid: Optional[float]) -> tuple[float, float, float]:
        stake = max(0.0, float(trade.amount or 0.0))
        entry_price = float(trade.price or 0.0)
        if stake <= 0.0 or not (0.0 < entry_price < 1.0):
            return 0.0, 0.0, 0.0
        shares = float(stake / entry_price)
        px = _safe_prob(side_bid)
        if px is None:
            return 0.0, 0.0, 0.0
        current_value = float(shares * px)
        raw_pnl = float(current_value - stake)
        pnl = float(apply_fee_to_pnl(raw_pnl, stake))
        roi_pct = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0
        return float(px), float(pnl), float(roi_pct)

    def _finalize_trade_close(
        self,
        *,
        trade: TradeRecord,
        pnl: float,
        close_reason: str,
        actual_outcome: str,
    ):
        realized = float(pnl)
        trade.pnl = realized
        won = bool(realized >= 0.0)
        trade.result = "WIN" if won else "LOSS"
        if won:
            self.risk_mgr.consecutive_losses = 0
        else:
            self.risk_mgr.consecutive_losses += 1
        self.risk_mgr.daily_pnl += realized
        self._upsert_live_trade_closed(
            trade=trade,
            window_start=self.current_trade_window_start,
            actual_outcome=actual_outcome,
            close_reason=close_reason,
        )
        self.current_trade = None
        self.current_trade_signal_confidence = None
        self.current_trade_signal_reason = None
        self.current_trade_entry_source = None
        self._early_exit_opposite_hits.clear()
        self._persist_runtime_state()

    def _schedule_settlement_exit_for_previous_window(
        self,
        *,
        trade: TradeRecord,
        market: Optional[MarketInfo],
        won: bool,
    ) -> None:
        self._pending_settlement_exit = None
        if config.trading.dry_run:
            return
        if not bool(config.trading.live_settlement_exit_enabled):
            return
        if not won:
            return
        if market is None:
            return

        direction = str(trade.direction or "").upper()
        if direction == "UP":
            token_id = str(market.up_token_id or "")
        elif direction == "DOWN":
            token_id = str(market.down_token_id or "")
        else:
            return
        if not token_id:
            return

        stake = max(0.0, float(trade.amount or 0.0))
        entry_price = float(trade.price or 0.0)
        if stake <= 0.0 or not (0.0 < entry_price < 1.0):
            return
        shares = float(stake / entry_price)
        if shares <= 0.0:
            return

        d1 = max(0.0, float(config.trading.live_settlement_exit_delay1_sec))
        d2 = max(0.0, float(config.trading.live_settlement_exit_delay2_sec))
        offsets = sorted({float(d1), float(d2)})
        if not offsets:
            return

        self._pending_settlement_exit = {
            "market": market,
            "slug": str(market.slug or ""),
            "window_start": int(market.start_timestamp or 0),
            "window_end": int(market.end_timestamp or 0),
            "direction": direction,
            "token_id": token_id,
            "shares": float(shares),
            "stake": float(stake),
            "entry_price": float(entry_price),
            "attempt_offsets": offsets,
            "attempt_index": 0,
        }
        logger.info(
            "Scheduled post-settlement exit: slug=%s dir=%s shares=%.6f @+%ss/+%ss",
            str(market.slug or ""),
            direction,
            float(shares),
            int(offsets[0]),
            int(offsets[-1]),
        )

    async def _maybe_run_pending_settlement_exit(self, *, now_ts: float) -> None:
        pending = self._pending_settlement_exit
        if pending is None or config.trading.dry_run:
            return

        offsets = [float(x) for x in list(pending.get("attempt_offsets") or []) if float(x) >= 0.0]
        if not offsets:
            self._pending_settlement_exit = None
            return

        attempt_idx = int(pending.get("attempt_index") or 0)
        if attempt_idx >= len(offsets):
            logger.info("Post-settlement exit attempts exhausted; fallback to claim.")
            self._pending_settlement_exit = None
            await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        window_end = float(pending.get("window_end") or 0.0)
        due_ts = window_end + float(offsets[attempt_idx])
        if float(now_ts) < due_ts:
            return

        # Consume this slot now to avoid repeated retries on the same timestamp.
        pending["attempt_index"] = attempt_idx + 1

        market = pending.get("market")
        if not isinstance(market, MarketInfo):
            self._pending_settlement_exit = None
            return

        try:
            await self.poly_client.refresh_odds(market)
        except Exception as e:
            logger.warning("Post-settlement exit odds refresh failed: %s", e)
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        direction = str(pending.get("direction") or "").upper()
        if direction == "UP":
            side_bid = _safe_prob(market.up_best_bid) or _safe_prob(market.up_price)
        elif direction == "DOWN":
            side_bid = _safe_prob(market.down_best_bid) or _safe_prob(market.down_price)
        else:
            side_bid = None

        token_id = str(pending.get("token_id") or "")
        shares = float(pending.get("shares") or 0.0)
        stake = float(pending.get("stake") or 0.0)
        if not token_id or shares <= 0.0 or stake <= 0.0 or side_bid is None:
            logger.warning(
                "Post-settlement exit skipped (attempt %s/%s): invalid quote/token",
                int(pending.get("attempt_index") or 0),
                len(offsets),
            )
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        est_notional = float(shares * float(side_bid))
        est_pnl = float(apply_fee_to_pnl(est_notional - stake, stake))
        est_roi_pct = float((est_pnl / stake) * 100.0) if stake > 0.0 else 0.0
        min_bid = float(config.trading.live_settlement_exit_min_bid)
        min_roi = float(config.trading.live_settlement_exit_min_roi_pct)
        if float(side_bid) < min_bid or float(est_roi_pct) < min_roi:
            logger.info(
                "Post-settlement exit skipped (attempt %s/%s): bid=%.3f roi=%+.2f%% (need bid>=%.3f roi>=%.2f%%)",
                int(pending.get("attempt_index") or 0),
                len(offsets),
                float(side_bid),
                float(est_roi_pct),
                float(min_bid),
                float(min_roi),
            )
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        exit_result = await self.poly_client.place_exit_order(
            token_id=str(token_id),
            side="SELL",
            shares=float(shares),
            reference_bid=float(side_bid),
        )

        if exit_result is None:
            logger.warning("Post-settlement exit failed: missing order result")
        elif bool(exit_result.get("uncertain_fill", False)):
            logger.warning("Post-settlement exit uncertain fill: %s", exit_result.get("reason"))
        elif not bool(exit_result.get("accepted", True)):
            logger.warning(
                "Post-settlement exit rejected: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
        elif not bool(exit_result.get("filled", False)):
            logger.warning(
                "Post-settlement exit not filled: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
        else:
            executed_size = float(exit_result.get("executed_size") or 0.0)
            if executed_size <= 0.0 or executed_size < (shares * 0.95):
                logger.warning(
                    "Post-settlement exit partial/invalid fill: %.6f / %.6f",
                    executed_size,
                    shares,
                )
            else:
                executed_notional = float(exit_result.get("executed_notional") or 0.0)
                executed_price = float(exit_result.get("executed_price") or 0.0)
                if executed_notional <= 0.0 and executed_size > 0.0 and 0.0 < executed_price < 1.0:
                    executed_notional = float(executed_size * executed_price)
                realized_pnl = float(apply_fee_to_pnl(executed_notional - stake, stake))
                logger.info(
                    "Post-settlement exit success: slug=%s dir=%s fill_px=%.3f pnl=$%+.2f roi=%+.2f%%",
                    str(pending.get("slug") or ""),
                    direction,
                    float(executed_price),
                    float(realized_pnl),
                    (float(realized_pnl) / max(float(stake), 1e-9)) * 100.0,
                )
                self._pending_settlement_exit = None
                await self._refresh_adaptive_balance_cap(force=True, reason="post_settlement_exit")
                return

        if int(pending.get("attempt_index") or 0) >= len(offsets):
            self._pending_settlement_exit = None
            await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")

    async def _maybe_auto_claim(self, *, now_ts: float, force: bool = False, reason: str = "periodic"):
        if config.trading.dry_run:
            return
        if not bool(config.trading.live_auto_claim_enabled):
            return
        if self._pending_settlement_exit is not None and str(reason or "").strip().lower() == "post_settlement":
            logger.info("Deferring immediate claim: pending post-settlement exit window active.")
            return
        if self._pending_settlement_exit is not None and not force:
            return
        interval = max(10.0, float(config.trading.live_auto_claim_interval_seconds))
        if not force and (now_ts - self._last_auto_claim_ts) < interval:
            return
        self._last_auto_claim_ts = float(now_ts)
        result = await self.poly_client.auto_claim_winnings()
        if bool(result.get("ok")):
            claimed = float(result.get("claimed") or 0.0)
            if claimed > 0.0:
                logger.info("Auto-claim success (%s): +$%.2f", reason, claimed)
            return
        if bool(result.get("supported", False)):
            logger.warning(
                "Auto-claim failed (%s): %s",
                reason,
                result.get("error") or result.get("status") or "unknown",
            )

    async def _maybe_early_exit_open_trade(self, *, now_ts: float, seconds_remaining: float) -> bool:
        trade = self.current_trade
        if trade is None or trade.result != "PENDING":
            return False
        if self.current_market is None:
            return False
        if not bool(config.trading.live_enable_early_exit):
            return False

        if self.current_market.last_odds_update < now_ts - 1.0:
            await self.poly_client.refresh_odds(self.current_market)

        hold_sec = float(now_ts - float(trade.timestamp or now_ts))
        if hold_sec < float(config.trading.live_early_exit_min_elapsed_sec):
            return False

        token_id, side_bid, _side_ask, opposite_ask = self._resolve_trade_quotes(trade.direction)
        if side_bid is None or token_id is None:
            return False

        exit_px, mtm_pnl, mtm_roi_pct = self._mark_to_market_trade(trade, side_bid)
        if exit_px <= 0.0:
            return False

        window_key = int(self.current_trade_window_start or int(trade.timestamp))
        early_reason: Optional[str] = None
        if (
            opposite_ask is not None
            and float(opposite_ask) >= float(config.trading.live_early_exit_opposite_ask)
            and float(mtm_roi_pct) <= float(config.trading.live_early_exit_opposite_min_loss_roi_pct)
        ):
            hits = int(self._early_exit_opposite_hits.get(window_key, 0)) + 1
            self._early_exit_opposite_hits[window_key] = hits
            if hits >= max(1, int(config.trading.live_early_exit_opposite_confirm_polls)):
                early_reason = (
                    f"opposite_prob_surge(opposite_ask={float(opposite_ask):.3f}"
                    f" >= {float(config.trading.live_early_exit_opposite_ask):.3f},"
                    f" roi={float(mtm_roi_pct):+.2f}% <= {float(config.trading.live_early_exit_opposite_min_loss_roi_pct):+.2f}%,"
                    f" hits={hits})"
                )
        else:
            self._early_exit_opposite_hits.pop(window_key, None)

        signal_conf = float(self.current_trade_signal_confidence or 0.5)
        dynamic_stop_loss_roi = float(config.trading.live_early_exit_stop_loss_roi_pct)
        dynamic_stop_loss_min_hold = max(
            float(config.trading.live_early_exit_min_elapsed_sec),
            float(config.trading.live_early_exit_stop_loss_min_hold_sec),
        )
        if signal_conf >= float(config.trading.live_early_exit_stop_loss_high_conf_cutoff):
            dynamic_stop_loss_min_hold = min(
                dynamic_stop_loss_min_hold,
                max(
                    float(config.trading.live_early_exit_min_elapsed_sec),
                    float(config.trading.live_early_exit_stop_loss_high_conf_min_hold_sec),
                ),
            )
        elif signal_conf <= float(config.trading.live_early_exit_stop_loss_low_conf_cutoff):
            dynamic_stop_loss_roi -= abs(float(config.trading.live_early_exit_stop_loss_low_conf_relax_pct))

        btc_adverse_ok = True
        btc_move_from_entry_pct = None
        if bool(config.trading.live_early_exit_stop_loss_require_btc_adverse):
            btc_entry_px = self.price_feed.get_price_at(float(trade.timestamp or now_ts))
            btc_now_px = float(self.price_feed.current_price or 0.0)
            if btc_entry_px is not None and btc_entry_px > 0.0 and btc_now_px > 0.0:
                btc_move_from_entry_pct = ((btc_now_px - float(btc_entry_px)) / float(btc_entry_px)) * 100.0
                adverse_thr = abs(float(config.trading.live_early_exit_stop_loss_btc_adverse_pct))
                if str(trade.direction).upper() == "UP":
                    btc_adverse_ok = float(btc_move_from_entry_pct) <= -adverse_thr
                else:
                    btc_adverse_ok = float(btc_move_from_entry_pct) >= adverse_thr
            else:
                btc_adverse_ok = False

        if (
            early_reason is None
            and hold_sec >= dynamic_stop_loss_min_hold
            and float(mtm_roi_pct) <= dynamic_stop_loss_roi
            and btc_adverse_ok
        ):
            self._early_exit_opposite_hits.pop(window_key, None)
            btc_move_note = (
                f", btc_entry_move={float(btc_move_from_entry_pct):+.4f}%"
                if btc_move_from_entry_pct is not None
                else ""
            )
            early_reason = (
                f"stop_loss(roi={float(mtm_roi_pct):+.2f}%"
                f" <= {dynamic_stop_loss_roi:+.2f}%"
                f", hold={hold_sec:.1f}s >= {dynamic_stop_loss_min_hold:.1f}s"
                f", conf={signal_conf:.3f}"
                f"{btc_move_note})"
            )
        elif (
            early_reason is None
            and hold_sec >= float(config.trading.live_early_exit_max_hold_sec)
            and float(mtm_roi_pct) <= float(config.trading.live_early_exit_timestop_max_roi_pct)
            and float(seconds_remaining) <= float(config.trading.live_early_exit_timestop_max_remain_sec)
        ):
            self._early_exit_opposite_hits.pop(window_key, None)
            early_reason = (
                f"time_stop(hold={hold_sec:.1f}s, rem={float(seconds_remaining):.1f}s,"
                f" roi={float(mtm_roi_pct):+.2f}% <= {float(config.trading.live_early_exit_timestop_max_roi_pct):+.2f}%)"
            )
        elif (
            early_reason is None
            and bool(config.trading.live_pre_expiry_liquidation_enabled)
            and float(seconds_remaining) <= float(config.trading.live_pre_expiry_liquidation_remain_sec)
            and float(side_bid) >= float(config.trading.live_pre_expiry_liquidation_min_bid)
            and float(mtm_roi_pct) >= float(config.trading.live_pre_expiry_liquidation_min_roi_pct)
        ):
            self._early_exit_opposite_hits.pop(window_key, None)
            early_reason = (
                f"pre_expiry_liquidation(rem={float(seconds_remaining):.1f}s"
                f", bid={float(side_bid):.3f} >= {float(config.trading.live_pre_expiry_liquidation_min_bid):.3f}"
                f", roi={float(mtm_roi_pct):+.2f}% >= {float(config.trading.live_pre_expiry_liquidation_min_roi_pct):+.2f}%)"
            )

        if not early_reason:
            return False

        shares = float(trade.amount / max(float(trade.price), 1e-9))
        exit_result = await self.poly_client.place_exit_order(
            token_id=str(token_id),
            side="SELL",
            shares=float(shares),
            reference_bid=float(exit_px),
        )
        if exit_result is None:
            self._set_kill_switch("early-exit order result missing (possible unknown fill)")
            return False
        if bool(exit_result.get("uncertain_fill", False)):
            self._set_kill_switch(
                f"early-exit uncertain fill: {exit_result.get('reason') or 'unknown'}"
            )
            return False
        if not bool(exit_result.get("accepted", True)):
            logger.warning(
                "Early-exit rejected: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
            return False
        if not bool(exit_result.get("filled", False)):
            logger.warning(
                "Early-exit not filled: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
            return False

        executed_size = float(exit_result.get("executed_size") or 0.0)
        if executed_size > 0.0 and executed_size < (shares * 0.95):
            self._set_kill_switch(
                f"early-exit partial fill too small: {executed_size:.6f}/{shares:.6f} shares"
            )
            return False

        executed_notional = float(exit_result.get("executed_notional") or 0.0)
        executed_price = float(exit_result.get("executed_price") or 0.0)
        if executed_notional <= 0.0 and executed_size > 0.0 and 0.0 < executed_price < 1.0:
            executed_notional = float(executed_size * executed_price)
        if executed_notional <= 0.0:
            executed_notional = float(shares * exit_px)

        realized_pnl = float(apply_fee_to_pnl(executed_notional - float(trade.amount), float(trade.amount)))
        close_reason = (
            f"{early_reason} | exit_px={float(exit_px):.3f}"
            f" fill_px={float(executed_price):.3f}"
            f" fill_notional=${float(executed_notional):.2f}"
        )
        logger.warning(
            "EARLY EXIT ws=%s dir=%s reason=%s pnl=$%+.2f roi=%+.2f%%",
            self.current_trade_window_start,
            trade.direction,
            early_reason,
            realized_pnl,
            (realized_pnl / max(float(trade.amount), 1e-9)) * 100.0,
        )
        self._finalize_trade_close(
            trade=trade,
            pnl=realized_pnl,
            close_reason=close_reason,
            actual_outcome="EARLY_EXIT",
        )
        await self._refresh_adaptive_balance_cap(force=True, reason="post_early_exit")
        return True

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
        logger.info("Position mode: %s | Sizing mode: %s", self.position_mode, self.live_sizing_mode)
        logger.info(
            "Entry execution: mode=%s | timeout=%.2fs | poll=%.2fs | drift_abs=%.4f | drift_ratio=%.2f%%",
            config.trading.entry_order_mode,
            float(config.trading.limit_order_timeout_seconds),
            float(config.trading.order_poll_interval_seconds),
            float(config.trading.max_entry_price_drift_abs),
            float(config.trading.max_entry_price_drift_ratio) * 100.0,
        )
        logger.info(
            "Adaptive sizing: base=%.2f%% min=%.2f%% max=%.2f%% edge_boost=%.3f conf_boost=%.3f",
            float(config.trading.live_adaptive_base_frac) * 100.0,
            float(config.trading.live_adaptive_min_frac) * 100.0,
            float(config.trading.live_adaptive_max_frac) * 100.0,
            float(config.trading.live_adaptive_edge_boost),
            float(config.trading.live_adaptive_conf_boost),
        )
        logger.info(
            "Live profit mode: %s | relax(entry=%.0f%% edge=%.0f%% support=%.0f%%) "
            "kelly=%.2f max_frac=%.2f loss_deboost=%.2f",
            self.live_profit_mode,
            float(config.trading.live_aggressive_entry_relax) * 100.0,
            float(config.trading.live_aggressive_min_edge_relax) * 100.0,
            float(config.trading.live_aggressive_support_relax) * 100.0,
            float(config.trading.live_aggressive_kelly_frac),
            float(config.trading.live_aggressive_max_frac),
            float(config.trading.live_aggressive_loss_deboost),
        )
        logger.info(
            "Live guards: entry_start=%.0fs support>=%.0f%% unanim=%s move>=%.4f%%(lookback=%.0fs) "
            "trend(lookback=%.0fs opp<=%.4f%%) implied(side>=%.2f opp<=%.2f) "
            "contra_gap<=%.3f(ovr p>=%.2f conf>=%.2f) down_block>=%.4f%%",
            float(config.trading.live_entry_start_seconds),
            float(config.trading.live_min_support_ratio) * 100.0,
            config.trading.live_require_unanimous,
            float(config.trading.live_min_recent_move_pct),
            float(config.trading.live_recent_move_lookback_sec),
            float(config.trading.live_trend_align_lookback_sec),
            float(config.trading.live_trend_align_max_opposing_move_pct),
            float(config.trading.live_min_entry_side_implied),
            float(config.trading.live_max_opposite_implied),
            float(config.trading.live_max_contra_gap),
            float(config.trading.live_contra_override_min_model_prob),
            float(config.trading.live_contra_override_min_conf),
            float(config.trading.live_down_above_start_block_pct),
        )
        logger.info(
            "Fast-lane: enabled=%s elapsed=[%.0f, %.0f]s remain>=%.0fs move=[%.4f%%, %.4f%%] "
            "recent>=%.4f%% ask<=%.3f p>=%.3f edge>=%.3f ev>=%.3f%%",
            bool(config.trading.fast_lane_enabled),
            float(config.trading.fast_lane_min_seconds_elapsed),
            float(config.trading.fast_lane_max_seconds_elapsed),
            float(config.trading.fast_lane_min_seconds_remaining),
            float(config.trading.fast_lane_min_move_pct),
            float(config.trading.fast_lane_max_move_pct),
            float(config.trading.fast_lane_min_recent_move_pct),
            float(config.trading.fast_lane_max_entry_price),
            float(config.trading.fast_lane_min_direction_prob),
            float(config.trading.fast_lane_min_prob_edge),
            float(config.trading.fast_lane_min_expected_roi) * 100.0,
        )
        logger.info(
            "Feature feed: lookback=%ss | resample=%ss | max_points=%s",
            int(config.trading.feature_lookback_seconds),
            float(config.trading.feature_resample_seconds),
            int(config.trading.feature_max_points),
        )
        logger.info(
            "Adaptive balance refresh: every %.0fs + post-fill",
            self._balance_refresh_sec,
        )
        logger.info(
            "Live early-exit: enabled=%s min_elapsed=%.0fs opp_ask>=%.2f stop_loss<=%.1f%%",
            bool(config.trading.live_enable_early_exit),
            float(config.trading.live_early_exit_min_elapsed_sec),
            float(config.trading.live_early_exit_opposite_ask),
            float(config.trading.live_early_exit_stop_loss_roi_pct),
        )
        logger.info(
            "Pre-expiry liquidation: enabled=%s remain<=%.0fs bid>=%.2f roi>=%.1f%%",
            bool(config.trading.live_pre_expiry_liquidation_enabled),
            float(config.trading.live_pre_expiry_liquidation_remain_sec),
            float(config.trading.live_pre_expiry_liquidation_min_bid),
            float(config.trading.live_pre_expiry_liquidation_min_roi_pct),
        )
        logger.info(
            "Post-settlement exit: enabled=%s at +%.0fs/+%.0fs bid>=%.2f roi>=%.1f%% (else claim)",
            bool(config.trading.live_settlement_exit_enabled),
            float(config.trading.live_settlement_exit_delay1_sec),
            float(config.trading.live_settlement_exit_delay2_sec),
            float(config.trading.live_settlement_exit_min_bid),
            float(config.trading.live_settlement_exit_min_roi_pct),
        )
        logger.info(
            "Auto-claim: enabled=%s interval=%.0fs (best-effort)",
            bool(config.trading.live_auto_claim_enabled),
            float(config.trading.live_auto_claim_interval_seconds),
        )
        logger.info("=" * 60)

        self._load_runtime_state()
        if self._kill_switch_reason:
            allow_reset = os.getenv("LIVE_KILL_SWITCH_RESET_ON_START", "false").lower() == "true"
            if allow_reset:
                logger.warning(
                    "LIVE_KILL_SWITCH_RESET_ON_START=true -> clearing latched kill-switch: %s",
                    self._kill_switch_reason,
                )
                self._clear_kill_switch()
            else:
                logger.error(
                    "Kill-switch is latched from previous run: %s",
                    self._kill_switch_reason,
                )
                logger.error(
                    "Refusing to start live loop. After manual verification set "
                    "LIVE_KILL_SWITCH_RESET_ON_START=true for one restart."
                )
                self._close_state_conn()
                return

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
            self._close_state_conn()
            return

        logger.info(f"BTC price: ${self.price_feed.current_price:,.2f}")

        # Initialize current 5m market immediately so startup reconcile can run once.
        ts0 = compute_market_timestamps(time.time())
        await self._on_new_market(int(ts0["current"]["start"]), float(ts0["seconds_elapsed"]))
        if not self._running:
            price_task.cancel()
            self._close_state_conn()
            return

        await self._refresh_adaptive_balance_cap(force=True, reason="startup")
        await self._maybe_auto_claim(now_ts=float(time.time()), force=True, reason="startup")

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
            self._persist_runtime_state()
            self._close_state_conn()
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
        if self._kill_switch_reason:
            self._running = False
            return
        now = time.time()
        ts = compute_market_timestamps(now)

        current_start = ts["current"]["start"]
        seconds_elapsed = ts["seconds_elapsed"]
        seconds_remaining = ts["seconds_remaining"]

        # ---- New 5-min window? ----
        if self.current_market is None or self.current_market.start_timestamp != current_start:
            await self._on_new_market(current_start, seconds_elapsed)
            if not self._running:
                return

        if self.current_market is None:
            return

        await self._refresh_adaptive_balance_cap(reason="periodic")
        await self._maybe_run_pending_settlement_exit(now_ts=float(now))
        await self._maybe_auto_claim(now_ts=float(now), reason="periodic")

        # ---- Resolve previous trade ----
        if self.current_trade and self.current_trade.result == "PENDING":
            if self.current_trade.timestamp < (self.current_market.start_timestamp - 10):
                await self._resolve_previous_trade()
                await self._maybe_run_pending_settlement_exit(now_ts=float(now))
                await self._maybe_auto_claim(now_ts=float(now), force=True, reason="post_settlement")
                if self.current_trade and self.current_trade.result == "PENDING":
                    self._set_kill_switch(
                        "Pending trade remained unresolved after rollover check; stopping for safety"
                    )
                    return

        # ---- Manage open trade (early exit) ----
        if self.current_trade and self.current_trade.result == "PENDING":
            await self._maybe_early_exit_open_trade(
                now_ts=float(now),
                seconds_remaining=float(seconds_remaining),
            )
            if self.current_trade and self.current_trade.result == "PENDING":
                return

        # ---- One trade per 5m window ----
        if (
            self._trade_locked_window_start is not None
            and int(self._trade_locked_window_start) == int(current_start)
        ):
            return

        # ---- Timing filters ----
        if seconds_remaining < config.trading.cutoff_before_close_seconds:
            return

        # ---- Risk check ----
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            if "entering cooldown" in str(reason).lower():
                self._persist_runtime_state()
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

        # ---- Fast-lane: Binance lead / Polymarket lag (judge bypass) ----
        fast_signal = self._evaluate_fast_lane_signal(ctx, now)
        if fast_signal is not None:
            fast_direction = str(fast_signal.get("direction", ""))
            if self.position_mode == "UP_ONLY" and fast_direction != "UP":
                fast_signal = None
            elif self.position_mode == "DOWN_ONLY" and fast_direction != "DOWN":
                fast_signal = None

        if fast_signal is not None:
            fast_direction = str(fast_signal["direction"])
            fast_conf = float(fast_signal["confidence"])
            fast_edge = float(fast_signal["prob_edge"])
            fast_price = float(fast_signal["entry_price"])
            fast_p = float(fast_signal["direction_prob"])
            fast_ev = float(fast_signal["expected_roi"])
            fast_move = float(fast_signal["move_pct"])
            fast_recent = float(fast_signal["recent_move_pct"])

            bet_size = self._compute_entry_bet_size(
                fast_conf,
                fast_edge,
                expected_roi=float(fast_ev),
                model_prob=float(fast_p),
                entry_price=float(fast_price),
            )
            if bet_size >= config.trading.min_bet_size:
                if fast_direction == "UP":
                    token_id = self.current_market.up_token_id
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

                if price is None:
                    price = fast_price

                if price is not None and 0.01 < price < 0.99 and token_id:
                    logger.info(
                        ">>> FAST-LANE TRADE: %s | $%.2f @ %.4f | p=%.3f edge=%.3f ev=%+.3f%% | "
                        "move=%+.4f%% recent=%+.4f%%",
                        fast_direction,
                        bet_size,
                        float(price),
                        fast_p,
                        fast_edge,
                        fast_ev * 100.0,
                        fast_move,
                        fast_recent,
                    )
                    result = await self.poly_client.place_entry_order(
                        token_id=token_id,
                        side=fast_direction,
                        amount=bet_size,
                        reference_ask=float(price),
                    )
                    handled = self._handle_order_result(
                        result,
                        direction=fast_direction,
                        fallback_amount=float(bet_size),
                        fallback_price=float(price),
                        source="Fast-lane",
                        signal_confidence=float(fast_conf),
                        signal_reason=str(fast_signal.get("reason") or "fast_lane"),
                    )
                    if handled:
                        await self._refresh_adaptive_balance_cap(force=True, reason="post_fill")
                    if handled or self._kill_switch_reason:
                        return

        # Jury timing floor is separate from fast-lane timing.
        if seconds_elapsed < float(config.trading.live_entry_start_seconds):
            return

        # ---- Jury deliberation ----
        decision = self.jury.deliberate(ctx)

        if decision.direction == "NO_TRADE":
            return

        required_min_edge = float(config.trading.min_edge)
        required_support_ratio = float(config.trading.live_min_support_ratio)
        if self.live_profit_mode == "AGGRESSIVE":
            required_min_edge *= (1.0 - _clamp(float(config.trading.live_aggressive_min_edge_relax), 0.0, 0.60))
            required_support_ratio -= _clamp(float(config.trading.live_aggressive_support_relax), 0.0, 0.25)
        required_min_edge = _clamp(required_min_edge, 0.02, 0.95)
        required_support_ratio = _clamp(required_support_ratio, 0.50, 1.0)

        if decision.avg_confidence < required_min_edge:
            return

        if self.position_mode == "UP_ONLY" and decision.direction != "UP":
            return
        if self.position_mode == "DOWN_ONLY" and decision.direction != "DOWN":
            return

        support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
        support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
        if support_ratio < required_support_ratio:
            return
        if config.trading.live_require_unanimous and not decision.unanimous:
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
        trend_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=now,
            lookback_sec=float(config.trading.live_trend_align_lookback_sec),
        )
        if trend_move is None:
            return
        trend_opp_thr = abs(float(config.trading.live_trend_align_max_opposing_move_pct))
        if decision.direction == "UP" and trend_move < -trend_opp_thr:
            logger.info(
                "Skip live trend-align guard: dir=UP trend_move=%.4f%% < -%.4f%% (lookback=%.0fs)",
                trend_move,
                trend_opp_thr,
                float(config.trading.live_trend_align_lookback_sec),
            )
            return
        if decision.direction == "DOWN" and trend_move > trend_opp_thr:
            logger.info(
                "Skip live trend-align guard: dir=DOWN trend_move=%.4f%% > +%.4f%% (lookback=%.0fs)",
                trend_move,
                trend_opp_thr,
                float(config.trading.live_trend_align_lookback_sec),
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
        if self.live_profit_mode == "AGGRESSIVE":
            relax = _clamp(float(config.trading.live_aggressive_entry_relax), 0.0, 0.60)
            dynamic_min_roi = max(0.0, dynamic_min_roi * (1.0 - relax))

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
        if side_ask is not None and opposite_ask is not None:
            contra_gap = float(opposite_ask) - float(side_ask)
            if contra_gap > float(config.trading.live_max_contra_gap):
                if not (
                    float(gate.model_prob) >= float(config.trading.live_contra_override_min_model_prob)
                    and float(decision.avg_confidence) >= float(config.trading.live_contra_override_min_conf)
                ):
                    logger.info(
                        "Skip live contra-gap guard: dir=%s gap=+%.3f > %.3f (p=%.3f conf=%.3f, need p>=%.3f conf>=%.3f)",
                        decision.direction,
                        contra_gap,
                        float(config.trading.live_max_contra_gap),
                        float(gate.model_prob),
                        float(decision.avg_confidence),
                        float(config.trading.live_contra_override_min_model_prob),
                        float(config.trading.live_contra_override_min_conf),
                    )
                    return
        if gate.expected_roi < dynamic_min_roi:
            logger.info(
                "Skip live dynamic EV guard: net_ev=%+.3f%% < %.3f%%",
                gate.expected_roi * 100.0,
                dynamic_min_roi * 100.0,
            )
            return

        bet_size = self._compute_entry_bet_size(
            decision.avg_confidence,
            decision.max_edge,
            expected_roi=float(gate.expected_roi),
            model_prob=float(gate.model_prob),
            entry_price=float(price),
        )
        if bet_size < config.trading.min_bet_size:
            return

        logger.info(
            f">>> TRADE: {decision.direction} | ${bet_size:.2f} @ {price:.4f} | "
            f"conf={decision.avg_confidence:.3f} | unan={decision.unanimous} | "
            f"net_ev={gate.expected_roi:+.3%} | "
            f"BTC_chg={ctx.current_binance_price - ctx.market_start_price:+.2f} | "
            f"poly_up={ctx.poly_up_price:.3f} poly_down={ctx.poly_down_price:.3f}"
        )
        jury_reason = (
            f"{support_votes}/{len(decision.verdicts)} {decision.direction} votes | "
            f"net_ev={gate.expected_roi:+.3%} >= target={dynamic_min_roi:.3%} ({gate.reason})"
        )

        result = await self.poly_client.place_entry_order(
            token_id=token_id,
            side=decision.direction,
            amount=bet_size,
            reference_ask=float(price),
        )
        handled = self._handle_order_result(
            result,
            direction=decision.direction,
            fallback_amount=float(bet_size),
            fallback_price=float(price),
            source="Jury",
            signal_confidence=float(decision.avg_confidence),
            signal_reason=jury_reason,
        )
        if handled:
            await self._refresh_adaptive_balance_cap(force=True, reason="post_fill")

    async def _on_new_market(self, start_timestamp: int, seconds_elapsed: float):
        if self.current_trade and self.current_trade.result == "PENDING":
            await self._resolve_previous_trade()
        if self.current_trade and self.current_trade.result == "PENDING":
            self._set_kill_switch(
                "Pending trade could not be resolved at market rollover; manual review required"
            )
            return

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
                if not await self._reconcile_exchange_for_current_market("rollover"):
                    return
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
        self.current_trade_window_start = None
        self.current_trade_signal_confidence = None
        self.current_trade_signal_reason = None
        self.current_trade_entry_source = None
        self._trade_locked_window_start = None
        self._early_exit_opposite_hits.clear()
        self._persist_runtime_state()

    async def _resolve_previous_trade(self):
        if not self.current_trade or self.current_trade.result != "PENDING":
            return

        if self.market_start_price and self.price_feed.current_price:
            resolved_trade = self.current_trade
            resolved_market = self.current_market
            went_up = self.price_feed.current_price >= self.market_start_price
            actual_direction = "UP" if went_up else "DOWN"
            won = resolved_trade.direction == actual_direction

            self.risk_mgr.resolve_trade(resolved_trade, won)
            self.recent_results.append(actual_direction)
            if len(self.recent_results) > 50:
                self.recent_results = self.recent_results[-50:]

            logger.info(
                f"Market resolved: {actual_direction} | "
                f"Trade={resolved_trade.direction} -> {'WIN' if won else 'LOSS'}"
            )
            self._schedule_settlement_exit_for_previous_window(
                trade=resolved_trade,
                market=resolved_market,
                won=bool(won),
            )
            self._upsert_live_trade_closed(
                trade=resolved_trade,
                window_start=self.current_trade_window_start,
                actual_outcome=actual_direction,
                close_reason="expiry_settlement",
            )
            self.current_trade = None
            self.current_trade_window_start = None
            self.current_trade_signal_confidence = None
            self.current_trade_signal_reason = None
            self.current_trade_entry_source = None
            self._trade_locked_window_start = None
            self._early_exit_opposite_hits.clear()
            self._persist_runtime_state()
            await self._refresh_adaptive_balance_cap(force=True, reason="post_settlement")

    def _build_context(self, seconds_elapsed: float, seconds_remaining: float) -> Optional[MarketContext]:
        if self.price_feed.current_price is None or self.market_start_price is None:
            return None
        if self.current_market is None:
            return None

        recent = self.price_feed.get_recent_prices(
            int(config.trading.feature_lookback_seconds)
        )
        resampled_prices, resampled_ts = _resample_ticks_fixed_interval(
            recent,
            interval_sec=float(config.trading.feature_resample_seconds),
            max_points=int(config.trading.feature_max_points),
        )
        # Fallback to raw ticks if resampling produced too few points.
        if len(resampled_prices) < 10 or len(resampled_ts) < 10:
            resampled_prices = [float(t.price) for t in recent]
            resampled_ts = [float(t.timestamp) for t in recent]

        return MarketContext(
            current_binance_price=self.price_feed.current_price,
            market_start_price=self.market_start_price,
            recent_prices=resampled_prices,
            recent_timestamps=resampled_ts,
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
