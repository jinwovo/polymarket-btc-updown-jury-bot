"""
Signal generator for ETH 5-minute binary options (Polymarket).

Standalone process that:
  1. Reads ETH price from eth_ticks table
  2. Reads CLOB odds from poly_odds where slug LIKE 'eth-updown-5m%'
  3. Runs Jury (3 judges: Statistical, Arbitrage, Orderbook)
  4. Runs entry_gate evaluation
  5. Computes technical indicators (BB, VWAP, ask drift, quality score)
  6. Writes to signal_cache_eth5 / signal_cache_log_eth5

Usage:
    python signal_generator_eth5.py
"""
import json
import logging
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

from config import config
from market_config import ETH_5M, env
from judges import Jury, MarketContext
from trade_gate import evaluate_entry_gate
from entry_guards import evaluate_market_guards
from db_config import (
    connect_db,
    execute_write,
    fetch_all,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET = ETH_5M
INTERVAL = MARKET.interval_seconds           # 300
PRICE_TABLE = MARKET.price_table             # eth_ticks
SLUG_PREFIX = MARKET.slug_prefix             # eth-updown-5m
SIGNAL_CACHE = MARKET.signal_cache_table     # signal_cache_eth5
SIGNAL_LOG = MARKET.signal_cache_log_table   # signal_cache_log_eth5
ENV_PREFIX = MARKET.env_prefix               # ETH5_

POLL_INTERVAL = 1.0       # seconds between ticks
PRICE_LOOKBACK = 600      # seconds of price history to keep
MIN_PRICES = 20           # minimum ticks before running jury

# ---------------------------------------------------------------------------
# Logging -- ASCII only, file + console
# ---------------------------------------------------------------------------
_log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_datefmt = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("signal_eth5")
logger.setLevel(logging.INFO)
logger.propagate = False  # don't duplicate to root logger

_file_handler = logging.FileHandler("bot_signal_eth5.log", encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(_log_fmt, _log_datefmt))
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.WARNING)
_console_handler.setFormatter(logging.Formatter(_log_fmt, _log_datefmt))
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    """Read float from ETH5_<name>, fallback to PAPER_<name>, then default."""
    return float(env(MARKET, name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(env(MARKET, name, str(default)))


def _env_str(name: str, default: str) -> str:
    return env(MARKET, name, default)


def _window_start_for(ts: float) -> int:
    """Compute the window_start aligned to INTERVAL boundaries."""
    return int(ts // INTERVAL) * INTERVAL


# ---------------------------------------------------------------------------
# Signal Generator
# ---------------------------------------------------------------------------
class ETH5SignalGenerator:
    """Polls DB for ETH prices + CLOB odds, runs jury + gate, writes signal."""

    def __init__(self):
        self.db = connect_db()
        init_market_schema(self.db)

        self._jury = Jury(threshold=_env_int("JURY_THRESHOLD", 2))
        self._running = True

        # Price history ring-buffers
        self._recent_prices: deque[float] = deque(maxlen=6000)
        self._recent_timestamps: deque[float] = deque(maxlen=6000)
        self._recent_volumes: deque[float] = deque(maxlen=6000)

        # Per-window state
        self._current_ws: int = 0
        self._window_start_price: float | None = None
        self._window_initial_up_ask: float | None = None
        self._window_initial_down_ask: float | None = None
        self._ask_history: list[float] = []

        # Direction stability lock
        self._stable_direction: str = ""
        self._stable_until: float = 0.0
        self._stable_ws: int = 0

        # Lag arb lock
        self._lag_arb_lock: dict | None = None

        # Ensure signal cache table exists
        self._ensure_tables()

        logger.info(
            "ETH5 Signal Generator started | interval=%ds slug=%s cache=%s",
            INTERVAL, SLUG_PREFIX, SIGNAL_CACHE,
        )

    # -- Schema ---------------------------------------------------------------
    def _ensure_tables(self):
        """Tables are created by db_config._multi_market_tables, but double-check."""
        for stmt in [
            f"""CREATE TABLE IF NOT EXISTS {SIGNAL_CACHE} (
                id TINYINT PRIMARY KEY DEFAULT 1,
                ts DOUBLE NOT NULL,
                window_start BIGINT NOT NULL,
                direction VARCHAR(16) NOT NULL,
                avg_confidence DOUBLE NOT NULL,
                max_edge DOUBLE NOT NULL,
                unanimous TINYINT NOT NULL DEFAULT 0,
                judges_json LONGTEXT NOT NULL,
                up_ask DOUBLE NULL, down_ask DOUBLE NULL,
                btc_price DOUBLE NULL, start_price DOUBLE NULL,
                seconds_elapsed DOUBLE NULL, seconds_remaining DOUBLE NULL,
                btc_move_pct DOUBLE NULL, recent_move_pct DOUBLE NULL,
                trend_move_pct DOUBLE NULL,
                guards_passed TINYINT NOT NULL DEFAULT 0,
                buy_sell_ratio DOUBLE NULL,
                gate_allow TINYINT NOT NULL DEFAULT 0,
                gate_ev DOUBLE NULL, gate_reason TEXT NULL,
                binance_rtds_gap DOUBLE NULL,
                prev_outcome VARCHAR(16) NULL,
                odds_velocity DOUBLE NULL, btc_accel_ok TINYINT NULL,
                lag_arb_allow TINYINT NOT NULL DEFAULT 0,
                lag_arb_direction VARCHAR(16) NULL,
                lag_arb_entry_price DOUBLE NULL,
                bb_pos DOUBLE NULL, vwap_agree TINYINT NULL,
                ask_drift DOUBLE NULL, btc_still_moving TINYINT NULL,
                quality_score DOUBLE NULL
            ) ENGINE=InnoDB""",
            f"""CREATE TABLE IF NOT EXISTS {SIGNAL_LOG} (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts DOUBLE NOT NULL,
                window_start BIGINT NOT NULL,
                direction VARCHAR(16) NOT NULL,
                avg_confidence DOUBLE NOT NULL,
                max_edge DOUBLE NOT NULL,
                unanimous TINYINT NOT NULL DEFAULT 0,
                judges_json LONGTEXT NOT NULL,
                up_ask DOUBLE NULL, down_ask DOUBLE NULL,
                btc_price DOUBLE NULL, start_price DOUBLE NULL,
                seconds_elapsed DOUBLE NULL, seconds_remaining DOUBLE NULL,
                btc_move_pct DOUBLE NULL, recent_move_pct DOUBLE NULL,
                trend_move_pct DOUBLE NULL,
                guards_passed TINYINT NOT NULL DEFAULT 0,
                buy_sell_ratio DOUBLE NULL,
                gate_allow TINYINT NOT NULL DEFAULT 0,
                gate_ev DOUBLE NULL, gate_reason TEXT NULL,
                binance_rtds_gap DOUBLE NULL,
                prev_outcome VARCHAR(16) NULL,
                odds_velocity DOUBLE NULL, btc_accel_ok TINYINT NULL,
                lag_arb_allow TINYINT NOT NULL DEFAULT 0,
                lag_arb_direction VARCHAR(16) NULL,
                lag_arb_entry_price DOUBLE NULL,
                bb_pos DOUBLE NULL, vwap_agree TINYINT NULL,
                ask_drift DOUBLE NULL, btc_still_moving TINYINT NULL,
                quality_score DOUBLE NULL,
                INDEX idx_scl_eth5_ws_ts (window_start, ts),
                INDEX idx_scl_eth5_ws_gate (window_start, gate_allow)
            ) ENGINE=InnoDB""",
        ]:
            try:
                execute_write(self.db, stmt)
                self.db.commit()
            except Exception:
                pass

    # -- Data loading ---------------------------------------------------------
    def _load_recent_prices(self, now: float):
        """Load recent ETH prices from eth_ticks into memory buffers."""
        cutoff = now - PRICE_LOOKBACK
        rows = fetch_all(
            self.db,
            f"SELECT ts, price, COALESCE(volume, 0), COALESCE(buy_volume, 0), COALESCE(sell_volume, 0) "
            f"FROM {PRICE_TABLE} WHERE ts > %s ORDER BY ts ASC",
            (cutoff,),
        )
        self._recent_prices.clear()
        self._recent_timestamps.clear()
        self._recent_volumes.clear()
        for r in rows:
            self._recent_timestamps.append(float(r[0]))
            self._recent_prices.append(float(r[1]))
            self._recent_volumes.append(float(r[2]))

    def _get_latest_price(self) -> tuple[float | None, float]:
        """Return (price, timestamp) of the latest ETH tick."""
        row = fetch_one(
            self.db,
            f"SELECT price, ts FROM {PRICE_TABLE} ORDER BY ts DESC LIMIT 1",
        )
        if row:
            return float(row[0]), float(row[1])
        return None, 0.0

    def _get_window_start_price(self, ws: int) -> tuple[float | None, bool]:
        """Get the ETH price at window start from market_windows or first tick.
        Returns (price, is_from_market_windows)."""
        # Try market_windows first (PTB = authoritative)
        row = fetch_one_dict(
            self.db,
            "SELECT btc_start_price FROM market_windows WHERE window_start = %s AND slug LIKE 'eth-updown-5m%%'",
            (ws,),
        )
        if row and row.get("btc_start_price") and float(row["btc_start_price"]) > 0:
            return float(row["btc_start_price"]), True
        # Fallback: earliest eth_ticks in this window
        row2 = fetch_one(
            self.db,
            f"SELECT price FROM {PRICE_TABLE} WHERE ts >= %s AND ts < %s ORDER BY ts ASC LIMIT 1",
            (ws, ws + 5),
        )
        if row2:
            return float(row2[0]), False
        return None, False

    def _get_clob_odds(self, ws: int, now: float):
        """Get latest CLOB odds for current ETH 5min window from poly_odds."""
        slug = f"{SLUG_PREFIX}-{ws}"
        row = fetch_one_dict(
            self.db,
            "SELECT up_best_ask, down_best_ask, up_best_bid, down_best_bid, "
            "up_mid, down_mid "
            "FROM poly_odds WHERE window_start = %s AND slug = %s "
            "ORDER BY ts DESC LIMIT 1",
            (ws, slug),
        )
        if not row:
            # Try slug LIKE pattern
            row = fetch_one_dict(
                self.db,
                "SELECT up_best_ask, down_best_ask, up_best_bid, down_best_bid, "
                "up_mid, down_mid "
                "FROM poly_odds WHERE window_start = %s AND slug LIKE %s "
                "ORDER BY ts DESC LIMIT 1",
                (ws, f"{SLUG_PREFIX}%"),
            )
        return row

    def _get_buy_sell_ratio(self, now: float) -> float | None:
        """Buy/sell volume ratio from eth_ticks over last 60s."""
        row = fetch_one(
            self.db,
            f"SELECT SUM(buy_volume), SUM(sell_volume) FROM {PRICE_TABLE} WHERE ts > %s",
            (now - 60,),
        )
        if row and row[0] is not None and row[1] is not None:
            bv = float(row[0])
            sv = float(row[1])
            if sv > 0:
                return bv / sv
            elif bv > 0:
                return 10.0
        return None

    # -- Technical indicators -------------------------------------------------
    def _compute_bb_pos(self, prices: list[float]) -> float | None:
        """Bollinger Band position: (price - mean) / (2 * std)."""
        if len(prices) < 30:
            return None
        window = prices[-min(60, len(prices)):]
        mean = sum(window) / len(window)
        std = (sum((p - mean) ** 2 for p in window) / len(window)) ** 0.5
        if std < 0.01:
            return None
        return round((prices[-1] - mean) / (2 * std), 4)

    def _compute_vwap_agree(
        self, direction: str, prices: list[float], volumes: list[float],
        timestamps: list[float], ws_ts: float,
    ) -> int | None:
        """Whether price is above/below VWAP in direction's favor."""
        if len(prices) < 30:
            return None
        sum_pv = 0.0
        sum_v = 0.0
        for i in range(len(timestamps)):
            if timestamps[i] >= ws_ts and i < len(volumes) and volumes[i] > 0:
                sum_pv += prices[i] * volumes[i]
                sum_v += volumes[i]
        if sum_v <= 0:
            return None
        vwap = sum_pv / sum_v
        above = prices[-1] > vwap
        if direction == "UP":
            return 1 if above else 0
        else:
            return 1 if not above else 0

    def _compute_ask_drift(
        self, direction: str, up_ask: float | None, dn_ask: float | None,
    ) -> float | None:
        """How much CLOB ask moved from window start."""
        if up_ask is None or dn_ask is None:
            return None
        if self._window_initial_up_ask is None and float(up_ask) > 0.01:
            self._window_initial_up_ask = float(up_ask)
            self._window_initial_down_ask = float(dn_ask)
        if self._window_initial_up_ask is None:
            return None
        if direction == "UP":
            return round(float(up_ask) - self._window_initial_up_ask, 4)
        else:
            return round(float(dn_ask) - (self._window_initial_down_ask or 0), 4)

    def _compute_btc_still_moving(
        self, direction: str, now: float,
    ) -> int | None:
        """Is ETH moving in judge direction over last N seconds?"""
        lookback = float(os.getenv(f"{ENV_PREFIX}BTC_STILL_LOOKBACK",
                                   os.getenv("PAPER_BTC_STILL_LOOKBACK", "20")))
        prices = list(self._recent_prices)
        ts_arr = list(self._recent_timestamps)
        if len(prices) < 10 or len(ts_arr) < 10:
            return None
        now_px = prices[-1]
        past_px = None
        for i in range(len(ts_arr) - 1, -1, -1):
            if ts_arr[i] <= now - lookback:
                past_px = prices[i]
                break
        if past_px is None or past_px <= 0:
            return None
        if direction == "UP":
            return 1 if now_px > past_px else 0
        else:
            return 1 if now_px < past_px else 0

    def _compute_btc_accel(self, direction: str) -> int | None:
        """BTC acceleration: comparing velocity in 2 halves of recent price."""
        prices = list(self._recent_prices)
        if len(prices) < 15:
            return None
        p1 = prices[-1]
        p2 = prices[-max(len(prices) // 3, 5)]
        p3 = prices[-max(2 * len(prices) // 3, 10)]
        if p3 <= 0:
            return None
        v1 = (p1 - p2) / p3 * 100
        v2 = (p2 - p3) / p3 * 100
        a = v1 - v2
        if direction == "UP":
            return 1 if a > 0 else 0
        else:
            return 1 if a < 0 else 0

    def _compute_odds_velocity(
        self, direction: str, up_ask: float | None, dn_ask: float | None,
        ws: int, now: float,
    ) -> float | None:
        """Change in directional ask over last ~30s."""
        dir_ask = up_ask if direction == "UP" else dn_ask
        if dir_ask is None:
            return None
        entry_ask = float(dir_ask)
        try:
            row = fetch_one_dict(
                self.db,
                "SELECT up_best_ask, down_best_ask FROM poly_odds "
                "WHERE window_start = %s AND ts >= %s AND ts <= %s "
                "ORDER BY ts ASC LIMIT 1",
                (ws, now - 35, now - 25),
            )
            if row:
                past_ask = (
                    float(row.get("up_best_ask") or 0.5)
                    if direction == "UP"
                    else float(row.get("down_best_ask") or 0.5)
                )
                return round(entry_ask - past_ask, 6)
        except Exception:
            pass
        return None

    def _compute_prev_outcome(self, ws: int) -> str | None:
        """Outcome of previous window."""
        prev_ws = ws - INTERVAL
        try:
            row = fetch_one_dict(
                self.db,
                "SELECT actual_outcome FROM market_windows WHERE window_start = %s AND slug LIKE 'eth-updown-5m%%'",
                (prev_ws,),
            )
            if row and row.get("actual_outcome"):
                return str(row["actual_outcome"])
        except Exception:
            pass
        return None

    def _compute_quality_score(
        self, direction: str, up_ask: float | None, dn_ask: float | None,
    ) -> float | None:
        """Quality score for dynamic sizing (0.5 - 2.0)."""
        prices = list(self._recent_prices)
        if len(prices) < 30:
            return None
        quality = 1.0

        # Acceleration
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

        # CLOB ask stability
        if up_ask and dn_ask:
            cur_ask = float(up_ask) if direction == "UP" else float(dn_ask)
            self._ask_history.append(cur_ask)
            if len(self._ask_history) > 300:
                self._ask_history = self._ask_history[-300:]
            if len(self._ask_history) >= 30:
                recent = self._ask_history[-30:]
                am = sum(recent) / len(recent)
                astd = (sum((a - am) ** 2 for a in recent) / len(recent)) ** 0.5
                if astd < 0.04:
                    quality += 0.5
                elif astd < 0.055:
                    quality += 0.25
                elif astd > 0.065:
                    quality -= 0.3

        return round(max(0.5, min(2.0, quality)), 2)

    # -- Main signal evaluation -----------------------------------------------
    def _evaluate_signal(self, now: float):
        """Run jury + gate + indicators, write to signal_cache_eth5."""
        ws = _window_start_for(now)
        elapsed = max(0.0, now - float(ws))
        remaining = max(0.0, float(ws + INTERVAL) - now)

        # -- New window? Reset per-window state --
        if ws != self._current_ws:
            self._current_ws = ws
            self._window_start_price, self._start_price_from_mw = self._get_window_start_price(ws)
            self._window_initial_up_ask = None
            self._window_initial_down_ask = None
            self._ask_history = []
            self._lag_arb_lock = None
            logger.info(
                "New window: ws=%d elapsed=%.1f start_price=%s (src=%s)",
                ws, elapsed,
                f"${self._window_start_price:.2f}" if self._window_start_price else "N/A",
                "PTB" if self._start_price_from_mw else "eth_ticks",
            )

        # -- Re-check start price from market_windows if we used eth_ticks fallback --
        # data_collector writes PTB after window start; retry until we get it
        if self._window_start_price and not self._start_price_from_mw and elapsed < 30:
            _mw_price, _from_mw = self._get_window_start_price(ws)
            if _from_mw and _mw_price:
                logger.info("Start price updated from PTB: $%.2f -> $%.2f", self._window_start_price, _mw_price)
                self._window_start_price = _mw_price
                self._start_price_from_mw = True

        # -- Load fresh prices --
        self._load_recent_prices(now)
        if len(self._recent_prices) < MIN_PRICES:
            return

        eth_price, _ = self._get_latest_price()
        if eth_price is None or eth_price <= 0:
            return

        start_price = self._window_start_price
        if start_price is None or start_price <= 0:
            # Try to get it again
            start_price = self._get_window_start_price(ws)
            self._window_start_price = start_price
        if start_price is None or start_price <= 0:
            return

        # -- CLOB odds --
        odds = self._get_clob_odds(ws, now)
        up_ask = None
        dn_ask = None
        up_bid = None
        dn_bid = None
        if odds:
            up_ask = float(odds["up_best_ask"]) if odds.get("up_best_ask") else None
            dn_ask = float(odds["down_best_ask"]) if odds.get("down_best_ask") else None
            up_bid = float(odds["up_best_bid"]) if odds.get("up_best_bid") else None
            dn_bid = float(odds["down_best_bid"]) if odds.get("down_best_bid") else None

        # Validate asks
        if up_ask is not None and not (0 < up_ask < 1):
            up_ask = None
        if dn_ask is not None and not (0 < dn_ask < 1):
            dn_ask = None

        # -- Build MarketContext --
        ctx = MarketContext(
            current_binance_price=eth_price,
            market_start_price=start_price,
            recent_prices=list(self._recent_prices)[-600:],
            recent_timestamps=list(self._recent_timestamps)[-600:],
            poly_up_price=up_ask if up_ask else 0.5,
            poly_down_price=dn_ask if dn_ask else 0.5,
            seconds_elapsed=elapsed,
            seconds_remaining=remaining,
            poly_up_bid=up_bid,
            poly_up_ask=up_ask,
            poly_down_bid=dn_bid,
            poly_down_ask=dn_ask,
        )

        # Buy/sell ratio
        bs_ratio = self._get_buy_sell_ratio(now)
        ctx.buy_sell_ratio = bs_ratio

        # -- Jury --
        decision = self._jury.deliberate(ctx)

        # -- Price guards --
        btc_move_pct = 0.0
        recent_move_pct = None
        trend_move_pct = None
        guards_passed = 0

        # Require valid CLOB prices before running guards/gate
        _has_valid_asks = (up_ask is not None and dn_ask is not None
                          and 0.01 < up_ask < 0.99 and 0.01 < dn_ask < 0.99)

        if decision.direction in ("UP", "DOWN") and start_price > 0 and _has_valid_asks:
            btc_move_pct = ((eth_price - start_price) / start_price) * 100.0
            try:
                guard = evaluate_market_guards(
                    direction=decision.direction,
                    btc_price=eth_price,
                    start_price=start_price,
                    up_ask=up_ask,
                    down_ask=dn_ask,
                    elapsed=elapsed,
                    db_conn=self.db,
                    window_start=ws,
                    now_ts=now,
                )
                guards_passed = 1 if guard.passed else 0
            except Exception as e:
                logger.debug("Guards eval failed: %s", e)

        # -- Direction stability: hold direction for 5s after gate_allow --
        now_ts = time.time()
        if (
            self._stable_ws == ws
            and now_ts < self._stable_until
            and decision.direction != self._stable_direction
            and decision.direction in ("UP", "DOWN")
        ):
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
        if decision.direction in ("UP", "DOWN") and _has_valid_asks:
            try:
                entry_price = float(dn_ask if decision.direction == "DOWN" else up_ask)
                # Price range filter
                down_min = _env_float("DOWN_MIN_ENTRY_PRICE", 0.30)
                max_ask = _env_float("MAX_ENTRY_PRICE", 0.70)
                if entry_price < down_min:
                    gate_reason = f"cheap_token: ask={entry_price:.3f} < {down_min:.3f}"
                    raise ValueError(gate_reason)
                if entry_price > max_ask:
                    gate_reason = f"expensive_entry: ask={entry_price:.3f} > {max_ask:.3f}"
                    raise ValueError(gate_reason)

                support = sum(
                    1 for v in decision.verdicts
                    if v.vote.value == decision.direction
                )
                support_ratio = support / max(len(decision.verdicts), 1)
                gate = evaluate_entry_gate(
                    direction=decision.direction,
                    entry_price=entry_price,
                    current_price=eth_price,
                    start_price=start_price,
                    seconds_elapsed=elapsed,
                    jury_confidence=float(decision.avg_confidence),
                    support_ratio=float(support_ratio),
                    seconds_remaining=remaining,
                    recent_prices=list(self._recent_prices)[-600:],
                    recent_timestamps=list(self._recent_timestamps)[-600:],
                    poly_up_ask=up_ask,
                    poly_down_ask=dn_ask,
                )
                gate_allow = 1 if gate.allow else 0
                gate_ev = float(gate.expected_roi) if gate.expected_roi else None
                gate_reason = str(gate.reason or "")[:200]
                # Lock direction on gate pass
                if gate_allow:
                    self._stable_direction = decision.direction
                    self._stable_until = now_ts + 5.0
                    self._stable_ws = ws
            except Exception as ge:
                logger.debug("Entry gate check failed: %s", ge)

        # -- Gate stability: once gate_allow=1, hold for 5s --
        # Also preserve ask prices from the gate moment (prevents NULL ask in held ticks)
        _gs_until = getattr(self, '_gate_stable_until', 0.0)
        _gs_ws = getattr(self, '_gate_stable_ws', 0)
        if gate_allow:
            self._gate_stable_until = now_ts + 5.0
            self._gate_stable_ws = ws
            self._gate_stable_ev = gate_ev
            self._gate_stable_reason = gate_reason
            self._gate_stable_dir = decision.direction
            self._gate_stable_up_ask = up_ask
            self._gate_stable_dn_ask = dn_ask
        elif (_gs_ws == ws
              and now_ts < _gs_until
              and decision.direction == getattr(self, '_gate_stable_dir', '')):
            gate_allow = 1
            gate_ev = getattr(self, '_gate_stable_ev', gate_ev)
            gate_reason = getattr(self, '_gate_stable_reason', gate_reason)
            # Restore ask prices from gate moment if current tick has NULL
            if up_ask is None or dn_ask is None:
                up_ask = getattr(self, '_gate_stable_up_ask', up_ask)
                dn_ask = getattr(self, '_gate_stable_dn_ask', dn_ask)

        # -- Lag Arb detection --
        lag_arb_allow = 0
        lag_arb_direction = None
        lag_arb_entry_price = None
        lag_btc_thr = float(os.getenv("LAG_ARB_BTC_THRESHOLD_PCT", "0.04"))
        lag_max_ask = float(os.getenv("LAG_ARB_MAX_ASK", "0.50"))
        if eth_price and start_price and start_price > 0 and up_ask and dn_ask:
            lag_move = ((eth_price - start_price) / start_price) * 100
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
        elif (
            self._lag_arb_lock
            and self._lag_arb_lock.get("ws") == ws
            and (now - self._lag_arb_lock["ts"]) < 2.0
        ):
            lag_arb_allow = 1
            lag_arb_direction = self._lag_arb_lock["dir"]
            lag_arb_entry_price = self._lag_arb_lock["ep"]

        # -- Technical indicators --
        prices = list(self._recent_prices)
        volumes = list(self._recent_volumes)
        ts_arr = list(self._recent_timestamps)

        prev_outcome = self._compute_prev_outcome(ws)
        odds_velocity = self._compute_odds_velocity(
            decision.direction, up_ask, dn_ask, ws, now,
        )
        btc_accel_ok = self._compute_btc_accel(decision.direction)
        bb_pos = self._compute_bb_pos(prices)
        vwap_agree = self._compute_vwap_agree(
            decision.direction, prices, volumes, ts_arr, float(ws),
        )
        ask_drift = self._compute_ask_drift(decision.direction, up_ask, dn_ask)
        btc_still_moving = self._compute_btc_still_moving(decision.direction, now)
        quality_score = self._compute_quality_score(
            decision.direction, up_ask, dn_ask,
        )

        # -- Binance-RTDS gap (not applicable for ETH, set None) --
        binance_rtds_gap = None

        # -- Write to signal_cache_eth5 --
        judges_json = json.dumps([
            {
                "judge": v.judge_name,
                "vote": v.vote.value,
                "confidence": v.confidence,
                "reason": v.reason,
            }
            for v in decision.verdicts
        ])

        sc_params = (
            now, ws, decision.direction,
            float(decision.avg_confidence), float(decision.max_edge),
            1 if decision.unanimous else 0, judges_json,
            up_ask, dn_ask, eth_price, start_price,
            elapsed, remaining,
            btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
            bs_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
            prev_outcome, odds_velocity, btc_accel_ok,
            lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
            bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score,
        )

        try:
            execute_write(
                self.db,
                f"""REPLACE INTO {SIGNAL_CACHE}
                   (id, ts, window_start, direction, avg_confidence, max_edge,
                    unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                    seconds_elapsed, seconds_remaining,
                    btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                    buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                    prev_outcome, odds_velocity, btc_accel_ok,
                    lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
                    bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score)
                   VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s)""",
                sc_params,
            )
            # Log when gate_allow or guards_passed or lag_arb
            if gate_allow or guards_passed or lag_arb_allow:
                execute_write(
                    self.db,
                    f"""INSERT INTO {SIGNAL_LOG}
                       (ts, window_start, direction, avg_confidence, max_edge,
                        unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                        seconds_elapsed, seconds_remaining,
                        btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                        buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                        prev_outcome, odds_velocity, btc_accel_ok,
                        lag_arb_allow, lag_arb_direction, lag_arb_entry_price,
                        bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s)""",
                    sc_params,
                )
            self.db.commit()
        except Exception as e:
            logger.warning("Signal cache write failed: %s", e)
            try:
                self.db.rollback()
            except Exception:
                pass

        # Log summary
        if gate_allow:
            logger.info(
                "[ETH5] GATE ALLOW | dir=%s ws=%d elapsed=%.0f "
                "eth=$%.2f move=%.4f%% ask=%.3f ev=%.3f%%",
                decision.direction, ws, elapsed, eth_price,
                btc_move_pct, float(up_ask if decision.direction == "UP" else dn_ask or 0),
                (gate_ev or 0) * 100,
            )

    # -- Main loop ------------------------------------------------------------
    def run(self):
        """Poll every POLL_INTERVAL, evaluate signal, write cache."""
        logger.info("Starting ETH5 signal generator main loop (poll=%.1fs)", POLL_INTERVAL)
        while self._running:
            try:
                now = time.time()
                self._evaluate_signal(now)
            except Exception as e:
                logger.error("Signal eval error: %s", e, exc_info=True)
                # Reconnect DB on connection errors
                try:
                    self.db.ping(reconnect=True)
                except Exception:
                    try:
                        self.db = connect_db()
                    except Exception as ce:
                        logger.error("DB reconnect failed: %s", ce)
            time.sleep(POLL_INTERVAL)

    def stop(self):
        self._running = False
        logger.info("ETH5 signal generator stopping")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    gen = ETH5SignalGenerator()

    def _sighandler(signum, frame):
        gen.stop()

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    try:
        gen.run()
    except KeyboardInterrupt:
        gen.stop()
    logger.info("ETH5 signal generator exited")


if __name__ == "__main__":
    main()
