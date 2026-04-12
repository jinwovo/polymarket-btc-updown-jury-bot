"""
Paper trade replay: run EXACT paper_trade_sim logic on historical DB data.

Guarantees backtest = paper by using the SAME code path.
Reads signal_cache_log + btc_ticks + poly_odds from DB.

Usage:
    python paper_replay.py --last-hours 72
    python paper_replay.py --last-hours 24 --equity 100
"""
import argparse
import logging
import os
import sys
import time as _time_mod

# Ensure env is loaded
from config import config
from db_config import (
    connect_db,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
)
from trade_gate import apply_fee_to_pnl
from exit_policy import ExitPolicyConfig, ExitPolicyInput, evaluate_exit_policy
from judges import Jury, MarketContext

# Import paper_trade_sim functions we'll reuse directly
from paper_trade_sim import (
    _paper_exit_policy_config,
    _mark_to_market,
    _safe_prob,
    _recent_price_series,
    _price_at_or_near,
    _compute_bet_size,
)

logger = logging.getLogger("paper_replay")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class ReplayTrade:
    def __init__(self, window_start, direction, entry_price, stake, shares,
                 opened_at, confidence):
        self.window_start = window_start
        self.window_end = window_start + 300
        self.direction = direction
        self.entry_price = entry_price
        self.stake = stake
        self.shares = shares
        self.opened_at = opened_at
        self.confidence = confidence
        self.pnl = 0.0
        self.won = False
        self.close_reason = ""
        self.closed_at = 0.0


class _RamCache:
    """Preload btc_ticks + poly_odds into RAM for fast lookup."""

    def __init__(self, conn, start_ts: float, end_ts: float):
        import bisect as _bisect
        self._bisect = _bisect
        logger.info("Preloading data into RAM...")
        t0 = _time_mod.time()

        cur = conn.cursor()
        cur.execute("SELECT ts, price, volume, buy_volume, sell_volume FROM btc_ticks WHERE ts >= %s AND ts <= %s ORDER BY ts",
                    (int(start_ts) - 600, int(end_ts) + 300))
        rows = cur.fetchall()
        self.btc_ts = [float(r[0]) for r in rows]
        self.btc_px = [float(r[1]) for r in rows]
        self.btc_vol = [float(r[2] or 0) for r in rows]
        self.btc_buy_vol = [float(r[3] or 0) for r in rows]
        self.btc_sell_vol = [float(r[4] or 0) for r in rows]

        cur.execute("SELECT ts, window_start, up_best_bid, up_best_ask, down_best_bid, down_best_ask, up_mid, down_mid "
                    "FROM poly_odds WHERE ts >= %s AND ts <= %s ORDER BY ts",
                    (int(start_ts) - 60, int(end_ts) + 300))
        rows2 = cur.fetchall()
        self.odds_ts = [float(r[0]) for r in rows2]
        self.odds_ws = [int(r[1]) for r in rows2]
        self.odds_data = [{
            "up_best_bid": r[2], "up_best_ask": r[3],
            "down_best_bid": r[4], "down_best_ask": r[5],
            "up_mid": r[6], "down_mid": r[7],
        } for r in rows2]

        # BTC 15-min odds (lazy load - only if needed)
        self.btc15_ts = []
        self.btc15_ua = []
        self.btc15_da = []
        self.eth_ts = []
        self.eth_px = []
        self._btc15_loaded = False
        self._eth_loaded = False
        self._conn_ref = conn
        self._start_ts = start_ts
        self._end_ts = end_ts

        # Preload market_windows for fast outcome lookup
        cur.execute("SELECT window_start, actual_outcome, btc_start_price FROM market_windows WHERE window_start >= %s AND window_start <= %s AND slug LIKE 'btc-updown-5m%%'",
                    (int(start_ts) - 300, int(end_ts) + 300))
        self._mw_map = {}
        for r in cur.fetchall():
            self._mw_map[int(r[0])] = {"actual_outcome": r[1], "btc_start_price": float(r[2]) if r[2] else None}

        t1 = _time_mod.time()
        logger.info("RAM loaded: %d btc_ticks, %d poly_odds, %d market_windows in %.1fs",
                     len(self.btc_ts), len(self.odds_ts), len(self._mw_map), t1 - t0)

    def _ensure_btc15(self):
        if self._btc15_loaded:
            return
        self._btc15_loaded = True
        try:
            cur = self._conn_ref.cursor()
            cur.execute("""SELECT ts, window_start, up_best_ask, down_best_ask
                          FROM poly_odds WHERE slug LIKE 'btc-updown-15m%%'
                          AND ts >= %s AND ts <= %s ORDER BY ts""",
                        (int(self._start_ts) - 900, int(self._end_ts) + 900))
            rows15 = cur.fetchall()
            self.btc15_ts = [float(r[0]) for r in rows15]
            self.btc15_ua = [float(r[2] or 0.5) for r in rows15]
            self.btc15_da = [float(r[3] or 0.5) for r in rows15]
            logger.info("Lazy loaded %d btc15_odds", len(self.btc15_ts))
        except Exception:
            pass

    def _ensure_eth(self):
        if self._eth_loaded:
            return
        self._eth_loaded = True
        try:
            cur = self._conn_ref.cursor()
            cur.execute("SELECT ts, price FROM eth_ticks WHERE ts >= %s AND ts <= %s ORDER BY ts",
                        (int(self._start_ts) - 600, int(self._end_ts) + 300))
            rows_eth = cur.fetchall()
            self.eth_ts = [float(r[0]) for r in rows_eth]
            self.eth_px = [float(r[1]) for r in rows_eth]
            logger.info("Lazy loaded %d eth_ticks", len(self.eth_ts))
        except Exception:
            pass

    def price_at(self, ts: float):
        idx = self._bisect.bisect_right(self.btc_ts, ts) - 1
        if idx < 0:
            return None
        return self.btc_px[idx]

    def prices_range(self, ts_start: float, ts_end: float):
        i0 = self._bisect.bisect_left(self.btc_ts, ts_start)
        i1 = self._bisect.bisect_right(self.btc_ts, ts_end)
        return self.btc_ts[i0:i1], self.btc_px[i0:i1]

    def anchored_vwap(self, ws_start: float, ts: float):
        """VWAP anchored at window start. Returns (vwap, price_vs_vwap)."""
        i0 = self._bisect.bisect_left(self.btc_ts, ws_start)
        i1 = self._bisect.bisect_right(self.btc_ts, ts)
        if i1 - i0 < 5:
            return None, 0.0
        sum_pv = 0.0
        sum_v = 0.0
        for i in range(i0, i1):
            v = self.btc_vol[i]
            if v > 0:
                sum_pv += self.btc_px[i] * v
                sum_v += v
        if sum_v <= 0:
            return None, 0.0
        vwap = sum_pv / sum_v
        cur_price = self.btc_px[i1 - 1]
        return vwap, (cur_price - vwap) / vwap * 100  # pct above/below

    def velocity_consistency(self, ts: float, lookback_sec: float = 30.0):
        """Count how many 1s intervals moved in the same direction over lookback.
        Returns (ratio, dominant_dir): ratio 0-1, 'UP'/'DOWN'."""
        i_end = self._bisect.bisect_right(self.btc_ts, ts) - 1
        i_start = self._bisect.bisect_left(self.btc_ts, ts - lookback_sec)
        if i_end - i_start < 10:
            return 0.5, None
        up_count = 0
        down_count = 0
        for i in range(i_start, i_end):
            diff = self.btc_px[i + 1] - self.btc_px[i]
            if diff > 0:
                up_count += 1
            elif diff < 0:
                down_count += 1
        total = up_count + down_count
        if total == 0:
            return 0.5, None
        if up_count >= down_count:
            return up_count / total, "UP"
        return down_count / total, "DOWN"

    def volume_surge(self, ts: float, short_sec: float = 10.0, long_sec: float = 60.0):
        """Ratio of recent volume (short window) vs baseline (long window).
        Returns surge_ratio (>1 = surge)."""
        i_end = self._bisect.bisect_right(self.btc_ts, ts)
        i_short = self._bisect.bisect_left(self.btc_ts, ts - short_sec)
        i_long = self._bisect.bisect_left(self.btc_ts, ts - long_sec)
        short_vol = sum(self.btc_vol[i_short:i_end])
        long_vol = sum(self.btc_vol[i_long:i_end])
        short_dur = max(ts - (self.btc_ts[i_short] if i_short < len(self.btc_ts) else ts), 1.0)
        long_dur = max(ts - (self.btc_ts[i_long] if i_long < len(self.btc_ts) else ts), 1.0)
        short_rate = short_vol / short_dur
        long_rate = long_vol / long_dur
        if long_rate <= 0:
            return 1.0
        return short_rate / long_rate

    def efficiency_ratio(self, ts_start: float, ts_end: float):
        """Efficiency Ratio: |net move| / sum(|each tick move|). 1.0=straight line, 0=noise."""
        i0 = self._bisect.bisect_left(self.btc_ts, ts_start)
        i1 = self._bisect.bisect_right(self.btc_ts, ts_end)
        if i1 - i0 < 5:
            return None
        net_move = abs(self.btc_px[i1 - 1] - self.btc_px[i0])
        total_path = sum(abs(self.btc_px[i + 1] - self.btc_px[i]) for i in range(i0, i1 - 1))
        if total_path <= 0:
            return None
        return net_move / total_path

    def immediate_momentum(self, ts: float, lookback_sec: float = 10.0):
        """BTC direction in the last N seconds. Returns 'UP'/'DOWN'/None."""
        i1 = self._bisect.bisect_right(self.btc_ts, ts) - 1
        i0 = self._bisect.bisect_left(self.btc_ts, ts - lookback_sec)
        if i1 <= i0 or i0 >= len(self.btc_px):
            return None
        diff = self.btc_px[i1] - self.btc_px[i0]
        if diff > 0:
            return "UP"
        elif diff < 0:
            return "DOWN"
        return None

    def prev_window_move(self, ws: int):
        """BTC move % in previous 5-min window."""
        prev_start = float(ws - 300)
        prev_end = float(ws)
        i0 = self._bisect.bisect_left(self.btc_ts, prev_start)
        i1 = self._bisect.bisect_right(self.btc_ts, prev_end) - 1
        if i1 <= i0 or i0 >= len(self.btc_px):
            return None
        start_px = self.btc_px[i0]
        end_px = self.btc_px[i1]
        if start_px <= 0:
            return None
        return (end_px - start_px) / start_px * 100

    def clob_velocity(self, ws: int, ts: float, direction: str, lookback_sec: float = 30.0):
        """CLOB ask change speed in last N seconds for our direction.
        Returns velocity (positive = moving in our favor)."""
        odds_now = self.odds_at(ws, ts)
        odds_before = self.odds_at(ws, ts - lookback_sec)
        if not odds_now or not odds_before:
            return None
        if direction == "UP":
            a_now = float(odds_now.get("up_best_ask") or 0.5)
            a_before = float(odds_before.get("up_best_ask") or 0.5)
        else:
            a_now = float(odds_now.get("down_best_ask") or 0.5)
            a_before = float(odds_before.get("down_best_ask") or 0.5)
        return a_now - a_before  # positive = our side getting more expensive

    def btc15_trend(self, ts: float):
        """Get BTC 15-min market direction at timestamp.
        Returns (up_ask, down_ask) or (None, None)."""
        self._ensure_btc15()
        if not self.btc15_ts:
            return None, None
        idx = self._bisect.bisect_right(self.btc15_ts, ts) - 1
        if idx < 0:
            return None, None
        return self.btc15_ua[idx], self.btc15_da[idx]

    def eth_move_pct(self, ws_start: float, ts: float):
        """ETH price move % from window start to ts."""
        self._ensure_eth()
        if not self.eth_ts:
            return None
        i0 = self._bisect.bisect_left(self.eth_ts, ws_start)
        i1 = self._bisect.bisect_right(self.eth_ts, ts) - 1
        if i0 >= len(self.eth_ts) or i1 < 0 or i1 <= i0:
            return None
        eth_start = self.eth_px[i0]
        eth_now = self.eth_px[i1]
        if eth_start <= 0:
            return None
        return (eth_now - eth_start) / eth_start * 100

    def cvd_slope(self, ts: float, lookback_sec: float = 60.0):
        """CVD (cumulative volume delta) slope over lookback.
        Returns (slope_direction, cvd_change): 'UP'/'DOWN', float."""
        i_end = self._bisect.bisect_right(self.btc_ts, ts)
        i_start = self._bisect.bisect_left(self.btc_ts, ts - lookback_sec)
        if i_end - i_start < 10:
            return None, 0.0
        # Split into first half and second half
        mid = (i_start + i_end) // 2
        cvd_first = sum(self.btc_buy_vol[i] - self.btc_sell_vol[i] for i in range(i_start, mid))
        cvd_second = sum(self.btc_buy_vol[i] - self.btc_sell_vol[i] for i in range(mid, i_end))
        cvd_change = cvd_second - cvd_first
        if cvd_change > 0:
            return "UP", cvd_change
        elif cvd_change < 0:
            return "DOWN", cvd_change
        return None, 0.0

    def odds_at(self, ws: int, ts: float):
        # Walk backward from bisect point to find matching window_start
        idx = self._bisect.bisect_right(self.odds_ts, ts) - 1
        while idx >= 0:
            if self.odds_ws[idx] == ws:
                return self.odds_data[idx]
            if self.odds_ts[idx] < ts - 300:
                break
            idx -= 1
        return None


class PaperReplay:
    def __init__(self, conn, equity: float = 1000.0, start_ts: float = 0, end_ts: float = 0):
        self.conn = conn
        self.initial_equity = equity
        self.equity = equity
        self.trades: list[ReplayTrade] = []
        self.peak_roi: dict[int, float] = {}
        self.opposite_hits: dict[int, int] = {}
        self.smart_exit_last: dict[int, float] = {}
        self.exit_cfg = _paper_exit_policy_config()
        self.smart_exit_enabled = os.getenv("SMART_EXIT_ENABLED", "true").lower() == "true"
        # RAM cache for fast lookups
        if start_ts > 0 and end_ts > 0:
            self._cache = _RamCache(conn, start_ts, end_ts)
        else:
            self._cache = None

        # Paper entry config
        self.entry_start_sec = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
        self.entry_end_sec = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
        self.down_entry_end_sec = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "200"))
        self.max_entry_price = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.58"))
        self.down_min_entry_price = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.35"))
        self.min_seconds_remaining = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
        self.max_spread = float(os.getenv("PAPER_MAX_ODDS_SPREAD", "0.12"))
        self.drift_max = float(os.getenv("MAX_ENTRY_PRICE_DRIFT_ABS", "0.080"))
        self.taker_fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.03"))
        self.min_edge_filter = float(os.getenv("REPLAY_MIN_EDGE", "0"))
        self.min_conf_filter = float(os.getenv("REPLAY_MIN_CONF", "0"))
        self.min_btc_move = float(os.getenv("REPLAY_MIN_BTC_MOVE", "0"))
        self.max_btc_move = float(os.getenv("REPLAY_MAX_BTC_MOVE", "0"))
        self.require_momentum_agree = os.getenv("REPLAY_REQUIRE_MOMENTUM_AGREE", "0") == "1"
        self.min_score = int(os.getenv("REPLAY_MIN_SCORE", "0"))
        self.no_lag_arb = os.getenv("REPLAY_NO_LAG_ARB", "0") == "1"
        self.bsr_extreme = os.getenv("REPLAY_BSR_EXTREME", "0") == "1"
        self.bsr_lo = float(os.getenv("REPLAY_BSR_LO", "0.5"))   # below this = extreme sell
        self.bsr_hi = float(os.getenv("REPLAY_BSR_HI", "2.0"))   # above this = extreme buy
        self.require_brt = os.getenv("REPLAY_REQUIRE_BRT", "0") == "1"
        self.require_bb_extreme = os.getenv("REPLAY_REQUIRE_BB_EXTREME", "0") == "1"
        self.bb_threshold = float(os.getenv("REPLAY_BB_THRESHOLD", "0.5"))
        self.require_vwap_agree = os.getenv("REPLAY_REQUIRE_VWAP_AGREE", "0") == "1"
        self.require_vel_consistency = float(os.getenv("REPLAY_VEL_CONSISTENCY", "0"))  # 0=off, e.g. 0.6
        self.require_vol_surge = float(os.getenv("REPLAY_VOL_SURGE", "0"))  # 0=off, e.g. 1.5
        self.max_ask_drift = float(os.getenv("REPLAY_MAX_ASK_DRIFT", "0"))  # 0=off, e.g. 0.06
        self.require_btc15_agree = os.getenv("REPLAY_REQUIRE_BTC15_AGREE", "0") == "1"
        self.btc15_min_prob = float(os.getenv("REPLAY_BTC15_MIN_PROB", "0.55"))
        self.require_eth_agree = os.getenv("REPLAY_REQUIRE_ETH_AGREE", "0") == "1"
        self.require_cvd_agree = os.getenv("REPLAY_REQUIRE_CVD_AGREE", "0") == "1"
        self.max_peak_retracement = float(os.getenv("REPLAY_MAX_PEAK_RETRACEMENT", "0"))  # 0=off, e.g. 0.50
        self.min_efficiency_ratio = float(os.getenv("REPLAY_MIN_EFFICIENCY_RATIO", "0"))  # 0=off, e.g. 0.3
        self.require_immediate_momentum = os.getenv("REPLAY_REQUIRE_IMMEDIATE_MOMENTUM", "0") == "1"
        self.prev_window_block_pct = float(os.getenv("REPLAY_PREV_WINDOW_BLOCK_PCT", "0"))  # 0=off, e.g. 0.10
        self.require_btc_still_moving = os.getenv("PAPER_REQUIRE_BTC_STILL_MOVING", "false").lower() == "true"
        self.btc_still_lookback = float(os.getenv("PAPER_BTC_STILL_LOOKBACK", "20"))
        self.min_path_eff = float(os.getenv("REPLAY_MIN_PATH_EFF", "0"))  # 0=off, e.g. 0.15
        self.block_slow_clob = os.getenv("REPLAY_BLOCK_SLOW_CLOB", "0") == "1"
        self.slow_clob_range = (float(os.getenv("REPLAY_SLOW_CLOB_LO", "-0.05")),
                                float(os.getenv("REPLAY_SLOW_CLOB_HI", "0.05")))
        self.clob_exit_enabled = os.getenv("REPLAY_CLOB_EXIT", "0") == "1"
        self.clob_exit_remaining_sec = float(os.getenv("REPLAY_CLOB_EXIT_REMAINING", "10"))  # check at Ns remaining

    def _check_brt(self, ws: int, entry_ts: float, start_price: float) -> bool:
        """Breakout-Retest-Continuation: BTC broke out, retested start, continued."""
        if not self._cache or start_price <= 0:
            return False
        import bisect
        ts_arr = self._cache.btc_ts
        px_arr = self._cache.btc_px
        i0 = bisect.bisect_left(ts_arr, ws + 20)
        i_entry = bisect.bisect_right(ts_arr, entry_ts)
        if i_entry - i0 < 20:
            return False
        bo_found = retest_found = False
        bo_dir = None
        for i in range(i0, i_entry, 3):
            move = (px_arr[i] - start_price) / start_price * 100
            if not bo_found and abs(move) >= 0.02:
                bo_found = True
                bo_dir = "UP" if move > 0 else "DOWN"
            elif bo_found and not retest_found and abs(move) <= 0.01:
                retest_found = True
            elif bo_found and retest_found:
                m2 = (px_arr[i] - start_price) / start_price * 100
                if (bo_dir == "UP" and m2 >= 0.02) or (bo_dir == "DOWN" and m2 <= -0.02):
                    return True
        return False

    def _get_scl_entries(self, ws: int) -> list[dict]:
        """Get signal_cache_log entries for this window."""
        if self.no_lag_arb:
            return fetch_all_dicts(self.conn, """
                SELECT ts, direction, avg_confidence, max_edge, up_ask, down_ask,
                       btc_price, start_price, seconds_elapsed, seconds_remaining,
                       btc_move_pct, buy_sell_ratio,
                       gate_allow, gate_ev, gate_reason,
                       prev_outcome, odds_velocity, btc_accel_ok,
                       lag_arb_allow, lag_arb_direction, lag_arb_entry_price
                FROM signal_cache_log
                WHERE window_start = %s AND gate_allow = 1
                ORDER BY ts ASC
            """, (ws,))
        return fetch_all_dicts(self.conn, """
            SELECT ts, direction, avg_confidence, max_edge, up_ask, down_ask,
                   btc_price, start_price, seconds_elapsed, seconds_remaining,
                   btc_move_pct, buy_sell_ratio,
                   gate_allow, gate_ev, gate_reason,
                   prev_outcome, odds_velocity, btc_accel_ok,
                   lag_arb_allow, lag_arb_direction, lag_arb_entry_price
            FROM signal_cache_log
            WHERE window_start = %s AND (gate_allow = 1 OR lag_arb_allow = 1)
            ORDER BY ts ASC
        """, (ws,))

    def _get_odds_at(self, ws: int, ts: float) -> dict | None:
        """Get latest poly_odds at or before timestamp. Uses RAM cache if available."""
        if self._cache:
            return self._cache.odds_at(ws, ts)
        return fetch_one_dict(self.conn, """
            SELECT up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
            FROM poly_odds
            WHERE window_start = %s AND ts <= %s
            ORDER BY ts DESC LIMIT 1
        """, (ws, ts))

    def _simulate_entry(self, ws: int, scl_entries: list[dict]) -> ReplayTrade | None:
        """Try to enter: scan ALL scl entries, find first that passes all filters."""
        if not scl_entries:
            return None

        # Paper reads signal_cache repeatedly. gate_allow=1 at elapsed=2 gets read
        # later when paper's own elapsed >= 80. So we need to find any gate_allow=1
        # entry, then check if odds at elapsed >= entry_start_sec still work.
        #
        # Strategy: for each gate_allow=1 entry, if its elapsed < entry_start_sec,
        # look ahead in poly_odds at elapsed=entry_start_sec to get actual entry price.

        for entry in scl_entries:
            direction = str(entry["direction"])
            scl_elapsed = float(entry.get("seconds_elapsed") or 0)
            gate_ev = float(entry.get("gate_ev") or 0)
            confidence = float(entry.get("avg_confidence") or 0.5)
            max_edge = float(entry.get("max_edge") or 0.1)
            _is_lag_arb = int(entry.get("lag_arb_allow") or 0) == 1
            _is_gate = int(entry.get("gate_allow") or 0) == 1

            # Lag arb: use lag_arb_direction, skip judge filters
            if _is_lag_arb and not _is_gate:
                _lag_dir = str(entry.get("lag_arb_direction") or "")
                _lag_ep = float(entry.get("lag_arb_entry_price") or 0)
                if _lag_dir in ("UP", "DOWN") and 0.01 < _lag_ep < 0.99:
                    direction = _lag_dir
                    # Use lag_arb entry price, skip all judge filters below
                    entry_ts = float(entry["ts"])
                    stake = float(os.getenv("PAPER_FIXED_STAKE", "0"))
                    if stake <= 0:
                        stake = round(self.initial_equity * 0.15, 2)
                    shares = stake / _lag_ep
                    return ReplayTrade(
                        window_start=ws, direction=direction,
                        entry_price=_lag_ep, stake=stake, shares=shares,
                        opened_at=entry_ts, confidence=confidence,
                    )
                continue

            # Judge entry: apply all filters
            # BSR extreme filter: only enter when volume imbalance is strong
            if self.bsr_extreme:
                _bsr = entry.get("buy_sell_ratio")
                if _bsr is not None:
                    _bsr_f = float(_bsr)
                    if self.bsr_lo <= _bsr_f <= self.bsr_hi:
                        continue  # Skip neutral BSR
                # If BSR is None, skip (can't confirm imbalance)
                elif _bsr is None:
                    continue
            # Edge/confidence quality filters
            if self.min_edge_filter > 0 and max_edge < self.min_edge_filter:
                continue
            if self.min_conf_filter > 0 and confidence < self.min_conf_filter:
                continue
            # BTC move filter: only enter when BTC moved enough in the right direction
            btc_move = float(entry.get("btc_move_pct") or 0)
            if self.min_btc_move > 0:
                if abs(btc_move) < self.min_btc_move:
                    continue
                # Direction must match the move
                if direction == "UP" and btc_move < 0:
                    continue
                if direction == "DOWN" and btc_move > 0:
                    continue
            # Max BTC move filter: skip overextended entries (mean reversion risk)
            if self.max_btc_move > 0 and abs(btc_move) > self.max_btc_move:
                continue

            # Momentum agreement: 30s BTC trend must match bet direction
            if self.require_momentum_agree:
                _check_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
                if self._cache:
                    _btc_30s_price = self._cache.price_at(_check_ts - 30)
                    _prev_tick = {"price": _btc_30s_price} if _btc_30s_price else None
                else:
                    _prev_tick = fetch_one_dict(self.conn, """
                        SELECT price FROM btc_ticks WHERE ts >= %s AND ts <= %s ORDER BY ts DESC LIMIT 1
                    """, (_check_ts - 35, _check_ts - 25))
                if _prev_tick:
                    _btc_now = float(entry.get("btc_price") or 0)
                    _btc_30s = float(_prev_tick.get("price") or 0)
                    if _btc_now > 0 and _btc_30s > 0:
                        _rising = _btc_now > _btc_30s
                        _conflict = (direction == "UP" and not _rising) or (direction == "DOWN" and _rising)
                        if _conflict:
                            continue

            # Score filter: count how many signals are positive (11 signals)
            if self.min_score > 0:
                _prev = str(entry.get("prev_outcome") or "")
                _ov = float(entry.get("odds_velocity") or 0)
                _accel = bool(int(entry.get("btc_accel_ok") or 0)) if entry.get("btc_accel_ok") is not None else False
                _score = 0
                if abs(btc_move) >= 0.02: _score += 1          # 1. BTC moved
                if _prev == direction: _score += 1               # 2. prev won same dir
                _ep = float(entry.get("up_ask") or 0.5) if direction == "UP" else float(entry.get("down_ask") or 0.5)
                if _ep <= 0.45: _score += 1                      # 3. cheap entry
                if gate_ev >= 0.20: _score += 1                  # 4. high EV
                if confidence >= 0.7: _score += 1                # 5. high conf
                if _ov >= 0.02: _score += 1                      # 6. odds velocity
                if _accel: _score += 1                           # 7. BTC accel
                # --- NEW: technical indicator scores ---
                _check_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
                if self._cache:
                    # 8. Anchored VWAP agree
                    _vw, _vw_pct = self._cache.anchored_vwap(float(ws), _check_ts)
                    if _vw is not None:
                        if (direction == "UP" and _vw_pct > 0) or (direction == "DOWN" and _vw_pct < 0):
                            _score += 1
                    # 9. Velocity consistency
                    _vr, _vd = self._cache.velocity_consistency(_check_ts)
                    if _vr >= 0.6 and _vd == direction:
                        _score += 1
                    # 10. Volume surge
                    _vs = self._cache.volume_surge(_check_ts)
                    if _vs >= 1.5:
                        _score += 1
                    # 11. BB extreme
                    import bisect as _sc_bisect
                    _bb_i2 = _sc_bisect.bisect_right(self._cache.btc_ts, _check_ts)
                    _bb_s2 = _sc_bisect.bisect_left(self._cache.btc_ts, _check_ts - 120)
                    if _bb_i2 - _bb_s2 >= 30:
                        _bb_px2 = self._cache.btc_px[_bb_s2:_bb_i2]
                        _bb_w2 = _bb_px2[-min(60, len(_bb_px2)):]
                        _bb_m2 = sum(_bb_w2) / len(_bb_w2)
                        _bb_sd2 = (sum((p - _bb_m2)**2 for p in _bb_w2) / len(_bb_w2)) ** 0.5
                        if _bb_sd2 > 0.01:
                            _bb_p2 = (_bb_px2[-1] - _bb_m2) / (2 * _bb_sd2)
                            if abs(_bb_p2) > 0.5:
                                _score += 1
                if _score < self.min_score:
                    continue

            # Only use gate_allow=1 entries at valid entry time.
            # Early signals (elapsed < entry_start) are stale by the time paper_sim
            # reaches entry_start -- market conditions will have changed. Skip them
            # to match what paper_sim/live actually see in real-time.
            if scl_elapsed < self.entry_start_sec:
                continue
            if True:
                up_ask = float(entry.get("up_ask") or 0.5)
                down_ask = float(entry.get("down_ask") or 0.5)
                elapsed = scl_elapsed
                remaining = float(entry.get("seconds_remaining") or (300 - elapsed))

            # Timing filters
            if elapsed > self.entry_end_sec:
                continue
            if remaining < self.min_seconds_remaining:
                continue
            if direction == "DOWN" and elapsed > self.down_entry_end_sec:
                continue

            entry_price = up_ask if direction == "UP" else down_ask

            if entry_price <= 0.01 or entry_price >= 0.99:
                continue
            if entry_price > self.max_entry_price:
                continue
            if direction == "DOWN" and entry_price < self.down_min_entry_price:
                continue

            # Spread filter
            spread = abs(up_ask - down_ask)
            if spread > self.max_spread:
                continue

            # Score filter — skip if pre-computed signals are NULL (old rebuild data)
            # Paper doesn't have this score filter on entry; it's only for sizing.
            # So don't block entry based on score — just use it for mega sizing later.

            # Drift simulation: check ask after entry (FAK execution window)
            # Rust FAK takes ~300ms. Check 0.2-0.5s after signal for realistic fill.
            # If ask rose past max_entry_price -> FAK limit exceeded -> no fill -> skip
            # If ask rose but still valid -> fill at worse (later) price
            entry_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
            _drift_start = float(os.getenv("REPLAY_DRIFT_START_SEC", "0.2"))
            _drift_end = float(os.getenv("REPLAY_DRIFT_END_SEC", "0.5"))
            _drift_odds = None
            if self._cache:
                _drift_odds = self._cache.odds_at(ws, entry_ts + _drift_end)
            else:
                _drift_rows = fetch_all_dicts(self.conn, """
                    SELECT up_best_ask, down_best_ask FROM poly_odds
                    WHERE window_start = %s AND ts >= %s AND ts <= %s
                    ORDER BY ts ASC LIMIT 1
                """, (ws, entry_ts + _drift_start, entry_ts + _drift_end))
                _drift_odds = _drift_rows[0] if _drift_rows else None
            if _drift_odds:
                later_price = float(_drift_odds.get("up_best_ask") or entry_price) if direction == "UP" \
                    else float(_drift_odds.get("down_best_ask") or entry_price)
                if 0.01 < later_price < 0.99:
                    # Ask rose past max -> FAK limit exceeded, order won't fill
                    if later_price > self.max_entry_price:
                        continue
                    # Ask rose past drift tolerance -> skip
                    if later_price - entry_price > self.drift_max:
                        continue
                    # Use actual fill price (later price = what live would get)
                    entry_price = later_price
            # Re-check price filters after drift adjustment
            if entry_price > self.max_entry_price:
                continue

            # Efficiency Ratio: skip noisy price action (zigzag = coin flip)
            if self.min_efficiency_ratio > 0 and self._cache:
                _er = self._cache.efficiency_ratio(float(ws), entry_ts)
                if _er is not None and _er < self.min_efficiency_ratio:
                    continue

            # Immediate momentum: last 10s must match direction
            if self.require_immediate_momentum and self._cache:
                _im = self._cache.immediate_momentum(entry_ts, 10.0)
                if _im and _im != direction:
                    continue  # price moving wrong way right now

            # Previous window block: skip same-direction bet after large move
            if self.prev_window_block_pct > 0 and self._cache:
                _pw_move = self._cache.prev_window_move(ws)
                if _pw_move is not None:
                    if direction == "UP" and _pw_move > self.prev_window_block_pct:
                        continue  # prev window already UP big, mean-revert likely
                    if direction == "DOWN" and _pw_move < -self.prev_window_block_pct:
                        continue

            # Peak retracement filter: skip when momentum faded from peak
            if self.max_peak_retracement > 0 and self._cache:
                import bisect as _pr_bisect
                _pr_i0 = _pr_bisect.bisect_left(self._cache.btc_ts, float(ws))
                _pr_i1 = _pr_bisect.bisect_right(self._cache.btc_ts, entry_ts)
                if _pr_i1 - _pr_i0 >= 10:
                    _pr_start = self._cache.btc_px[_pr_i0]
                    _pr_now = self._cache.btc_px[_pr_i1 - 1]
                    if _pr_start > 0:
                        _pr_moves = [(self._cache.btc_px[i] - _pr_start) / _pr_start * 100
                                     for i in range(_pr_i0, _pr_i1)]
                        _pr_current = (_pr_now - _pr_start) / _pr_start * 100
                        if direction == "UP":
                            _pr_peak = max(_pr_moves)
                            if _pr_peak > 0.01:  # meaningful peak
                                _pr_retrace = (_pr_peak - _pr_current) / _pr_peak
                                if _pr_retrace > self.max_peak_retracement:
                                    continue  # momentum faded too much
                        else:
                            _pr_peak = min(_pr_moves)
                            if _pr_peak < -0.01:
                                _pr_retrace = (_pr_peak - _pr_current) / _pr_peak
                                if _pr_retrace > self.max_peak_retracement:
                                    continue

            # BTC 15-min trend confirmation
            if self.require_btc15_agree and self._cache:
                _15ua, _15da = self._cache.btc15_trend(entry_ts)
                if _15ua is not None and _15da is not None:
                    if direction == "UP" and _15da > self.btc15_min_prob:
                        continue  # 15min says DOWN strongly, skip UP
                    if direction == "DOWN" and _15ua > self.btc15_min_prob:
                        continue  # 15min says UP strongly, skip DOWN

            # ETH 5-min correlation
            if self.require_eth_agree and self._cache:
                _eth_move = self._cache.eth_move_pct(float(ws), entry_ts)
                if _eth_move is not None:
                    if direction == "UP" and _eth_move < -0.02:
                        continue  # ETH dropping, skip BTC UP
                    if direction == "DOWN" and _eth_move > 0.02:
                        continue  # ETH rising, skip BTC DOWN

            # CVD (Cumulative Volume Delta) agree
            if self.require_cvd_agree and self._cache:
                _cvd_dir, _cvd_val = self._cache.cvd_slope(entry_ts)
                if _cvd_dir and _cvd_dir != direction:
                    continue  # CVD disagrees with direction

            # BTC still moving: last 30s BTC must be moving in our direction
            if self.require_btc_still_moving and self._cache:
                _bsm_p30 = self._cache.price_at(entry_ts - self.btc_still_lookback)
                _bsm_pnow = self._cache.price_at(entry_ts)
                if _bsm_p30 and _bsm_pnow:
                    if direction == "UP" and _bsm_pnow <= _bsm_p30:
                        continue
                    if direction == "DOWN" and _bsm_pnow >= _bsm_p30:
                        continue

            # Path efficiency filter: block very noisy price action (30s before entry)
            if self.min_path_eff > 0 and self._cache:
                import bisect as _pe_bisect
                _pe_i0 = _pe_bisect.bisect_left(self._cache.btc_ts, entry_ts - 30)
                _pe_i1 = _pe_bisect.bisect_right(self._cache.btc_ts, entry_ts)
                if _pe_i1 - _pe_i0 >= 10:
                    _pe_px = self._cache.btc_px[_pe_i0:_pe_i1]
                    _pe_net = abs(_pe_px[-1] - _pe_px[0])
                    _pe_path = sum(abs(_pe_px[i+1] - _pe_px[i]) for i in range(len(_pe_px)-1))
                    _pe_eff = _pe_net / _pe_path if _pe_path > 0 else 0
                    if _pe_eff < self.min_path_eff:
                        continue

            # CLOB velocity filter: block slow/uncertain CLOB movement
            if self.block_slow_clob and self._cache:
                _cv = self._cache.clob_velocity(ws, entry_ts, direction)
                if _cv is not None:
                    if self.slow_clob_range[0] <= _cv <= self.slow_clob_range[1]:
                        continue  # CLOB moving too slowly = uncertain

            # Ask drift filter: skip when CLOB already priced in the move
            if self.max_ask_drift > 0 and self._cache:
                # Get first odds for this window (near window start)
                _first_odds = self._cache.odds_at(ws, float(ws) + 5)
                if _first_odds:
                    _init_ask = float(_first_odds.get("up_best_ask") or 0.5) if direction == "UP" \
                        else float(_first_odds.get("down_best_ask") or 0.5)
                    _ask_drift = entry_price - _init_ask
                    if _ask_drift > self.max_ask_drift:
                        continue  # CLOB already moved, edge gone

            # BB extreme filter: only enter when price is at Bollinger Band extremes
            if self.require_bb_extreme and self._cache:
                import bisect as _bb_bisect
                _bb_ts = self._cache.btc_ts
                _bb_px = self._cache.btc_px
                _bb_i = _bb_bisect.bisect_right(_bb_ts, entry_ts)
                _bb_s = _bb_bisect.bisect_left(_bb_ts, entry_ts - 120)
                if _bb_i - _bb_s >= 30:
                    _bb_prices = _bb_px[_bb_s:_bb_i]
                    _bb_window = _bb_prices[-min(60, len(_bb_prices)):]
                    _bb_mean = sum(_bb_window) / len(_bb_window)
                    _bb_std = (sum((p - _bb_mean)**2 for p in _bb_window) / len(_bb_window)) ** 0.5
                    if _bb_std > 0.01:
                        _bb_pos = (_bb_prices[-1] - _bb_mean) / (2 * _bb_std)
                        if abs(_bb_pos) < self.bb_threshold:  # not extreme = skip
                            continue

            # Anchored VWAP filter: price must be on the same side as direction
            if self.require_vwap_agree and self._cache:
                _vwap, _vwap_pct = self._cache.anchored_vwap(float(ws), entry_ts)
                if _vwap is not None:
                    if direction == "UP" and _vwap_pct <= 0:
                        continue  # price below VWAP, skip UP
                    if direction == "DOWN" and _vwap_pct >= 0:
                        continue  # price above VWAP, skip DOWN

            # Velocity consistency filter: direction must be steady
            if self.require_vel_consistency > 0 and self._cache:
                _vel_ratio, _vel_dir = self._cache.velocity_consistency(entry_ts)
                if _vel_ratio < self.require_vel_consistency:
                    continue  # not consistent enough
                if _vel_dir and _vel_dir != direction:
                    continue  # consistent but wrong direction

            # Volume surge filter: recent volume must exceed baseline
            if self.require_vol_surge > 0 and self._cache:
                _surge = self._cache.volume_surge(entry_ts)
                if _surge < self.require_vol_surge:
                    continue  # not enough volume surge

            # BRT filter: Breakout-Retest-Continuation
            if self.require_brt:
                _start_px = float(entry.get("start_price") or 0)
                if not self._check_brt(ws, entry_ts, _start_px):
                    continue

            # Fixed sizing (matches paper's FIXED mode)
            _fixed_stake = float(os.getenv("PAPER_FIXED_STAKE", "0"))
            if _fixed_stake > 0:
                stake = _fixed_stake
            else:
                stake = round(self.initial_equity * 0.15, 2)

            # Kelly sizing: adjust stake based on conviction score
            if os.getenv("PAPER_KELLY_SIZING", "true").lower() == "true":
                _k_conf = confidence
                _k_move = abs(float(entry.get("btc_move_pct") or 0))
                _k_ua = float(entry.get("up_ask") or 0.5)
                _k_da = float(entry.get("down_ask") or 0.5)
                _k_spread = abs(_k_ua - _k_da)
                _k_score = 0
                if _k_conf >= 0.7: _k_score += 1
                if _k_move >= 0.03: _k_score += 1
                if _k_spread <= 0.10: _k_score += 1
                if entry_price <= 0.48: _k_score += 1
                if _k_move <= 0.10: _k_score += 1
                if _k_score >= 4:
                    stake = round(stake * 2.0, 2)
                elif _k_score >= 3:
                    stake = round(stake * 1.5, 2)
                elif _k_score <= 1:
                    stake = round(stake * 0.5, 2)

            # conf2x sizing: 2x when jury confidence >= 0.7
            _mega_mult = float(os.getenv("PAPER_MEGA_MULTIPLIER", "2.0"))
            _min_score = int(os.getenv("PAPER_MIN_ENTRY_SCORE", "0"))  # 0 = disabled
            if _min_score > 0:
                # Optional score filter (7 signals)
                _btc_move_abs = abs(float(entry.get("btc_move_pct") or 0))
                if _btc_move_abs == 0 and float(entry.get("start_price") or 0) > 0:
                    _btc_move_abs = abs((float(entry.get("btc_price") or 0) - float(entry["start_price"])) / float(entry["start_price"]) * 100)
                _prev_outcome = str(entry.get("prev_outcome") or "")
                if _prev_outcome not in ("UP", "DOWN"):
                    _prev_outcome = None
                _ov = float(entry.get("odds_velocity") or 0)
                _accel_ok = bool(int(entry.get("btc_accel_ok") or 0)) if entry.get("btc_accel_ok") is not None else False
                _score = 0
                if _btc_move_abs >= 0.02: _score += 1
                if _prev_outcome == direction: _score += 1
                if entry_price <= 0.45: _score += 1
                if gate_ev >= 0.20: _score += 1
                if confidence >= 0.7: _score += 1
                if _ov >= 0.02: _score += 1
                if _accel_ok: _score += 1
                if _score < _min_score:
                    continue

            # conf2x: 2x when confidence >= 0.7
            if confidence >= 0.7 and _mega_mult > 1.0:
                stake = round(stake * _mega_mult, 2)

            # Dynamic sizing: quality_score based on accel + CLOB stability
            if os.getenv("PAPER_DYNAMIC_SIZING", "false").lower() == "true" and self._cache:
                import bisect as _qs_bisect
                _qs = 1.0
                _qs_i0 = _qs_bisect.bisect_left(self._cache.btc_ts, entry_ts - 30)
                _qs_i1 = _qs_bisect.bisect_right(self._cache.btc_ts, entry_ts)
                if _qs_i1 - _qs_i0 >= 10:
                    _qs_px = self._cache.btc_px[_qs_i0:_qs_i1]
                    _qs_mid = len(_qs_px) // 2
                    _qs_v1 = _qs_px[-1] - _qs_px[_qs_mid]
                    _qs_v2 = _qs_px[_qs_mid] - _qs_px[0]
                    _qs_accel = _qs_v1 - _qs_v2
                    if direction == "DOWN": _qs_accel = -_qs_accel
                    if _qs_accel >= 18: _qs += 0.5
                    elif _qs_accel >= 14: _qs += 0.25
                    elif _qs_accel < 10: _qs -= 0.3
                # CLOB ask stability
                _qs_asks = []
                for _qs_sec in range(0, 30, 3):
                    _qs_o = self._cache.odds_at(ws, entry_ts - _qs_sec)
                    if _qs_o:
                        _qs_a = float(_qs_o.get("up_best_ask") or 0.5) if direction == "UP" \
                            else float(_qs_o.get("down_best_ask") or 0.5)
                        _qs_asks.append(_qs_a)
                if len(_qs_asks) >= 5:
                    _qs_m = sum(_qs_asks) / len(_qs_asks)
                    _qs_std = (sum((a - _qs_m)**2 for a in _qs_asks) / len(_qs_asks))**0.5
                    if _qs_std < 0.04: _qs += 0.5
                    elif _qs_std < 0.055: _qs += 0.25
                    elif _qs_std > 0.065: _qs -= 0.3
                _qs = max(0.5, min(2.0, _qs))
                stake = round(stake * _qs, 2)

            shares = stake / entry_price
            opened_at = entry_ts

            return ReplayTrade(
                window_start=ws,
                direction=direction,
                entry_price=entry_price,
                stake=stake,
                shares=shares,
                opened_at=opened_at,
                confidence=confidence,
            )

        # No valid entry found in any scl entry
        return None

    def _simulate_exit(self, trade: ReplayTrade, outcome: str | None) -> None:
        """Run the SAME exit logic as paper_trade_sim.resolve_open_trades()."""
        ws = trade.window_start
        we = trade.window_end
        direction = trade.direction
        stake = trade.stake
        shares = trade.shares
        entry_price = trade.entry_price
        opened_at = trade.opened_at

        # Check every second from entry to window end
        check_interval = 1.0
        t = opened_at + max(self.exit_cfg.min_elapsed_sec, 1.0)

        while t <= we:
            hold_sec = t - opened_at
            seconds_elapsed = t - ws
            remaining_sec = max(0.0, we - t)

            # 1) Settlement check
            if t >= we and outcome in ("UP", "DOWN"):
                won = (outcome == direction)
                if won:
                    raw_pnl = shares - stake
                    trade.pnl = apply_fee_to_pnl(raw_pnl, stake)
                else:
                    trade.pnl = -stake
                trade.won = won
                trade.close_reason = "expiry_settlement"
                trade.closed_at = t
                return

            # 2) Get odds for mark-to-market
            odds_row = self._get_odds_at(ws, t)
            if not odds_row:
                t += check_interval
                continue

            _exit_px, _value, mtm_pnl, mtm_roi_pct = _mark_to_market(
                direction=direction, stake=stake, shares=shares, odds_row=odds_row,
            )
            up_ask = _safe_prob(odds_row.get("up_best_ask")) or _safe_prob(odds_row.get("up_mid"))
            down_ask = _safe_prob(odds_row.get("down_best_ask")) or _safe_prob(odds_row.get("down_mid"))
            opposite_ask = up_ask if direction == "DOWN" else down_ask

            # BTC price (RAM cache for speed)
            if self._cache:
                current_btc = self._cache.price_at(t) or 0.0
                _btc_entry_val = self._cache.price_at(opened_at) or current_btc
                _mw = self._cache._mw_map.get(ws)
                start_btc = float(_mw["btc_start_price"]) if _mw and _mw.get("btc_start_price") else current_btc
            else:
                btc_now = _price_at_or_near(self.conn, t, prefer_before=True)
                _btc_entry_val = _price_at_or_near(self.conn, opened_at, prefer_before=True)
                current_btc = float(btc_now) if btc_now else 0.0
                _mw_row = fetch_one_dict(self.conn,
                    "SELECT btc_start_price FROM market_windows WHERE window_start = %s AND slug LIKE 'btc-updown-5m%%'", (ws,))
                start_btc = float(_mw_row.get("btc_start_price") or current_btc) if _mw_row and current_btc > 0 else current_btc

            btc_move_entry = None
            btc_adverse_ok = True
            if _btc_entry_val and current_btc and float(_btc_entry_val) > 0:
                btc_move_entry = ((float(current_btc) - float(_btc_entry_val)) / float(_btc_entry_val)) * 100.0
                if self.exit_cfg.stop_loss_require_btc_adverse:
                    thr = abs(float(self.exit_cfg.stop_loss_btc_adverse_pct))
                    if direction == "UP":
                        btc_adverse_ok = float(btc_move_entry) <= -thr
                    else:
                        btc_adverse_ok = float(btc_move_entry) >= thr

            if self._cache:
                _rts, _rpx = self._cache.prices_range(t - 180, t)
                recent_ts, recent_prices = _rts, _rpx
                if _rpx:
                    current_btc = float(_rpx[-1])
            else:
                recent_ts, recent_prices = _recent_price_series(self.conn, t, lookback_sec=180.0)
                if recent_prices:
                    current_btc = float(recent_prices[-1])

            trade_key = ws
            peak = max(float(self.peak_roi.get(trade_key, -999.0)), float(mtm_roi_pct))
            self.peak_roi[trade_key] = peak

            # CLOB mismatch exit: BTC favors us but CLOB strongly disagrees
            # Pattern: UP hold + BTC above start + CLOB DOWN > 0.70 = smart money says reversal
            if self.clob_exit_enabled and remaining_sec <= self.clob_exit_remaining_sec and remaining_sec > 0:
                _opp_ask = down_ask if direction == "UP" else up_ask
                _opp_threshold = float(os.getenv("REPLAY_CLOB_EXIT_OPP_THRESHOLD", "0.65"))
                if _opp_ask is not None and _opp_ask >= _opp_threshold and current_btc > 0 and start_btc > 0:
                    _btc_favors_us = (direction == "UP" and current_btc > start_btc) or \
                                     (direction == "DOWN" and current_btc < start_btc)
                    if _btc_favors_us:
                        # BTC says we should win, but CLOB strongly disagrees -> exit
                        _our_bid = float(odds_row.get("up_best_bid") or 0) if direction == "UP" \
                            else float(odds_row.get("down_best_bid") or 0)
                        if _our_bid > 0:
                            _sell_pnl = _our_bid * shares - stake
                            trade.pnl = apply_fee_to_pnl(_sell_pnl, stake) if _sell_pnl > 0 else _sell_pnl
                            trade.won = trade.pnl > 0
                            trade.close_reason = f"clob_mismatch_exit@{remaining_sec:.0f}s(opp={_opp_ask:.2f},btc_favors=True)"
                            trade.closed_at = t
                            return

            # 3) Exit policy
            exit_decision = evaluate_exit_policy(
                ExitPolicyInput(
                    direction=direction,
                    hold_sec=float(hold_sec),
                    seconds_elapsed=float(seconds_elapsed),
                    seconds_remaining=float(remaining_sec),
                    signal_confidence=float(trade.confidence),
                    mtm_roi_pct=float(mtm_roi_pct),
                    current_price=float(current_btc),
                    start_price=float(start_btc),
                    peak_roi_pct=float(peak),
                    opposite_ask=float(opposite_ask) if opposite_ask is not None else None,
                    recent_prices=list(recent_prices),
                    recent_timestamps=list(recent_ts),
                    btc_adverse_ok=bool(btc_adverse_ok),
                    btc_move_from_entry_pct=float(btc_move_entry) if btc_move_entry is not None else None,
                    opposite_hits=int(self.opposite_hits.get(trade_key, 0)),
                ),
                self.exit_cfg,
            )
            if exit_decision.opposite_hits > 0:
                self.opposite_hits[trade_key] = int(exit_decision.opposite_hits)
            else:
                self.opposite_hits.pop(trade_key, None)
            early_reason = exit_decision.reason

            # 4) Smart exit
            if (
                early_reason is None
                and self.smart_exit_enabled
                and hold_sec >= float(config.trading.smart_exit_min_hold_sec)
                and float(mtm_roi_pct) >= float(config.trading.smart_exit_min_roi_pct)
            ):
                _se_interval = float(config.trading.smart_exit_interval_sec)
                _se_last = self.smart_exit_last.get(trade_key, 0.0)
                if (t - _se_last) >= _se_interval:
                    self.smart_exit_last[trade_key] = t
                    try:
                        _se_jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))
                        _se_prices, _se_ts = _recent_price_series(self.conn, t, lookback_sec=600.0)
                        if len(_se_prices) >= 20 and start_btc > 0:
                            _se_up = float(up_ask) if up_ask else 0.5
                            _se_dn = float(down_ask) if down_ask else 0.5
                            _se_ctx = MarketContext(
                                current_binance_price=float(current_btc),
                                market_start_price=float(start_btc),
                                recent_prices=list(_se_prices[-600:]),
                                recent_timestamps=list(_se_ts[-600:]),
                                poly_up_price=_se_up,
                                poly_down_price=_se_dn,
                                seconds_elapsed=float(seconds_elapsed),
                                seconds_remaining=float(remaining_sec),
                                poly_up_ask=_se_up,
                                poly_down_ask=_se_dn,
                            )
                            _se_decision = _se_jury.deliberate(_se_ctx)
                            if _se_decision.direction != direction and _se_decision.direction != "NO_TRADE":
                                early_reason = (
                                    f"smart_exit_jury_flip(flip={_se_decision.direction}"
                                    f", hold={hold_sec:.0f}s, roi={mtm_roi_pct:+.1f}%"
                                    f", opp_ask={float(opposite_ask or 0):.3f})"
                                )
                            elif _se_decision.direction == "NO_TRADE" and float(mtm_roi_pct) < -30.0:
                                early_reason = (
                                    f"smart_exit_no_trade(hold={hold_sec:.0f}s"
                                    f", roi={mtm_roi_pct:+.1f}%"
                                    f", opp_ask={float(opposite_ask or 0):.3f})"
                                )
                    except Exception:
                        pass

            if early_reason:
                _exit_px2, _, pnl2, roi2 = _mark_to_market(
                    direction=direction, stake=stake, shares=shares, odds_row=odds_row,
                )
                trade.pnl = pnl2 - (stake * self.taker_fee_rate)  # taker fee on exit
                trade.won = pnl2 > 0
                trade.close_reason = early_reason
                trade.closed_at = t
                self.peak_roi.pop(trade_key, None)
                self.opposite_hits.pop(trade_key, None)
                self.smart_exit_last.pop(trade_key, None)
                return

            t += check_interval

        # If we get here, hold to settlement
        if outcome in ("UP", "DOWN"):
            won = (outcome == direction)
            if won:
                raw_pnl = shares - stake
                trade.pnl = apply_fee_to_pnl(raw_pnl, stake) - (stake * self.taker_fee_rate)
            else:
                trade.pnl = -stake  # no additional fee on total loss
            trade.won = won
            trade.close_reason = "expiry_settlement"
            trade.closed_at = we

    def run(self, start_ts: float, end_ts: float) -> list[ReplayTrade]:
        """Replay paper trades over a time range."""
        # Get all resolved windows in range
        windows = fetch_all_dicts(self.conn, """
            SELECT window_start, actual_outcome
            FROM market_windows
            WHERE window_start >= %s AND window_start <= %s
              AND actual_outcome IN ('UP', 'DOWN')
            ORDER BY window_start ASC
        """, (int(start_ts), int(end_ts)))

        total = len(windows)
        logger.info("Replaying %d windows (%.1f hours)", total, (end_ts - start_ts) / 3600)

        for i, w in enumerate(windows):
            ws = int(w["window_start"])
            outcome = w["actual_outcome"]

            # Get signal_cache_log entries for this window
            scl_entries = self._get_scl_entries(ws)
            if not scl_entries:
                continue  # No gate_allow=1 for this window

            # Try entry
            trade = self._simulate_entry(ws, scl_entries)
            if trade is None:
                continue

            # Check equity
            if trade.stake > self.equity:
                trade.stake = max(5.0, self.equity)
                trade.shares = trade.stake / trade.entry_price

            # Simulate exit
            self._simulate_exit(trade, outcome)

            # Update equity
            self.equity += trade.pnl
            self.trades.append(trade)

            if (i + 1) % 50 == 0:
                logger.info("  [%d/%d] trades=%d PnL=$%+.2f equity=$%.0f",
                            i + 1, total, len(self.trades),
                            sum(t.pnl for t in self.trades), self.equity)

        return self.trades


def main():
    parser = argparse.ArgumentParser(description="Paper trade replay (exact parity)")
    parser.add_argument("--last-hours", type=float, required=True)
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--entry-start", type=float, default=None, help="Override PAPER_ENTRY_START_SEC")
    parser.add_argument("--max-ask", type=float, default=None, help="Override PAPER_MAX_ENTRY_PRICE")
    parser.add_argument("--min-roi", type=float, default=None, help="Override MIN_EXPECTED_ROI (gate level)")
    parser.add_argument("--boundary", type=float, default=None, help="Override PAPER_MIN_BOUNDARY_DIST_PCT")
    parser.add_argument("--max-spread", type=float, default=None, help="Override PAPER_MAX_ODDS_SPREAD")
    parser.add_argument("--mega-mult", type=float, default=None, help="Override PAPER_MEGA_MULTIPLIER")
    parser.add_argument("--stake", type=float, default=None, help="Override PAPER_FIXED_STAKE")
    parser.add_argument("--min-edge", type=float, default=None, help="Min max_edge to enter (filters noisy signals)")
    parser.add_argument("--min-conf", type=float, default=None, help="Min avg_confidence to enter")
    parser.add_argument("--min-btc-move", type=float, default=None, help="Min abs(btc_move_pct) + direction match")
    parser.add_argument("--max-btc-move", type=float, default=None, help="Max abs(btc_move_pct) — skip overextended")
    parser.add_argument("--require-momentum-agree", action="store_true", help="Skip when 30s BTC trend conflicts with bet direction")
    parser.add_argument("--min-score", type=int, default=None, help="Min signal score to enter")
    parser.add_argument("--no-lag-arb", action="store_true", help="Disable lag_arb entries (gate_allow only)")
    parser.add_argument("--bsr-extreme", action="store_true", help="Only enter when BSR is extreme (<0.5 or >2.0)")
    parser.add_argument("--bsr-lo", type=float, default=None, help="BSR low threshold (default 0.5)")
    parser.add_argument("--bsr-hi", type=float, default=None, help="BSR high threshold (default 2.0)")
    parser.add_argument("--require-brt", action="store_true", help="Only enter on Breakout-Retest-Continuation pattern")
    parser.add_argument("--require-bb-extreme", action="store_true", help="Only enter when BB position is extreme (|bb|>threshold)")
    parser.add_argument("--bb-threshold", type=float, default=None, help="BB extreme threshold (default 0.5)")
    parser.add_argument("--require-vwap-agree", action="store_true", help="Only enter when price vs VWAP agrees with direction")
    parser.add_argument("--vel-consistency", type=float, default=None, help="Min velocity consistency ratio (e.g. 0.6)")
    parser.add_argument("--vol-surge", type=float, default=None, help="Min volume surge ratio (e.g. 1.5)")
    parser.add_argument("--max-ask-drift", type=float, default=None, help="Max ask drift from window start (e.g. 0.06)")
    parser.add_argument("--require-btc15-agree", action="store_true", help="Skip when BTC 15min trend disagrees")
    parser.add_argument("--btc15-min-prob", type=float, default=None, help="BTC15 opposing prob threshold (default 0.55)")
    parser.add_argument("--require-eth-agree", action="store_true", help="Skip when ETH 5min moves opposite")
    parser.add_argument("--require-cvd-agree", action="store_true", help="Skip when CVD slope disagrees")
    parser.add_argument("--max-peak-retracement", type=float, default=None, help="Max peak retracement ratio (e.g. 0.50 = 50%%)")
    parser.add_argument("--min-efficiency-ratio", type=float, default=None, help="Min ER to enter (e.g. 0.3)")
    parser.add_argument("--require-immediate-momentum", action="store_true", help="Last 10s BTC must match direction")
    parser.add_argument("--prev-window-block", type=float, default=None, help="Block same-dir after prev window moved X%% (e.g. 0.10)")
    parser.add_argument("--min-path-eff", type=float, default=None, help="Min price path efficiency in 30s before entry (e.g. 0.15)")
    parser.add_argument("--block-slow-clob", action="store_true", help="Block entry when CLOB velocity is slow")
    parser.add_argument("--slow-clob-lo", type=float, default=None, help="Slow CLOB velocity lower bound (default -0.05)")
    parser.add_argument("--slow-clob-hi", type=float, default=None, help="Slow CLOB velocity upper bound (default 0.05)")
    parser.add_argument("--clob-exit", action="store_true", help="Enable CLOB exit near expiry")
    parser.add_argument("--clob-exit-remaining", type=float, default=None, help="Seconds remaining to check CLOB exit (default 10)")
    parser.add_argument("--clob-exit-opp-threshold", type=float, default=None, help="Opposing ask threshold for CLOB exit (default 0.65)")
    parser.add_argument("--btc-still-lookback", type=float, default=None, help="BTC still moving lookback seconds (default 30)")
    args = parser.parse_args()

    # CLI overrides (bypass config.py load_dotenv override=True)
    if args.entry_start is not None:
        os.environ["PAPER_ENTRY_START_SEC"] = str(args.entry_start)
    if args.max_ask is not None:
        os.environ["PAPER_MAX_ENTRY_PRICE"] = str(args.max_ask)
    if args.min_roi is not None:
        os.environ["MIN_EXPECTED_ROI"] = str(args.min_roi)
    if args.boundary is not None:
        os.environ["PAPER_MIN_BOUNDARY_DIST_PCT"] = str(args.boundary)
    if args.max_spread is not None:
        os.environ["PAPER_MAX_ODDS_SPREAD"] = str(args.max_spread)
    if args.mega_mult is not None:
        os.environ["PAPER_MEGA_MULTIPLIER"] = str(args.mega_mult)
    if args.stake is not None:
        os.environ["PAPER_FIXED_STAKE"] = str(args.stake)
    if args.min_edge is not None:
        os.environ["REPLAY_MIN_EDGE"] = str(args.min_edge)
    if args.min_conf is not None:
        os.environ["REPLAY_MIN_CONF"] = str(args.min_conf)
    if args.min_btc_move is not None:
        os.environ["REPLAY_MIN_BTC_MOVE"] = str(args.min_btc_move)
    if args.max_btc_move is not None:
        os.environ["REPLAY_MAX_BTC_MOVE"] = str(args.max_btc_move)
    if args.require_momentum_agree:
        os.environ["REPLAY_REQUIRE_MOMENTUM_AGREE"] = "1"
    if args.min_score is not None:
        os.environ["REPLAY_MIN_SCORE"] = str(args.min_score)
    if args.no_lag_arb:
        os.environ["REPLAY_NO_LAG_ARB"] = "1"
    if args.bsr_extreme:
        os.environ["REPLAY_BSR_EXTREME"] = "1"
    if args.bsr_lo is not None:
        os.environ["REPLAY_BSR_LO"] = str(args.bsr_lo)
    if args.bsr_hi is not None:
        os.environ["REPLAY_BSR_HI"] = str(args.bsr_hi)
    if args.require_brt:
        os.environ["REPLAY_REQUIRE_BRT"] = "1"
    if args.require_bb_extreme:
        os.environ["REPLAY_REQUIRE_BB_EXTREME"] = "1"
    if args.bb_threshold is not None:
        os.environ["REPLAY_BB_THRESHOLD"] = str(args.bb_threshold)
    if args.require_vwap_agree:
        os.environ["REPLAY_REQUIRE_VWAP_AGREE"] = "1"
    if args.vel_consistency is not None:
        os.environ["REPLAY_VEL_CONSISTENCY"] = str(args.vel_consistency)
    if args.vol_surge is not None:
        os.environ["REPLAY_VOL_SURGE"] = str(args.vol_surge)
    if args.max_ask_drift is not None:
        os.environ["REPLAY_MAX_ASK_DRIFT"] = str(args.max_ask_drift)
    if args.require_btc15_agree:
        os.environ["REPLAY_REQUIRE_BTC15_AGREE"] = "1"
    if args.btc15_min_prob is not None:
        os.environ["REPLAY_BTC15_MIN_PROB"] = str(args.btc15_min_prob)
    if args.require_eth_agree:
        os.environ["REPLAY_REQUIRE_ETH_AGREE"] = "1"
    if args.require_cvd_agree:
        os.environ["REPLAY_REQUIRE_CVD_AGREE"] = "1"
    if args.max_peak_retracement is not None:
        os.environ["REPLAY_MAX_PEAK_RETRACEMENT"] = str(args.max_peak_retracement)
    if args.min_efficiency_ratio is not None:
        os.environ["REPLAY_MIN_EFFICIENCY_RATIO"] = str(args.min_efficiency_ratio)
    if args.require_immediate_momentum:
        os.environ["REPLAY_REQUIRE_IMMEDIATE_MOMENTUM"] = "1"
    if args.prev_window_block is not None:
        os.environ["REPLAY_PREV_WINDOW_BLOCK_PCT"] = str(args.prev_window_block)
    if hasattr(args, 'min_path_eff') and args.min_path_eff is not None:
        os.environ["REPLAY_MIN_PATH_EFF"] = str(args.min_path_eff)
    if hasattr(args, 'block_slow_clob') and args.block_slow_clob:
        os.environ["REPLAY_BLOCK_SLOW_CLOB"] = "1"
    if hasattr(args, 'slow_clob_lo') and args.slow_clob_lo is not None:
        os.environ["REPLAY_SLOW_CLOB_LO"] = str(args.slow_clob_lo)
    if hasattr(args, 'slow_clob_hi') and args.slow_clob_hi is not None:
        os.environ["REPLAY_SLOW_CLOB_HI"] = str(args.slow_clob_hi)
    if args.clob_exit:
        os.environ["REPLAY_CLOB_EXIT"] = "1"
    if args.clob_exit_remaining is not None:
        os.environ["REPLAY_CLOB_EXIT_REMAINING"] = str(args.clob_exit_remaining)
    if args.clob_exit_opp_threshold is not None:
        os.environ["REPLAY_CLOB_EXIT_OPP_THRESHOLD"] = str(args.clob_exit_opp_threshold)
    if args.btc_still_lookback is not None:
        os.environ["PAPER_BTC_STILL_LOOKBACK"] = str(args.btc_still_lookback)

    conn = connect_db()
    end_ts = _time_mod.time()
    start_ts = end_ts - args.last_hours * 3600

    replay = PaperReplay(conn, equity=args.equity, start_ts=start_ts, end_ts=end_ts)
    trades = replay.run(start_ts, end_ts)

    if not trades:
        print("No trades in replay period.")
        conn.close()
        return

    wins = sum(1 for t in trades if t.won)
    losses = len(trades) - wins
    total_pnl = sum(t.pnl for t in trades)
    avg_win = sum(t.pnl for t in trades if t.won) / max(wins, 1)
    avg_loss = sum(t.pnl for t in trades if not t.won) / max(losses, 1)
    gross_win = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    print(f"\n{'='*60}")
    print(f" PAPER REPLAY - {args.last_hours}h ({len(trades)} trades)")
    print(f"{'='*60}")
    print(f"  Trades:        {len(trades)}")
    print(f"  Win rate:      {wins}/{len(trades)} ({wins/len(trades)*100:.1f}%)")
    print(f"  Total PnL:     ${total_pnl:+.2f}")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Avg win:       ${avg_win:+.2f}")
    print(f"  Avg loss:      ${avg_loss:+.2f}")
    print(f"  Final equity:  ${replay.equity:.2f}")
    print(f"  Return:        {(replay.equity - args.equity) / args.equity * 100:+.1f}%")

    # Direction breakdown
    for d in ["UP", "DOWN"]:
        dt = [t for t in trades if t.direction == d]
        if dt:
            dw = sum(1 for t in dt if t.won)
            dp = sum(t.pnl for t in dt)
            print(f"  {d}:   {len(dt)}t {dw}W/{len(dt)-dw}L PnL=${dp:+.2f}")

    # Exit reason breakdown
    print(f"\n  Exit reasons:")
    reasons = {}
    for t in trades:
        r = t.close_reason.split("(")[0].strip()
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t.pnl
        if t.won:
            reasons[r]["wins"] += 1
    for r, d in sorted(reasons.items(), key=lambda x: x[1]["pnl"]):
        print(f"    {r:35s}: {d['count']:2d}t {d['wins']}W PnL=${d['pnl']:+.2f}")

    # Individual trades
    print(f"\n  Individual trades:")
    for t in trades:
        from datetime import datetime
        dt = datetime.fromtimestamp(t.opened_at)
        m = "W" if t.won else "L"
        print(f"    {dt.strftime('%m-%d %H:%M')} {t.direction:4s} {m} ${t.pnl:+8.2f} @{t.entry_price:.3f} stk=${t.stake:.0f} | {t.close_reason[:50]}")

    print(f"{'='*60}")
    conn.close()


if __name__ == "__main__":
    main()
