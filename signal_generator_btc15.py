"""
Signal generator for BTC 15-minute Up/Down binary options.

Standalone process that:
  1. Reads BTC prices from btc_ticks table
  2. Reads CLOB odds from poly_odds (slug LIKE 'btc-updown-15m%')
  3. Runs Jury (3 judges) with interval=900s
  4. Runs entry gate evaluation
  5. Computes BB, VWAP, ask drift, btc_still_moving, quality_score
  6. Writes signal_cache_btc15 + signal_cache_log_btc15

Usage:
    python signal_generator_btc15.py
"""
import json
import logging
import math
import os
import sys
import time

from config import config
from market_config import BTC_15M, env
from judges import Jury, MarketContext
from db_config import (
    connect_db,
    execute_write,
    fetch_all,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)

# ---------------------------------------------------------------------------
# Logging -- bot_signal_btc15.log
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_root = logging.getLogger()
_root.setLevel(logging.INFO)

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)

_fh = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_signal_btc15.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)

logger = logging.getLogger("signal_btc15")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET = BTC_15M
INTERVAL = MARKET.interval_seconds  # 900
SLUG_PREFIX = MARKET.slug_prefix     # "btc-updown-15m"
CACHE_TABLE = MARKET.signal_cache_table       # "signal_cache_btc15"
CACHE_LOG_TABLE = MARKET.signal_cache_log_table  # "signal_cache_log_btc15"
PRICE_TABLE = MARKET.price_table     # "btc_ticks"

POLL_INTERVAL = 1.0   # seconds
RECENT_MAX = 1200     # keep up to 1200 recent ticks (~20 min at 1/s)


def _env_float(name: str, default: str) -> float:
    return float(env(MARKET, name, default))


def _env_int(name: str, default: str) -> int:
    return int(env(MARKET, name, default))


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _current_window_start(now: float) -> int:
    """Floor timestamp to 900-second boundary."""
    return int(now // INTERVAL) * INTERVAL


def _load_recent_prices(db, now: float, limit: int = 600):
    """Load recent BTC prices + timestamps + volumes from btc_ticks."""
    rows = fetch_all(
        db,
        "SELECT ts, price, COALESCE(volume, 0) FROM %s WHERE ts > %%s ORDER BY ts ASC LIMIT %%s"
        % PRICE_TABLE,
        (now - 900, limit),
    )
    prices = []
    timestamps = []
    volumes = []
    for r in rows:
        timestamps.append(float(r[0]))
        prices.append(float(r[1]))
        volumes.append(float(r[2]))
    return prices, timestamps, volumes


def _load_clob_odds(db, window_start: int, now: float):
    """Load latest CLOB odds row for the current 15-min window."""
    row = fetch_one_dict(
        db,
        "SELECT up_best_ask, down_best_ask, up_best_bid, down_best_bid, up_mid, down_mid "
        "FROM poly_odds WHERE window_start = %s AND slug LIKE %s "
        "ORDER BY ts DESC LIMIT 1",
        (window_start, SLUG_PREFIX + "%"),
    )
    return row


def _load_window_start_price(db, window_start: int) -> float | None:
    """Get BTC price at window start from btc_ticks."""
    row = fetch_one(
        db,
        "SELECT price FROM %s WHERE ts >= %%s AND ts <= %%s ORDER BY ts ASC LIMIT 1"
        % PRICE_TABLE,
        (window_start, window_start + 5),
    )
    if row:
        return float(row[0])
    # Fallback: most recent price before window start
    row = fetch_one(
        db,
        "SELECT price FROM %s WHERE ts <= %%s ORDER BY ts DESC LIMIT 1" % PRICE_TABLE,
        (window_start,),
    )
    if row:
        return float(row[0])
    return None


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def _compute_bb_pos(prices: list[float]) -> float | None:
    """Bollinger Band position: (price - mean) / (2 * std). ~[-1, +1]."""
    if len(prices) < 30:
        return None
    window = prices[-min(60, len(prices)):]
    mean = sum(window) / len(window)
    std = (sum((p - mean) ** 2 for p in window) / len(window)) ** 0.5
    if std < 0.01:
        return None
    return round((prices[-1] - mean) / (2 * std), 4)


def _compute_vwap_agree(
    prices: list[float],
    volumes: list[float],
    timestamps: list[float],
    window_start_ts: float,
    direction: str,
) -> int | None:
    """VWAP agreement: 1 if price vs VWAP aligns with direction."""
    if len(prices) < 30:
        return None
    sum_pv = 0.0
    sum_v = 0.0
    for i in range(len(timestamps)):
        if timestamps[i] >= window_start_ts and i < len(volumes) and volumes[i] > 0:
            sum_pv += prices[i] * volumes[i]
            sum_v += volumes[i]
    if sum_v <= 0:
        return None
    vwap = sum_pv / sum_v
    above_vwap = prices[-1] > vwap
    if direction == "UP":
        return 1 if above_vwap else 0
    else:
        return 1 if not above_vwap else 0


def _compute_ask_drift(
    direction: str,
    up_ask: float | None,
    dn_ask: float | None,
    initial_up_ask: float | None,
    initial_dn_ask: float | None,
) -> float | None:
    """How much the CLOB ask moved from window start."""
    if up_ask is None or dn_ask is None:
        return None
    if initial_up_ask is None or initial_dn_ask is None:
        return None
    if direction == "UP":
        return round(up_ask - initial_up_ask, 4)
    else:
        return round(dn_ask - initial_dn_ask, 4)


def _compute_btc_still_moving(
    prices: list[float],
    timestamps: list[float],
    direction: str,
    now: float,
    lookback_sec: float = 20.0,
) -> int | None:
    """1 if BTC moved in judge direction over last N seconds."""
    if len(prices) < 10 or len(timestamps) < 10:
        return None
    now_px = prices[-1]
    past_px = None
    for i in range(len(timestamps) - 1, -1, -1):
        if timestamps[i] <= now - lookback_sec:
            past_px = prices[i]
            break
    if past_px is None or past_px <= 0 or now_px is None:
        return None
    if direction == "UP":
        return 1 if now_px > past_px else 0
    else:
        return 1 if now_px < past_px else 0


def _compute_quality_score(
    prices: list[float],
    direction: str,
    up_ask: float | None,
    dn_ask: float | None,
    ask_history: list[float],
) -> float | None:
    """Quality score for dynamic sizing: 0.5 to 2.0."""
    if len(prices) < 30:
        return None
    quality = 1.0

    # Acceleration: velocity in 2nd half vs 1st half of last 30 ticks
    p_end = prices[-1]
    p_mid = prices[-min(15, len(prices) // 2)]
    p_start = prices[-min(30, len(prices))]
    v1 = p_end - p_mid
    v2 = p_mid - p_start
    accel = v1 - v2
    if direction == "DOWN":
        accel = -accel
    if accel >= 18:
        quality += 0.5
    elif accel >= 14:
        quality += 0.25
    elif accel < 10:
        quality -= 0.3

    # CLOB ask stability (last 30 readings)
    if up_ask and dn_ask:
        cur_ask = float(up_ask) if direction == "UP" else float(dn_ask)
        ask_history.append(cur_ask)
        if len(ask_history) > 300:
            del ask_history[:len(ask_history) - 300]
        if len(ask_history) >= 30:
            recent = ask_history[-30:]
            am = sum(recent) / len(recent)
            astd = (sum((a - am) ** 2 for a in recent) / len(recent)) ** 0.5
            if astd < 0.04:
                quality += 0.5
            elif astd < 0.055:
                quality += 0.25
            elif astd > 0.065:
                quality -= 0.3

    return round(max(0.5, min(2.0, quality)), 2)


# ---------------------------------------------------------------------------
# Main signal loop
# ---------------------------------------------------------------------------

class BTC15SignalGenerator:
    """Generates signals for BTC 15-min market by polling DB."""

    def __init__(self):
        self.db = connect_db()
        init_market_schema(self.db)

        jury_threshold = _env_int("JURY_THRESHOLD", "2")
        self._jury = Jury(threshold=jury_threshold)

        self._current_ws: int = 0
        self._window_start_price: float | None = None
        self._window_initial_up_ask: float | None = None
        self._window_initial_dn_ask: float | None = None
        self._ask_history: list[float] = []

        # Direction stability lock
        self._stable_direction: str = ""
        self._stable_until: float = 0.0
        self._stable_ws: int = 0

        # Lag arb lock
        self._lag_arb_lock: dict | None = None

        logger.info("BTC15 signal generator initialized (interval=%ds, jury_threshold=%d)",
                     INTERVAL, jury_threshold)

    def _on_new_window(self, ws: int, now: float):
        """Reset per-window state."""
        self._current_ws = ws
        self._window_start_price = _load_window_start_price(self.db, ws)
        self._window_initial_up_ask = None
        self._window_initial_dn_ask = None
        self._ask_history = []
        self._lag_arb_lock = None

        px_str = "N/A"
        if self._window_start_price:
            px_str = "$%.2f" % self._window_start_price
        logger.info("New 15m window: ws=%d | start_price=%s", ws, px_str)

    def _evaluate_and_cache(self, now: float):
        """Core signal evaluation -- mirrors data_collector._evaluate_and_cache_signal."""
        try:
            ws = _current_window_start(now)

            # Detect new window
            if ws != self._current_ws:
                self._on_new_window(ws, now)

            if not self._window_start_price or self._window_start_price <= 0:
                return

            # Load recent BTC prices from DB
            prices, timestamps, volumes = _load_recent_prices(self.db, now, limit=600)
            if len(prices) < 20:
                return

            btc_price = prices[-1]
            if btc_price <= 0:
                return

            elapsed = max(0.0, now - float(ws))
            remaining = max(0.0, float(ws + INTERVAL) - now)

            # Load CLOB odds for this 15-min window
            odds = _load_clob_odds(self.db, ws, now)
            if not odds:
                return

            up_ask = float(odds["up_best_ask"]) if odds.get("up_best_ask") and 0 < float(odds["up_best_ask"]) < 1 else None
            dn_ask = float(odds["down_best_ask"]) if odds.get("down_best_ask") and 0 < float(odds["down_best_ask"]) < 1 else None
            up_bid = float(odds["up_best_bid"]) if odds.get("up_best_bid") and 0 < float(odds["up_best_bid"]) < 1 else None
            dn_bid = float(odds["down_best_bid"]) if odds.get("down_best_bid") and 0 < float(odds["down_best_bid"]) < 1 else None
            up_mid = float(odds.get("up_mid") or 0.5)
            dn_mid = float(odds.get("down_mid") or 0.5)

            # Build MarketContext for the Jury
            ctx = MarketContext(
                current_binance_price=btc_price,
                market_start_price=self._window_start_price,
                recent_prices=prices[-600:],
                recent_timestamps=timestamps[-600:],
                poly_up_price=up_mid,
                poly_down_price=dn_mid,
                seconds_elapsed=elapsed,
                seconds_remaining=remaining,
                poly_up_bid=up_bid,
                poly_up_ask=up_ask,
                poly_down_bid=dn_bid,
                poly_down_ask=dn_ask,
            )

            # Buy/sell volume ratio (last 60s)
            bs_ratio = None
            try:
                vol_row = fetch_one(
                    self.db,
                    "SELECT SUM(buy_volume), SUM(sell_volume) FROM %s WHERE ts > %%s"
                    % PRICE_TABLE,
                    (now - 60,),
                )
                if vol_row and vol_row[0] is not None and vol_row[1] is not None:
                    bv = float(vol_row[0])
                    sv = float(vol_row[1])
                    if sv > 0:
                        bs_ratio = bv / sv
                    elif bv > 0:
                        bs_ratio = 10.0
                ctx.buy_sell_ratio = bs_ratio
            except Exception:
                pass

            # Run Jury
            decision = self._jury.deliberate(ctx)

            # -- Price guards --
            btc_move_pct = 0.0
            recent_move_pct = None
            trend_move_pct = None
            guards_passed = 0

            if decision.direction in ("UP", "DOWN") and self._window_start_price and self._window_start_price > 0:
                btc_move_pct = ((btc_price - self._window_start_price) / self._window_start_price) * 100.0
                try:
                    from entry_guards import evaluate_market_guards
                    guard = evaluate_market_guards(
                        direction=decision.direction,
                        btc_price=btc_price,
                        start_price=self._window_start_price,
                        up_ask=up_ask,
                        down_ask=dn_ask,
                        elapsed=elapsed,
                        db_conn=self.db,
                        window_start=ws,
                        now_ts=now,
                    )
                    guards_passed = 1 if guard.passed else 0
                except Exception as e:
                    logger.debug("Guards check failed: %s", e)

            # -- Direction stability: once gate_allow=1, hold 5s --
            now_ts = time.time()
            if (self._stable_ws == ws
                    and now_ts < self._stable_until
                    and decision.direction != self._stable_direction
                    and decision.direction in ("UP", "DOWN")):
                decision = type(decision)(
                    final_vote=decision.final_vote,
                    direction=self._stable_direction,
                    avg_confidence=decision.avg_confidence,
                    max_edge=decision.max_edge,
                    verdicts=decision.verdicts,
                    unanimous=decision.unanimous,
                )

            # -- Entry gate --
            gate_allow = 0
            gate_ev = None
            gate_reason = None
            if decision.direction in ("UP", "DOWN") and guards_passed:
                try:
                    from trade_gate import evaluate_entry_gate
                    entry_price = float(dn_ask if decision.direction == "DOWN" else up_ask) if (up_ask and dn_ask) else 0.5

                    down_min = _env_float("DOWN_MIN_ENTRY_PRICE", "0.30")
                    max_ask = _env_float("MAX_ENTRY_PRICE", "0.58")
                    if entry_price < down_min:
                        gate_reason = "cheap_token: ask=%.3f < %.3f" % (entry_price, down_min)
                        raise ValueError(gate_reason)
                    if entry_price > max_ask:
                        gate_reason = "expensive_entry: ask=%.3f > %.3f" % (entry_price, max_ask)
                        raise ValueError(gate_reason)

                    support = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
                    support_ratio = support / max(len(decision.verdicts), 1)

                    gate_result = evaluate_entry_gate(
                        direction=decision.direction,
                        entry_price=entry_price,
                        current_price=btc_price,
                        start_price=self._window_start_price,
                        seconds_elapsed=elapsed,
                        jury_confidence=float(decision.avg_confidence),
                        support_ratio=float(support_ratio),
                        seconds_remaining=remaining,
                        recent_prices=prices[-600:],
                        recent_timestamps=timestamps[-600:],
                        poly_up_ask=up_ask,
                        poly_down_ask=dn_ask,
                    )
                    gate_allow = 1 if gate_result.allow else 0
                    gate_ev = float(gate_result.expected_roi) if gate_result.expected_roi else None
                    gate_reason = str(gate_result.reason or "")[:200]

                    if gate_allow:
                        self._stable_direction = decision.direction
                        self._stable_until = now_ts + 5.0
                        self._stable_ws = ws
                except Exception as ge:
                    logger.debug("Entry gate check failed: %s", ge)

            # -- Lag Arb detection --
            lag_arb_allow = 0
            lag_arb_direction = None
            lag_arb_entry_price = None
            lag_btc_thr = float(os.getenv("LAG_ARB_BTC_THRESHOLD_PCT", "0.04"))
            lag_max_ask = float(os.getenv("LAG_ARB_MAX_ASK", "0.50"))
            if btc_price and self._window_start_price and self._window_start_price > 0 and up_ask and dn_ask:
                lag_move = ((btc_price - self._window_start_price) / self._window_start_price) * 100
                if abs(lag_move) >= lag_btc_thr:
                    lag_dir = "UP" if lag_move > 0 else "DOWN"
                    lag_ask = float(up_ask if lag_dir == "UP" else dn_ask)
                    if 0.01 < lag_ask <= lag_max_ask:
                        lag_arb_allow = 1
                        lag_arb_direction = lag_dir
                        lag_arb_entry_price = lag_ask

            # Lag arb lock: hold for 2s (same window)
            if lag_arb_allow:
                self._lag_arb_lock = {
                    "ts": now, "dir": lag_arb_direction,
                    "ep": lag_arb_entry_price, "ws": ws,
                }
            elif self._lag_arb_lock and self._lag_arb_lock.get("ws") == ws and (now - self._lag_arb_lock["ts"]) < 2.0:
                lag_arb_allow = 1
                lag_arb_direction = self._lag_arb_lock["dir"]
                lag_arb_entry_price = self._lag_arb_lock["ep"]

            # -- Binance-RTDS gap --
            binance_rtds_gap = None

            # -- Pre-compute score signals --
            prev_outcome = None
            odds_velocity = None
            btc_accel_ok = None

            # Previous window outcome (15-min previous = ws - 900)
            try:
                pw = ws - INTERVAL
                pr = fetch_one_dict(
                    self.db,
                    "SELECT actual_outcome FROM market_windows WHERE window_start = %s",
                    (pw,),
                )
                if pr and pr.get("actual_outcome"):
                    prev_outcome = str(pr["actual_outcome"])
            except Exception as e:
                logger.warning("prev_outcome fetch err: %s", e)

            # Odds velocity: current ask vs 30s ago
            try:
                dir_ask = up_ask if decision.direction == "UP" else dn_ask
                entry_ask = float(dir_ask) if dir_ask else 0.5
                eo = fetch_one_dict(
                    self.db,
                    "SELECT up_best_ask, down_best_ask FROM poly_odds "
                    "WHERE window_start = %s AND slug LIKE %s AND ts >= %s AND ts <= %s "
                    "ORDER BY ts ASC LIMIT 1",
                    (ws, SLUG_PREFIX + "%", now - 35, now - 25),
                )
                if eo:
                    if decision.direction == "UP":
                        eep = float(eo.get("up_best_ask") or 0.5)
                    else:
                        eep = float(eo.get("down_best_ask") or 0.5)
                    odds_velocity = round(entry_ask - eep, 6)
            except Exception as e:
                logger.warning("odds_velocity fetch err: %s", e)

            # BTC acceleration
            try:
                if len(prices) >= 15:
                    p1 = prices[-1]
                    p2 = prices[-max(len(prices) // 3, 5)]
                    p3 = prices[-max(2 * len(prices) // 3, 10)]
                    if p3 > 0:
                        v1 = (p1 - p2) / p3 * 100
                        v2 = (p2 - p3) / p3 * 100
                        a = v1 - v2
                        btc_accel_ok = 1 if (
                            (decision.direction == "UP" and a > 0) or
                            (decision.direction == "DOWN" and a < 0)
                        ) else 0
            except Exception as e:
                logger.warning("btc_accel calc err: %s", e)

            # -- Technical indicators --
            bb_pos = _compute_bb_pos(prices)

            vwap_agree = _compute_vwap_agree(
                prices, volumes, timestamps,
                float(ws), decision.direction,
            )

            # Ask drift
            if self._window_initial_up_ask is None and up_ask and float(up_ask) > 0.01:
                self._window_initial_up_ask = float(up_ask)
                self._window_initial_dn_ask = float(dn_ask) if dn_ask else None

            ask_drift = _compute_ask_drift(
                decision.direction, up_ask, dn_ask,
                self._window_initial_up_ask, self._window_initial_dn_ask,
            )

            # BTC still moving
            bsm_lookback = _env_float("BTC_STILL_LOOKBACK", "20")
            btc_still_moving = _compute_btc_still_moving(
                prices, timestamps, decision.direction, now, bsm_lookback,
            )

            # Quality score
            quality_score = _compute_quality_score(
                prices, decision.direction, up_ask, dn_ask, self._ask_history,
            )

            # -- Build judges JSON --
            judges_json = json.dumps([
                {"judge": v.judge_name, "vote": v.vote.value,
                 "confidence": v.confidence, "reason": v.reason}
                for v in decision.verdicts
            ])

            # -- Write signal_cache_btc15 --
            sc_params = (
                now, ws, decision.direction,
                float(decision.avg_confidence), float(decision.max_edge),
                1 if decision.unanimous else 0, judges_json,
                up_ask, dn_ask, btc_price, self._window_start_price,
                elapsed, remaining,
                btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                bs_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                prev_outcome, odds_velocity, btc_accel_ok,
                lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
                bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score,
            )

            execute_write(
                self.db,
                """REPLACE INTO %s
                   (id, ts, window_start, direction, avg_confidence, max_edge,
                    unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                    seconds_elapsed, seconds_remaining,
                    btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                    buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                    prev_outcome, odds_velocity, btc_accel_ok,
                    lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
                    bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score)
                   VALUES (1, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s,
                           %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s,
                           %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s)"""
                % CACHE_TABLE,
                sc_params,
            )

            # Log when gate_allow=1 OR guards_passed=1 OR lag_arb_allow=1
            if gate_allow or guards_passed or lag_arb_allow:
                execute_write(
                    self.db,
                    """INSERT INTO %s
                       (ts, window_start, direction, avg_confidence, max_edge,
                        unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                        seconds_elapsed, seconds_remaining,
                        btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                        buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                        prev_outcome, odds_velocity, btc_accel_ok,
                        lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
                        bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score)
                       VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s,
                               %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s,
                               %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s)"""
                    % CACHE_LOG_TABLE,
                    sc_params,
                )

            self.db.commit()

        except Exception as e:
            logger.warning("Signal cache update failed: %s", e)
            try:
                self.db.rollback()
            except Exception:
                pass

    def run(self):
        """Main polling loop -- 1-second tick."""
        logger.info("Starting BTC 15-min signal generator (poll=%.1fs)", POLL_INTERVAL)

        while True:
            try:
                now = time.time()
                self._evaluate_and_cache(now)
            except KeyboardInterrupt:
                logger.info("Shutting down BTC15 signal generator")
                break
            except Exception as e:
                logger.error("Main loop error: %s", e)
                # Reconnect DB on connection errors
                try:
                    self.db.ping(reconnect=True)
                except Exception:
                    try:
                        self.db = connect_db()
                    except Exception as ce:
                        logger.error("DB reconnect failed: %s", ce)

            time.sleep(POLL_INTERVAL)

        # Cleanup
        try:
            self.db.close()
        except Exception:
            pass


def main():
    gen = BTC15SignalGenerator()
    gen.run()


if __name__ == "__main__":
    main()
