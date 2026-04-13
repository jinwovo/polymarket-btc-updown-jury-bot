"""
Multi-market paper trade replay for BTC 15min and ETH 5min.

Replays historical trades using signal_cache_log + price ticks + poly_odds.
Inline signal generation (no signal_cache_log dependency for new markets).

Usage:
    python paper_replay_multi.py --market btc15 --last-hours 66
    python paper_replay_multi.py --market eth5 --last-hours 66
    python paper_replay_multi.py --market eth5 --last-hours 66 --equity 500 --stake 50
"""
import argparse
import bisect
import logging
import math
import os
import sys
import time as _time_mod
from datetime import datetime

from config import config
from db_config import (
    connect_db,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
)
from market_config import MarketDef, get_market, env as mkt_env
from judges import Jury, MarketContext
from trade_gate import apply_fee_to_pnl

logger = logging.getLogger("paper_replay_multi")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# RAM cache -- preloads price ticks + poly_odds for fast lookback
# ---------------------------------------------------------------------------
class _RamCache:
    """Preload price ticks + poly_odds into RAM for fast lookup."""

    def __init__(self, conn, market: MarketDef, start_ts: float, end_ts: float):
        self._market = market
        self._interval = market.interval_seconds
        logger.info("Preloading %s data into RAM ...", market.label)
        t0 = _time_mod.time()

        cur = conn.cursor()

        # Price ticks (btc_ticks or eth_ticks)
        cur.execute(
            "SELECT ts, price, volume, buy_volume, sell_volume "
            "FROM %s WHERE ts >= %%s AND ts <= %%s ORDER BY ts" % market.price_table,
            (int(start_ts) - self._interval - 300, int(end_ts) + self._interval),
        )
        rows = cur.fetchall()
        self.px_ts = [float(r[0]) for r in rows]
        self.px_val = [float(r[1]) for r in rows]
        self.px_vol = [float(r[2] or 0) for r in rows]
        self.px_buy_vol = [float(r[3] or 0) for r in rows]
        self.px_sell_vol = [float(r[4] or 0) for r in rows]

        # Cross-market: load BTC ticks for ETH (BTC-leads-ETH signal)
        self.btc_ts: list[float] = []
        self.btc_val: list[float] = []
        if market.price_table != "btc_ticks":
            cur.execute(
                "SELECT ts, price FROM btc_ticks "
                "WHERE ts >= %s AND ts <= %s ORDER BY ts",
                (int(start_ts) - self._interval - 300, int(end_ts) + self._interval),
            )
            btc_rows = cur.fetchall()
            self.btc_ts = [float(r[0]) for r in btc_rows]
            self.btc_val = [float(r[1]) for r in btc_rows]
            logger.info("  Cross-market: %d BTC ticks loaded", len(self.btc_ts))
        else:
            self.btc_ts = self.px_ts
            self.btc_val = self.px_val

        # Poly odds -- filtered by slug prefix
        cur.execute(
            "SELECT ts, window_start, up_best_bid, up_best_ask, "
            "       down_best_bid, down_best_ask, up_mid, down_mid "
            "FROM poly_odds WHERE slug LIKE %s "
            "AND ts >= %s AND ts <= %s ORDER BY ts",
            (
                market.slug_prefix + "%",
                int(start_ts) - self._interval,
                int(end_ts) + self._interval,
            ),
        )
        rows2 = cur.fetchall()
        self.odds_ts = [float(r[0]) for r in rows2]
        self.odds_ws = [int(r[1]) for r in rows2]
        self.odds_data = [
            {
                "up_best_bid": r[2],
                "up_best_ask": r[3],
                "down_best_bid": r[4],
                "down_best_ask": r[5],
                "up_mid": r[6],
                "down_mid": r[7],
            }
            for r in rows2
        ]

        # Market windows (for outcome lookup)
        cur.execute(
            "SELECT window_start, actual_outcome, btc_start_price "
            "FROM market_windows WHERE slug LIKE %s "
            "AND window_start >= %s AND window_start <= %s",
            (
                market.slug_prefix + "%",
                int(start_ts) - self._interval,
                int(end_ts) + self._interval,
            ),
        )
        self._mw_map = {}
        for r in cur.fetchall():
            self._mw_map[int(r[0])] = {
                "actual_outcome": r[1],
                "start_price": float(r[2]) if r[2] else None,
            }

        # Signal cache log for this market (may or may not have data)
        scl_table = market.signal_cache_log_table
        self._scl_by_ws = {}
        try:
            cur.execute(
                "SELECT ts, window_start, direction, avg_confidence, max_edge, "
                "up_ask, down_ask, btc_price, start_price, "
                "seconds_elapsed, seconds_remaining, btc_move_pct, "
                "gate_allow, gate_ev, gate_reason, "
                "prev_outcome, odds_velocity, btc_accel_ok, "
                "lag_arb_allow, lag_arb_direction, lag_arb_entry_price, "
                "bb_pos, vwap_agree, ask_drift, btc_still_moving, quality_score "
                "FROM %s WHERE window_start >= %%s AND window_start <= %%s "
                "ORDER BY ts ASC" % scl_table,
                (int(start_ts), int(end_ts)),
            )
            for r in cur.fetchall():
                cols = [d[0] for d in cur.description]
                row_dict = dict(zip(cols, r))
                ws_key = int(row_dict["window_start"])
                self._scl_by_ws.setdefault(ws_key, []).append(row_dict)
        except Exception as e:
            logger.info("No signal_cache_log table '%s' or empty: %s", scl_table, e)

        t1 = _time_mod.time()
        logger.info(
            "RAM loaded: %d %s ticks, %d poly_odds, %d market_windows, %d scl windows in %.1fs",
            len(self.px_ts),
            market.price_table,
            len(self.odds_ts),
            len(self._mw_map),
            len(self._scl_by_ws),
            t1 - t0,
        )

    # -- Price helpers --

    def price_at(self, ts: float):
        idx = bisect.bisect_right(self.px_ts, ts) - 1
        if idx < 0:
            return None
        return self.px_val[idx]

    def btc_price_at(self, ts: float):
        """BTC price at timestamp (cross-market)."""
        if not self.btc_ts:
            return None
        idx = bisect.bisect_right(self.btc_ts, ts) - 1
        if idx < 0:
            return None
        return self.btc_val[idx]

    def btc_move_pct_range(self, ts_start: float, ts_end: float):
        """BTC price change % from ts_start to ts_end."""
        p0 = self.btc_price_at(ts_start)
        p1 = self.btc_price_at(ts_end)
        if not p0 or not p1 or p0 <= 0:
            return 0.0
        return (p1 - p0) / p0 * 100.0

    def volume_imbalance(self, ts_start: float, ts_end: float):
        """Buy/sell volume ratio over a period. >1 = buy-heavy, <1 = sell-heavy."""
        i0 = bisect.bisect_left(self.px_ts, ts_start)
        i1 = bisect.bisect_right(self.px_ts, ts_end)
        buy_v = sum(self.px_buy_vol[i0:i1])
        sell_v = sum(self.px_sell_vol[i0:i1])
        if sell_v <= 0:
            return 2.0 if buy_v > 0 else 1.0
        return buy_v / sell_v

    def prices_range(self, ts_start: float, ts_end: float):
        i0 = bisect.bisect_left(self.px_ts, ts_start)
        i1 = bisect.bisect_right(self.px_ts, ts_end)
        return self.px_ts[i0:i1], self.px_val[i0:i1]

    def anchored_vwap(self, ws_start: float, ts: float):
        """VWAP anchored at window start. Returns (vwap, price_vs_vwap_pct)."""
        i0 = bisect.bisect_left(self.px_ts, ws_start)
        i1 = bisect.bisect_right(self.px_ts, ts)
        if i1 - i0 < 5:
            return None, 0.0
        sum_pv = 0.0
        sum_v = 0.0
        for i in range(i0, i1):
            v = self.px_vol[i]
            if v > 0:
                sum_pv += self.px_val[i] * v
                sum_v += v
        if sum_v <= 0:
            return None, 0.0
        vwap = sum_pv / sum_v
        cur_price = self.px_val[i1 - 1]
        return vwap, (cur_price - vwap) / vwap * 100

    def velocity_consistency(self, ts: float, lookback_sec: float = 30.0):
        """Count how many 1s intervals moved in the same direction over lookback.
        Returns (ratio, dominant_dir): ratio 0-1, 'UP'/'DOWN'."""
        i_end = bisect.bisect_right(self.px_ts, ts) - 1
        i_start = bisect.bisect_left(self.px_ts, ts - lookback_sec)
        if i_end - i_start < 10:
            return 0.5, None
        up_count = 0
        down_count = 0
        for i in range(i_start, i_end):
            diff = self.px_val[i + 1] - self.px_val[i]
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
        """Ratio of recent volume vs baseline."""
        i_end = bisect.bisect_right(self.px_ts, ts)
        i_short = bisect.bisect_left(self.px_ts, ts - short_sec)
        i_long = bisect.bisect_left(self.px_ts, ts - long_sec)
        short_vol = sum(self.px_vol[i_short:i_end])
        long_vol = sum(self.px_vol[i_long:i_end])
        short_dur = max(ts - (self.px_ts[i_short] if i_short < len(self.px_ts) else ts), 1.0)
        long_dur = max(ts - (self.px_ts[i_long] if i_long < len(self.px_ts) else ts), 1.0)
        short_rate = short_vol / short_dur
        long_rate = long_vol / long_dur
        if long_rate <= 0:
            return 1.0
        return short_rate / long_rate

    def efficiency_ratio(self, ts_start: float, ts_end: float):
        """|net move| / sum(|each tick|). 1.0 = straight line."""
        i0 = bisect.bisect_left(self.px_ts, ts_start)
        i1 = bisect.bisect_right(self.px_ts, ts_end)
        if i1 - i0 < 5:
            return None
        net_move = abs(self.px_val[i1 - 1] - self.px_val[i0])
        total_path = sum(abs(self.px_val[i + 1] - self.px_val[i]) for i in range(i0, i1 - 1))
        if total_path <= 0:
            return None
        return net_move / total_path

    def immediate_momentum(self, ts: float, lookback_sec: float = 10.0):
        """Price direction in last N seconds. Returns 'UP'/'DOWN'/None."""
        i1 = bisect.bisect_right(self.px_ts, ts) - 1
        i0 = bisect.bisect_left(self.px_ts, ts - lookback_sec)
        if i1 <= i0 or i0 >= len(self.px_val):
            return None
        diff = self.px_val[i1] - self.px_val[i0]
        if diff > 0:
            return "UP"
        elif diff < 0:
            return "DOWN"
        return None

    def prev_window_move(self, ws: int):
        """Price move % in previous window."""
        prev_start = float(ws - self._interval)
        prev_end = float(ws)
        i0 = bisect.bisect_left(self.px_ts, prev_start)
        i1 = bisect.bisect_right(self.px_ts, prev_end) - 1
        if i1 <= i0 or i0 >= len(self.px_val):
            return None
        start_px = self.px_val[i0]
        end_px = self.px_val[i1]
        if start_px <= 0:
            return None
        return (end_px - start_px) / start_px * 100

    def odds_at(self, ws: int, ts: float):
        """Get CLOB odds at or before ts for the given window_start."""
        idx = bisect.bisect_right(self.odds_ts, ts) - 1
        while idx >= 0:
            if self.odds_ws[idx] == ws:
                return self.odds_data[idx]
            if self.odds_ts[idx] < ts - self._interval:
                break
            idx -= 1
        return None

    def get_scl_entries(self, ws: int) -> list:
        """Return signal_cache_log entries for this window (may be empty)."""
        return self._scl_by_ws.get(ws, [])


# ---------------------------------------------------------------------------
# Replay trade
# ---------------------------------------------------------------------------
class ReplayTrade:
    def __init__(self, window_start, window_end, direction, entry_price, stake,
                 shares, opened_at, confidence):
        self.window_start = window_start
        self.window_end = window_end
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


# ---------------------------------------------------------------------------
# Multi-market paper replay
# ---------------------------------------------------------------------------
class PaperReplayMulti:
    def __init__(self, conn, market: MarketDef, equity: float = 1000.0,
                 start_ts: float = 0, end_ts: float = 0,
                 stake_override: float = 0):
        self.conn = conn
        self.market = market
        self.interval = market.interval_seconds
        self.initial_equity = equity
        self.equity = equity
        self.trades: list[ReplayTrade] = []
        self.taker_fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.03"))

        # Build RAM cache
        if start_ts > 0 and end_ts > 0:
            self._cache = _RamCache(conn, market, start_ts, end_ts)
        else:
            self._cache = None

        # Jury for inline signal generation
        self._jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))

        # Entry config -- market-specific env with PAPER_ fallback
        self.entry_start_sec = float(mkt_env(market, "ENTRY_START_SEC", "80"))
        self.entry_end_sec = float(mkt_env(market, "ENTRY_END_SEC", "270"))
        self.down_entry_end_sec = float(mkt_env(market, "DOWN_ENTRY_END_SEC", "200"))
        self.max_entry_price = float(mkt_env(market, "MAX_ENTRY_PRICE", "0.58"))
        self.down_min_entry_price = float(mkt_env(market, "DOWN_MIN_ENTRY_PRICE", "0.35"))
        self.min_seconds_remaining = float(mkt_env(market, "MIN_SECONDS_REMAINING", "30"))
        self.max_spread = float(mkt_env(market, "MAX_ODDS_SPREAD", "0.12"))
        self.drift_max = float(os.getenv("MAX_ENTRY_PRICE_DRIFT_ABS", "0.080"))

        # Replay filters
        self.min_edge_filter = float(os.getenv("REPLAY_MIN_EDGE", "0"))
        self.min_conf_filter = float(os.getenv("REPLAY_MIN_CONF", "0"))
        self.min_btc_move = float(os.getenv("REPLAY_MIN_BTC_MOVE", "0"))
        self.max_btc_move = float(os.getenv("REPLAY_MAX_BTC_MOVE", "0"))
        self.require_momentum_agree = os.getenv("REPLAY_REQUIRE_MOMENTUM_AGREE", "0") == "1"
        self.min_score = int(os.getenv("REPLAY_MIN_SCORE", "0"))
        self.require_bb_extreme = os.getenv("REPLAY_REQUIRE_BB_EXTREME", "0") == "1"
        self.bb_threshold = float(os.getenv("REPLAY_BB_THRESHOLD", "0.5"))
        self.require_vwap_agree = os.getenv("REPLAY_REQUIRE_VWAP_AGREE", "0") == "1"
        self.require_btc_still_moving = os.getenv("PAPER_REQUIRE_BTC_STILL_MOVING", "false").lower() == "true"
        self.btc_still_lookback = float(os.getenv("PAPER_BTC_STILL_LOOKBACK", "20"))

        # Novel filters (cross-market)
        self.btc_leads_sec = float(os.getenv("REPLAY_BTC_LEADS_SEC", "0"))
        self.btc_leads_min_pct = float(os.getenv("REPLAY_BTC_LEADS_MIN_PCT", "0.03"))
        self.clob_fade_sec = float(os.getenv("REPLAY_CLOB_FADE_SEC", "0"))
        self.require_vol_agree = os.getenv("REPLAY_REQUIRE_VOL_AGREE", "0") == "1"
        self.vol_agree_lookback = float(os.getenv("REPLAY_VOL_AGREE_LOOKBACK", "100"))

        # Novel strategies
        self.btc_override_sec = float(os.getenv("REPLAY_BTC_OVERRIDE_SEC", "0"))
        self.btc_override_min_pct = float(os.getenv("REPLAY_BTC_OVERRIDE_MIN_PCT", "0.03"))
        self.btc_early_entry = os.getenv("REPLAY_BTC_EARLY_ENTRY", "0") == "1"
        self.btc_early_sec = float(os.getenv("REPLAY_BTC_EARLY_SEC", "35"))
        self.btc_early_min_pct = float(os.getenv("REPLAY_BTC_EARLY_MIN_PCT", "0.03"))
        self._scl_only = os.getenv("REPLAY_SCL_ONLY", "0") == "1"  # disable inline jury fallback

        # ETH5 discovery filters
        self.require_eth_accel_pos = os.getenv("REPLAY_REQUIRE_ETH_ACCEL_POS", "0") == "1"
        self.max_eth_vol = float(os.getenv("REPLAY_MAX_ETH_VOL", "0"))  # 0=off, e.g. 0.006
        self.max_clob_shift = float(os.getenv("REPLAY_MAX_CLOB_SHIFT", "0"))  # 0=off, e.g. 0.05
        self.loss_streak_reduce = int(os.getenv("REPLAY_LOSS_STREAK_REDUCE", "0"))
        self.btc_active_min_range = float(os.getenv("REPLAY_BTC_ACTIVE_MIN_RANGE", "0"))
        self.btc_active_lookback = float(os.getenv("REPLAY_BTC_ACTIVE_LOOKBACK", "300"))
        self.profit_take_bid = float(os.getenv("REPLAY_PROFIT_TAKE_BID", "0"))
        self.confidence_sizing = os.getenv("REPLAY_CONFIDENCE_SIZING", "0") == "1"
        self.conf_sizing_btc_mult = float(os.getenv("REPLAY_CONF_SIZING_BTC_MULT", "2.0"))
        self.conf_sizing_full_mult = float(os.getenv("REPLAY_CONF_SIZING_FULL_MULT", "3.0"))

        # Fixed stake
        self._stake_override = stake_override

    # ------------------------------------------------------------------
    # Inline signal generation: build MarketContext from RAM cache
    # ------------------------------------------------------------------
    def _build_context(self, ws: int, ts: float) -> MarketContext | None:
        """Build a MarketContext at timestamp ts for the given window."""
        if not self._cache:
            return None

        # Current asset price
        current_price = self._cache.price_at(ts)
        if not current_price or current_price <= 0:
            return None

        # Start price (at window open)
        start_price = self._cache.price_at(float(ws) + 1.0)
        if not start_price or start_price <= 0:
            return None

        # Recent prices (last 600s)
        r_ts, r_px = self._cache.prices_range(ts - 600, ts)
        if len(r_px) < 20:
            return None

        # CLOB odds
        odds = self._cache.odds_at(ws, ts)
        up_ask = float(odds.get("up_best_ask") or 0.5) if odds else 0.5
        down_ask = float(odds.get("down_best_ask") or 0.5) if odds else 0.5
        up_bid = float(odds.get("up_best_bid") or 0) if odds else 0.0
        down_bid = float(odds.get("down_best_bid") or 0) if odds else 0.0

        elapsed = ts - ws
        remaining = max(0.0, self.interval - elapsed)

        return MarketContext(
            current_binance_price=float(current_price),
            market_start_price=float(start_price),
            recent_prices=list(r_px[-600:]),
            recent_timestamps=list(r_ts[-600:]),
            poly_up_price=up_ask,
            poly_down_price=down_ask,
            seconds_elapsed=elapsed,
            seconds_remaining=remaining,
            poly_up_ask=up_ask,
            poly_down_ask=down_ask,
            poly_up_bid=up_bid,
            poly_down_bid=down_bid,
        )

    # ------------------------------------------------------------------
    # Try entry from signal_cache_log (if exists) or inline jury
    # ------------------------------------------------------------------
    def _simulate_entry_from_scl(self, ws: int, scl_entries: list) -> ReplayTrade | None:
        """Try entry from pre-computed signal_cache_log entries."""
        we = ws + self.interval
        for entry in scl_entries:
            _is_gate = int(entry.get("gate_allow") or 0) == 1
            if not _is_gate:
                continue

            direction = str(entry["direction"])
            scl_elapsed = float(entry.get("seconds_elapsed") or 0)
            confidence = float(entry.get("avg_confidence") or 0.5)
            max_edge = float(entry.get("max_edge") or 0.1)
            gate_ev = float(entry.get("gate_ev") or 0)
            btc_move = float(entry.get("btc_move_pct") or 0)

            # Edge/confidence quality filters
            if self.min_edge_filter > 0 and max_edge < self.min_edge_filter:
                continue
            if self.min_conf_filter > 0 and confidence < self.min_conf_filter:
                continue
            # BTC move filter
            if self.min_btc_move > 0:
                if abs(btc_move) < self.min_btc_move:
                    continue
                if direction == "UP" and btc_move < 0:
                    continue
                if direction == "DOWN" and btc_move > 0:
                    continue
            if self.max_btc_move > 0 and abs(btc_move) > self.max_btc_move:
                continue

            # If signal came early, wait until entry_start_sec and read odds at that time
            if scl_elapsed < self.entry_start_sec:
                check_ts = ws + self.entry_start_sec
                odds_at = self._cache.odds_at(ws, check_ts) if self._cache else None
                if odds_at:
                    up_ask = float(odds_at.get("up_best_ask") or 0.5)
                    down_ask = float(odds_at.get("down_best_ask") or 0.5)
                else:
                    continue
            else:
                # Use SCL recorded ask directly (matches paper_sim gate_lock snapshot)
                up_ask = float(entry.get("up_ask") or 0.5)
                down_ask = float(entry.get("down_ask") or 0.5)
            elapsed = max(scl_elapsed, self.entry_start_sec)
            remaining = float(entry.get("seconds_remaining") or (self.interval - elapsed))

            # Timing
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

            # Spread
            spread = abs(up_ask - down_ask)
            if spread > self.max_spread:
                continue

            # Drift simulation: production polls signal_cache every 0.1s.
            # signal_cache updates every ~1s from signal_generator.
            # paper_sim reads ask within ~0.1-0.3s of gate_allow=1 being written.
            # Use 0.3s delay to match realistic read timing.
            entry_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
            if self._cache:
                _drift_odds = self._cache.odds_at(ws, entry_ts + 0.3)
                if _drift_odds:
                    later_price = float(_drift_odds.get("up_best_ask") or entry_price) if direction == "UP" \
                        else float(_drift_odds.get("down_best_ask") or entry_price)
                    if 0.01 < later_price < 0.99:
                        if later_price > self.max_entry_price:
                            continue
                        if later_price - entry_price > self.drift_max:
                            continue
                        entry_price = later_price
            if entry_price > self.max_entry_price:
                continue

            # BTC still moving filter
            if self.require_btc_still_moving and self._cache:
                p_before = self._cache.price_at(entry_ts - self.btc_still_lookback)
                p_now = self._cache.price_at(entry_ts)
                if p_before and p_now:
                    if direction == "UP" and p_now <= p_before:
                        continue
                    if direction == "DOWN" and p_now >= p_before:
                        continue

            # --- NOVEL FILTERS (SCL path) ---

            # BTC-leads-ETH
            if self.btc_leads_sec > 0 and self._cache:
                btc_chg = self._cache.btc_move_pct_range(float(ws), float(ws) + self.btc_leads_sec)
                if abs(btc_chg) < self.btc_leads_min_pct:
                    continue
                btc_dir = "UP" if btc_chg > 0 else "DOWN"
                if btc_dir != direction:
                    continue

            # CLOB Fade
            if self.clob_fade_sec > 0 and self._cache:
                early_odds = self._cache.odds_at(ws, float(ws) + self.clob_fade_sec)
                if early_odds:
                    e_up_ask = float(early_odds.get("up_best_ask") or 0.5)
                    e_dn_ask = float(early_odds.get("down_best_ask") or 0.5)
                    if abs(e_up_ask - e_dn_ask) > 0.02:
                        clob_lean = "UP" if e_up_ask > e_dn_ask else "DOWN"
                        if direction == clob_lean:
                            continue

            # Volume imbalance
            if self.require_vol_agree and self._cache:
                vol_ratio = self._cache.volume_imbalance(float(ws), entry_ts)
                if direction == "UP" and vol_ratio < 1.0:
                    continue
                if direction == "DOWN" and vol_ratio > 1.0:
                    continue

            # BTC activity filter (SCL path)
            if self.btc_active_min_range > 0 and self._cache:
                i_s = bisect.bisect_left(self._cache.btc_ts, entry_ts - self.btc_active_lookback)
                i_e = bisect.bisect_right(self._cache.btc_ts, entry_ts)
                if i_e - i_s >= 10:
                    btc_range = self._cache.btc_val[i_s:i_e]
                    btc_hi = max(btc_range)
                    btc_lo = min(btc_range)
                    btc_mid = (btc_hi + btc_lo) / 2
                    if btc_mid > 0:
                        range_pct = (btc_hi - btc_lo) / btc_mid * 100
                        if range_pct < self.btc_active_min_range:
                            continue

            # ETH acceleration filter (SCL path)
            if self.require_eth_accel_pos and self._cache:
                p60 = self._cache.price_at(entry_ts - 60)
                p30 = self._cache.price_at(entry_ts - 30)
                p_now = self._cache.price_at(entry_ts)
                if p60 and p30 and p_now and p60 > 0:
                    mom_r = (p_now - p30) / p60 * 100
                    mom_p = (p30 - p60) / p60 * 100
                    accel = (mom_r - mom_p) if direction == "UP" else -(mom_r - mom_p)
                    if accel < 0:
                        continue

            # ETH volatility cap (SCL path)
            if self.max_eth_vol > 0 and self._cache:
                _ev_s = bisect.bisect_left(self._cache.px_ts, entry_ts - 60)
                _ev_e = bisect.bisect_right(self._cache.px_ts, entry_ts)
                _ev_px = self._cache.px_val[_ev_s:_ev_e]
                if len(_ev_px) >= 10:
                    _ev_rets = [(_ev_px[i+1]-_ev_px[i])/_ev_px[i] for i in range(len(_ev_px)-1) if _ev_px[i]>0]
                    _ev_vol = (sum(r**2 for r in _ev_rets)/max(len(_ev_rets),1))**0.5*100 if _ev_rets else 0
                    if _ev_vol > self.max_eth_vol:
                        continue

            # CLOB shift cap (SCL path)
            if self.max_clob_shift > 0 and self._cache:
                _cs_start_odds = self._cache.odds_at(ws, float(ws) + 5)
                if _cs_start_odds:
                    if direction == "UP":
                        _cs_drift = up_ask - float(_cs_start_odds.get("up_best_ask") or 0.5)
                    else:
                        _cs_drift = down_ask - float(_cs_start_odds.get("down_best_ask") or 0.5)
                    if abs(_cs_drift) > self.max_clob_shift:
                        continue

            # Sizing
            stake = self._get_stake(entry_price, confidence, ws=ws, entry_ts=entry_ts, direction=direction)
            shares = stake / entry_price

            return ReplayTrade(
                window_start=ws, window_end=we, direction=direction,
                entry_price=entry_price, stake=stake, shares=shares,
                opened_at=entry_ts, confidence=confidence,
            )

        return None

    def _simulate_entry_inline(self, ws: int) -> ReplayTrade | None:
        """Generate signals inline from price + odds data via Jury."""
        if not self._cache:
            return None

        we = ws + self.interval
        # Scan every 5 seconds from entry_start_sec to entry_end_sec
        scan_step = 5.0
        t = ws + self.entry_start_sec
        t_end = ws + min(self.entry_end_sec, self.interval - self.min_seconds_remaining)

        while t <= t_end:
            ctx = self._build_context(ws, t)
            if ctx is None:
                t += scan_step
                continue

            # Run jury
            try:
                decision = self._jury.deliberate(ctx)
            except Exception:
                t += scan_step
                continue

            if decision.direction not in ("UP", "DOWN"):
                t += scan_step
                continue

            direction = decision.direction
            confidence = decision.avg_confidence
            max_edge = decision.max_edge

            # BTC-direction override: flip jury direction when BTC strongly disagrees
            if self.btc_override_sec > 0 and self._cache:
                btc_chg = self._cache.btc_move_pct_range(float(ws), float(ws) + self.btc_override_sec)
                if abs(btc_chg) >= self.btc_override_min_pct:
                    btc_dir = "UP" if btc_chg > 0 else "DOWN"
                    if btc_dir != direction:
                        direction = btc_dir  # override jury with BTC

            # Edge/confidence quality filters
            if self.min_edge_filter > 0 and max_edge < self.min_edge_filter:
                t += scan_step
                continue
            if self.min_conf_filter > 0 and confidence < self.min_conf_filter:
                t += scan_step
                continue

            # BTC move
            start_px = self._cache.price_at(float(ws) + 1.0)
            cur_px = self._cache.price_at(t)
            btc_move = 0.0
            if start_px and cur_px and start_px > 0:
                btc_move = (cur_px - start_px) / start_px * 100.0

            if self.min_btc_move > 0:
                if abs(btc_move) < self.min_btc_move:
                    t += scan_step
                    continue
                if direction == "UP" and btc_move < 0:
                    t += scan_step
                    continue
                if direction == "DOWN" and btc_move > 0:
                    t += scan_step
                    continue
            if self.max_btc_move > 0 and abs(btc_move) > self.max_btc_move:
                t += scan_step
                continue

            # Timing for DOWN
            elapsed = t - ws
            remaining = self.interval - elapsed
            if direction == "DOWN" and elapsed > self.down_entry_end_sec:
                t += scan_step
                continue

            # Get ask price -- use odds 2s AFTER signal (simulates paper_sim read delay)
            # Production: signal_generator sets gate_allow=1 → paper_sim reads signal_cache
            # on next poll (~0.1s) but signal_cache ask is already 1-2s stale from poly_odds.
            # Using t+2 matches the realistic fill price paper_sim would see.
            _read_delay = 2.0
            odds = self._cache.odds_at(ws, t + _read_delay)
            if not odds:
                odds = self._cache.odds_at(ws, t)
            if not odds:
                t += scan_step
                continue

            up_ask = float(odds.get("up_best_ask") or 0.5)
            down_ask = float(odds.get("down_best_ask") or 0.5)
            entry_price = up_ask if direction == "UP" else down_ask

            if entry_price <= 0.01 or entry_price >= 0.99:
                t += scan_step
                continue
            if entry_price > self.max_entry_price:
                t += scan_step
                continue
            if direction == "DOWN" and entry_price < self.down_min_entry_price:
                t += scan_step
                continue

            # Spread
            spread = abs(up_ask - down_ask)
            if spread > self.max_spread:
                t += scan_step
                continue

            # Additional drift check: also check 1s later (FAK execution window)
            _drift_odds = self._cache.odds_at(ws, t + _read_delay + 1.0)
            if _drift_odds:
                later_price = float(_drift_odds.get("up_best_ask") or entry_price) if direction == "UP" \
                    else float(_drift_odds.get("down_best_ask") or entry_price)
                if 0.01 < later_price < 0.99:
                    if later_price > self.max_entry_price:
                        t += scan_step
                        continue
                    if later_price - entry_price > self.drift_max:
                        t += scan_step
                        continue
                    entry_price = later_price

            if entry_price > self.max_entry_price:
                t += scan_step
                continue

            # Momentum agreement
            if self.require_momentum_agree and self._cache:
                p_30s = self._cache.price_at(t - 30)
                p_now = self._cache.price_at(t)
                if p_30s and p_now:
                    rising = p_now > p_30s
                    if (direction == "UP" and not rising) or (direction == "DOWN" and rising):
                        t += scan_step
                        continue

            # BB extreme filter
            if self.require_bb_extreme and self._cache:
                _bb_s = bisect.bisect_left(self._cache.px_ts, t - 120)
                _bb_i = bisect.bisect_right(self._cache.px_ts, t)
                if _bb_i - _bb_s >= 30:
                    _bb_prices = self._cache.px_val[_bb_s:_bb_i]
                    _bb_window = _bb_prices[-min(60, len(_bb_prices)):]
                    _bb_mean = sum(_bb_window) / len(_bb_window)
                    _bb_std = (sum((p - _bb_mean) ** 2 for p in _bb_window) / len(_bb_window)) ** 0.5
                    if _bb_std > 0.01:
                        _bb_pos = (_bb_prices[-1] - _bb_mean) / (2 * _bb_std)
                        if abs(_bb_pos) < self.bb_threshold:
                            t += scan_step
                            continue

            # VWAP agree filter
            if self.require_vwap_agree and self._cache:
                _vwap, _vwap_pct = self._cache.anchored_vwap(float(ws), t)
                if _vwap is not None:
                    if direction == "UP" and _vwap_pct <= 0:
                        t += scan_step
                        continue
                    if direction == "DOWN" and _vwap_pct >= 0:
                        t += scan_step
                        continue

            # BTC still moving filter
            if self.require_btc_still_moving and self._cache:
                p_before = self._cache.price_at(t - self.btc_still_lookback)
                p_now = self._cache.price_at(t)
                if p_before and p_now:
                    if direction == "UP" and p_now <= p_before:
                        t += scan_step
                        continue
                    if direction == "DOWN" and p_now >= p_before:
                        t += scan_step
                        continue

            # --- NOVEL FILTERS ---

            # BTC-leads-ETH: BTC price direction must confirm entry direction
            if self.btc_leads_sec > 0 and self._cache:
                btc_chg = self._cache.btc_move_pct_range(float(ws), float(ws) + self.btc_leads_sec)
                if abs(btc_chg) < self.btc_leads_min_pct:
                    t += scan_step
                    continue
                btc_dir = "UP" if btc_chg > 0 else "DOWN"
                if btc_dir != direction:
                    t += scan_step
                    continue

            # CLOB Fade: early CLOB direction is systematically wrong, bet against it
            if self.clob_fade_sec > 0 and self._cache:
                early_odds = self._cache.odds_at(ws, float(ws) + self.clob_fade_sec)
                if early_odds:
                    e_up_ask = float(early_odds.get("up_best_ask") or 0.5)
                    e_dn_ask = float(early_odds.get("down_best_ask") or 0.5)
                    # CLOB lean = direction with higher ask (market expects this)
                    # Fade = bet opposite
                    if abs(e_up_ask - e_dn_ask) > 0.02:
                        clob_lean = "UP" if e_up_ask > e_dn_ask else "DOWN"
                        # We want to FADE the CLOB, so our direction should be OPPOSITE to CLOB lean
                        if direction == clob_lean:
                            t += scan_step
                            continue

            # Volume imbalance: buy/sell ratio must confirm direction
            if self.require_vol_agree and self._cache:
                vol_ratio = self._cache.volume_imbalance(float(ws), t)
                if direction == "UP" and vol_ratio < 1.0:
                    t += scan_step
                    continue
                if direction == "DOWN" and vol_ratio > 1.0:
                    t += scan_step
                    continue

            # BTC activity filter: skip dead markets where BTC range is tiny
            if self.btc_active_min_range > 0 and self._cache:
                i_s = bisect.bisect_left(self._cache.btc_ts, t - self.btc_active_lookback)
                i_e = bisect.bisect_right(self._cache.btc_ts, t)
                if i_e - i_s >= 10:
                    btc_range = self._cache.btc_val[i_s:i_e]
                    btc_hi = max(btc_range)
                    btc_lo = min(btc_range)
                    btc_mid = (btc_hi + btc_lo) / 2
                    if btc_mid > 0:
                        range_pct = (btc_hi - btc_lo) / btc_mid * 100
                        if range_pct < self.btc_active_min_range:
                            t += scan_step
                            continue

            # ETH acceleration: skip when price decelerating in our direction
            if self.require_eth_accel_pos and self._cache:
                p60 = self._cache.price_at(t - 60)
                p30 = self._cache.price_at(t - 30)
                p_now = self._cache.price_at(t)
                if p60 and p30 and p_now and p60 > 0:
                    mom_r = (p_now - p30) / p60 * 100
                    mom_p = (p30 - p60) / p60 * 100
                    accel = (mom_r - mom_p) if direction == "UP" else -(mom_r - mom_p)
                    if accel < 0:
                        t += scan_step
                        continue

            # ETH volatility cap: skip when vol too high (noise)
            if self.max_eth_vol > 0 and self._cache:
                _ev_s = bisect.bisect_left(self._cache.px_ts, t - 60)
                _ev_e = bisect.bisect_right(self._cache.px_ts, t)
                _ev_px = self._cache.px_val[_ev_s:_ev_e]
                if len(_ev_px) >= 10:
                    _ev_rets = [(_ev_px[i+1]-_ev_px[i])/_ev_px[i] for i in range(len(_ev_px)-1) if _ev_px[i]>0]
                    _ev_vol = (sum(r**2 for r in _ev_rets)/max(len(_ev_rets),1))**0.5*100 if _ev_rets else 0
                    if _ev_vol > self.max_eth_vol:
                        t += scan_step
                        continue

            # CLOB shift cap: only enter when CLOB hasn't moved much (lazy CLOB = our edge)
            if self.max_clob_shift > 0 and self._cache:
                _cs_start_odds = self._cache.odds_at(ws, float(ws) + 5)
                if _cs_start_odds:
                    if direction == "UP":
                        _cs_drift = up_ask - float(_cs_start_odds.get("up_best_ask") or 0.5)
                    else:
                        _cs_drift = down_ask - float(_cs_start_odds.get("down_best_ask") or 0.5)
                    if abs(_cs_drift) > self.max_clob_shift:
                        t += scan_step
                        continue

            # Score filter (11 signals)
            if self.min_score > 0:
                _score = 0
                if abs(btc_move) >= 0.02:
                    _score += 1
                # prev window outcome
                prev_ws = ws - self.interval
                prev_mw = self._cache._mw_map.get(prev_ws)
                if prev_mw and prev_mw.get("actual_outcome") == direction:
                    _score += 1
                if entry_price <= 0.45:
                    _score += 1
                # gate EV approximation: (prob - entry_price) / entry_price
                _prob_est = confidence * 0.5 + 0.5  # rough
                _ev_est = (_prob_est - entry_price) / max(entry_price, 0.01)
                if _ev_est >= 0.20:
                    _score += 1
                if confidence >= 0.7:
                    _score += 1
                # VWAP agree
                _vw, _vw_pct = self._cache.anchored_vwap(float(ws), t)
                if _vw is not None:
                    if (direction == "UP" and _vw_pct > 0) or (direction == "DOWN" and _vw_pct < 0):
                        _score += 1
                # Velocity consistency
                _vr, _vd = self._cache.velocity_consistency(t)
                if _vr >= 0.6 and _vd == direction:
                    _score += 1
                # Volume surge
                _vs = self._cache.volume_surge(t)
                if _vs >= 1.5:
                    _score += 1
                # BB extreme
                _bb_s2 = bisect.bisect_left(self._cache.px_ts, t - 120)
                _bb_i2 = bisect.bisect_right(self._cache.px_ts, t)
                if _bb_i2 - _bb_s2 >= 30:
                    _bb_px2 = self._cache.px_val[_bb_s2:_bb_i2]
                    _bb_w2 = _bb_px2[-min(60, len(_bb_px2)):]
                    _bb_m2 = sum(_bb_w2) / len(_bb_w2)
                    _bb_sd2 = (sum((p - _bb_m2) ** 2 for p in _bb_w2) / len(_bb_w2)) ** 0.5
                    if _bb_sd2 > 0.01:
                        _bb_p2 = (_bb_px2[-1] - _bb_m2) / (2 * _bb_sd2)
                        if abs(_bb_p2) > 0.5:
                            _score += 1

                if _score < self.min_score:
                    t += scan_step
                    continue

            # Sizing
            stake = self._get_stake(entry_price, confidence, ws=ws, entry_ts=t, direction=direction)
            shares = stake / entry_price

            return ReplayTrade(
                window_start=ws, window_end=we, direction=direction,
                entry_price=entry_price, stake=stake, shares=shares,
                opened_at=t, confidence=confidence,
            )

            # Only first valid signal matters; if we get here we already returned.

        return None

    def _get_stake(self, entry_price: float, confidence: float,
                   ws: int = 0, entry_ts: float = 0, direction: str = "") -> float:
        """Compute stake size."""
        if self._stake_override > 0:
            base_stake = self._stake_override
        else:
            _fixed_stake = float(os.getenv("PAPER_FIXED_STAKE", "0"))
            if _fixed_stake > 0:
                base_stake = _fixed_stake
            else:
                # Default: 15% of initial equity
                base_stake = round(self.initial_equity * 0.15, 2)

                # Kelly-style sizing
                if os.getenv("PAPER_KELLY_SIZING", "true").lower() == "true":
                    _k_score = 0
                    if confidence >= 0.7:
                        _k_score += 1
                    if entry_price <= 0.48:
                        _k_score += 1
                    if _k_score >= 2:
                        base_stake = round(base_stake * 1.5, 2)
                    elif _k_score == 0:
                        base_stake = round(base_stake * 0.5, 2)

                # conf2x
                _mega_mult = float(os.getenv("PAPER_MEGA_MULTIPLIER", "2.0"))
                if confidence >= 0.7 and _mega_mult > 1.0:
                    base_stake = round(base_stake * _mega_mult, 2)

        # Loss streak reduction: halve stake after N consecutive losses
        if self.loss_streak_reduce > 0 and hasattr(self, 'trades') and len(self.trades) >= self.loss_streak_reduce:
            recent = self.trades[-self.loss_streak_reduce:]
            if all(not t.won for t in recent):
                base_stake = round(base_stake * 0.5, 2)

        # Confidence-based sizing: multiply stake when signals align
        if self.confidence_sizing and self._cache and ws > 0 and direction:
            btc_chg = self._cache.btc_move_pct_range(float(ws), float(ws) + 30)
            btc_agrees = (abs(btc_chg) >= 0.03 and
                          ("UP" if btc_chg > 0 else "DOWN") == direction)

            if btc_agrees:
                # Check BB + VWAP for full confidence
                _has_bb = False
                _has_vwap = False
                if entry_ts > 0:
                    _bb_s = bisect.bisect_left(self._cache.px_ts, entry_ts - 120)
                    _bb_i = bisect.bisect_right(self._cache.px_ts, entry_ts)
                    if _bb_i - _bb_s >= 30:
                        _bb_px = self._cache.px_val[_bb_s:_bb_i]
                        _bb_w = _bb_px[-min(60, len(_bb_px)):]
                        _bb_m = sum(_bb_w) / len(_bb_w)
                        _bb_sd = (sum((p - _bb_m) ** 2 for p in _bb_w) / len(_bb_w)) ** 0.5
                        if _bb_sd > 0.01:
                            _bb_p = (_bb_px[-1] - _bb_m) / (2 * _bb_sd)
                            _has_bb = abs(_bb_p) > 0.5

                    _vwap, _vwap_pct = self._cache.anchored_vwap(float(ws), entry_ts)
                    if _vwap is not None:
                        if (direction == "UP" and _vwap_pct > 0) or \
                           (direction == "DOWN" and _vwap_pct < 0):
                            _has_vwap = True

                if _has_bb and _has_vwap:
                    base_stake = round(base_stake * self.conf_sizing_full_mult, 2)
                else:
                    base_stake = round(base_stake * self.conf_sizing_btc_mult, 2)

        return base_stake

    # ------------------------------------------------------------------
    # Exit: hold to settlement OR profit-take at CLOB bid threshold
    # ------------------------------------------------------------------
    def _simulate_exit(self, trade: ReplayTrade, outcome: str | None) -> None:
        """Exit via profit-take (if enabled) or hold to settlement."""

        # Profit-take: scan CLOB bids after entry, sell if bid >= threshold
        if self.profit_take_bid > 0 and self._cache:
            scan_step = 5.0
            t = trade.opened_at + 5.0  # start checking 5s after entry
            t_end = float(trade.window_end) - 5.0

            while t <= t_end:
                odds = self._cache.odds_at(trade.window_start, t)
                if odds:
                    if trade.direction == "UP":
                        bid = float(odds.get("up_best_bid") or 0)
                    else:
                        bid = float(odds.get("down_best_bid") or 0)

                    if bid >= self.profit_take_bid:
                        # Sell at bid price (taker fee applies)
                        sell_value = trade.shares * bid
                        raw_pnl = sell_value - trade.stake
                        # Taker fee on sell
                        trade.pnl = raw_pnl - (sell_value * self.taker_fee_rate)
                        trade.won = trade.pnl > 0
                        trade.close_reason = f"profit_take_bid={bid:.3f}"
                        trade.closed_at = t
                        return
                t += scan_step

        # Fall through to settlement
        if outcome not in ("UP", "DOWN"):
            trade.pnl = 0.0
            trade.close_reason = "no_outcome"
            trade.closed_at = trade.window_end
            return

        won = (outcome == trade.direction)
        if won:
            raw_pnl = trade.shares - trade.stake
            trade.pnl = apply_fee_to_pnl(raw_pnl, trade.stake)
        else:
            trade.pnl = -trade.stake
        trade.won = won
        trade.close_reason = "expiry_settlement"
        trade.closed_at = trade.window_end

    # ------------------------------------------------------------------
    # BTC early entry: enter at 30-40s when BTC has clearly moved,
    # before CLOB reprices ETH, capturing mispricing edge
    # ------------------------------------------------------------------
    def _simulate_btc_early_entry(self, ws: int) -> ReplayTrade | None:
        """Enter early based on BTC direction, before ETH CLOB reprices."""
        if not self._cache or not self.btc_early_entry:
            return None

        we = ws + self.interval
        check_ts = float(ws) + self.btc_early_sec

        # Check BTC direction
        btc_chg = self._cache.btc_move_pct_range(float(ws), check_ts)
        if abs(btc_chg) < self.btc_early_min_pct:
            return None

        direction = "UP" if btc_chg > 0 else "DOWN"

        # Get ETH CLOB price at this early time (should still be near 0.50)
        odds = self._cache.odds_at(ws, check_ts)
        if not odds:
            return None

        up_ask = float(odds.get("up_best_ask") or 0.5)
        down_ask = float(odds.get("down_best_ask") or 0.5)
        entry_price = up_ask if direction == "UP" else down_ask

        if entry_price <= 0.01 or entry_price >= 0.99:
            return None
        if entry_price > self.max_entry_price:
            return None
        if direction == "DOWN" and entry_price < self.down_min_entry_price:
            return None

        # Spread check
        spread = abs(up_ask - down_ask)
        if spread > self.max_spread:
            return None

        stake = self._get_stake(entry_price, 0.6)
        shares = stake / entry_price

        return ReplayTrade(
            window_start=ws, window_end=we, direction=direction,
            entry_price=entry_price, stake=stake, shares=shares,
            opened_at=check_ts, confidence=0.6,
        )

    # ------------------------------------------------------------------
    # Main replay loop
    # ------------------------------------------------------------------
    def run(self, start_ts: float, end_ts: float) -> list[ReplayTrade]:
        """Replay paper trades over a time range."""
        if not self._cache:
            logger.error("No RAM cache -- cannot replay")
            return []

        # Get all windows from poly_odds (more complete than market_windows)
        _interval = self.market.interval_seconds
        windows_raw = fetch_all_dicts(self.conn, """
            SELECT DISTINCT window_start
            FROM poly_odds
            WHERE slug LIKE %s
              AND window_start >= %s AND window_start <= %s
            ORDER BY window_start ASC
        """, (self.market.slug_prefix + "%", int(start_ts), int(end_ts) - _interval))

        # Determine outcome from price ticks (start vs end price)
        windows = []
        for wr in windows_raw:
            ws = int(wr["window_start"])
            we = ws + _interval
            # Check market_windows first
            mw = self._cache._mw_map.get(ws)
            if mw and mw.get("actual_outcome") in ("UP", "DOWN"):
                windows.append({"window_start": ws, "actual_outcome": mw["actual_outcome"]})
                continue
            # Fallback: compute from price ticks
            sp = self._cache.price_at(float(ws) + 1)
            ep = self._cache.price_at(float(we) - 1)
            if sp and ep and sp > 0:
                outcome = "UP" if ep > sp else "DOWN"
                windows.append({"window_start": ws, "actual_outcome": outcome})
            # else: skip (no price data)

        total = len(windows)
        logger.info("Replaying %d %s windows (%.1fh)", total, self.market.label,
                     (end_ts - start_ts) / 3600)

        for i, w in enumerate(windows):
            ws = int(w["window_start"])
            outcome = w["actual_outcome"]

            # Strategy 0: BTC early entry (before jury, captures mispricing)
            trade = self._simulate_btc_early_entry(ws)

            # Strategy 1: try signal_cache_log entries (if exist)
            if trade is None:
                scl_entries = self._cache.get_scl_entries(ws)
                if scl_entries:
                    trade = self._simulate_entry_from_scl(ws, scl_entries)

            # Strategy 2: inline jury evaluation (skip if --scl-only)
            if trade is None and not self._scl_only:
                trade = self._simulate_entry_inline(ws)

            if trade is None:
                continue

            # Cap stake to available equity
            if trade.stake > self.equity:
                trade.stake = max(5.0, self.equity)
                trade.shares = trade.stake / trade.entry_price

            # Settlement
            self._simulate_exit(trade, outcome)

            # Update equity
            self.equity += trade.pnl
            self.trades.append(trade)

            if (i + 1) % 50 == 0:
                logger.info(
                    "  [%d/%d] trades=%d PnL=$%+.2f equity=$%.0f",
                    i + 1, total, len(self.trades),
                    sum(t.pnl for t in self.trades), self.equity,
                )

        return self.trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Multi-market paper trade replay")
    parser.add_argument("--market", type=str, required=True, choices=["btc15", "eth5"],
                        help="Market to replay: btc15 or eth5")
    parser.add_argument("--last-hours", type=float, required=True,
                        help="Lookback period in hours")
    parser.add_argument("--equity", type=float, default=1000.0,
                        help="Starting equity (default 1000)")
    parser.add_argument("--entry-start", type=float, default=None,
                        help="Override entry start seconds")
    parser.add_argument("--max-ask", type=float, default=None,
                        help="Override max entry price")
    parser.add_argument("--stake", type=float, default=None,
                        help="Override fixed stake size")
    parser.add_argument("--min-edge", type=float, default=None,
                        help="Min max_edge to enter")
    parser.add_argument("--min-conf", type=float, default=None,
                        help="Min avg_confidence to enter")
    parser.add_argument("--min-btc-move", type=float, default=None,
                        help="Min abs(btc_move_pct) + direction match")
    parser.add_argument("--max-btc-move", type=float, default=None,
                        help="Max abs(btc_move_pct)")
    parser.add_argument("--require-momentum-agree", action="store_true",
                        help="30s price trend must match direction")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Min signal score to enter")
    parser.add_argument("--require-bb-extreme", action="store_true",
                        help="Only enter at BB extremes")
    parser.add_argument("--bb-threshold", type=float, default=None,
                        help="BB extreme threshold (default 0.5)")
    parser.add_argument("--require-vwap-agree", action="store_true",
                        help="Price vs VWAP must agree with direction")
    parser.add_argument("--require-btc-still-moving", action="store_true",
                        help="Price must still move in direction at entry")
    parser.add_argument("--btc-still-lookback", type=float, default=None,
                        help="Lookback seconds for still-moving check (default 20)")
    # Novel cross-market filters
    parser.add_argument("--btc-leads-sec", type=float, default=None,
                        help="BTC-leads-ETH: check BTC direction over first N sec of window")
    parser.add_argument("--btc-leads-min-pct", type=float, default=None,
                        help="BTC-leads-ETH: min BTC move %% (default 0.03)")
    parser.add_argument("--clob-fade-sec", type=float, default=None,
                        help="CLOB Fade: only enter against early CLOB lean at N sec")
    parser.add_argument("--require-vol-agree", action="store_true",
                        help="Volume imbalance must agree with direction")
    parser.add_argument("--vol-agree-lookback", type=float, default=None,
                        help="Volume lookback seconds (default 100)")
    # Novel strategies
    parser.add_argument("--btc-override-sec", type=float, default=None,
                        help="BTC direction override: flip jury when BTC disagrees over N sec")
    parser.add_argument("--btc-override-min-pct", type=float, default=None,
                        help="BTC override min move %% (default 0.03)")
    parser.add_argument("--btc-early-entry", action="store_true",
                        help="Enter early at 35s when BTC has clearly moved (before CLOB reprices)")
    parser.add_argument("--btc-early-sec", type=float, default=None,
                        help="BTC early entry check time (default 35s)")
    parser.add_argument("--btc-early-min-pct", type=float, default=None,
                        help="BTC early entry min move %% (default 0.03)")
    parser.add_argument("--loss-streak-reduce", type=int, default=None,
                        help="Halve stake after N consecutive losses (e.g., 3)")
    parser.add_argument("--btc-active-min-range", type=float, default=None,
                        help="Skip when BTC 5min range < X%% (dead market filter)")
    parser.add_argument("--btc-active-lookback", type=float, default=None,
                        help="BTC activity lookback seconds (default 300)")
    parser.add_argument("--profit-take-bid", type=float, default=None,
                        help="Sell when CLOB bid reaches this price (e.g., 0.75)")
    parser.add_argument("--confidence-sizing", action="store_true",
                        help="Variable sizing: 2x when BTC-leads, 3x when BB+VWAP+BTC all agree")
    parser.add_argument("--conf-sizing-btc-mult", type=float, default=None,
                        help="Multiplier when BTC-leads agrees (default 2.0)")
    parser.add_argument("--conf-sizing-full-mult", type=float, default=None,
                        help="Multiplier when BB+VWAP+BTC all agree (default 3.0)")
    parser.add_argument("--scl-only", action="store_true",
                        help="Only use signal_cache_log entries, disable inline jury fallback")
    args = parser.parse_args()

    # CLI overrides -> env (set both PAPER_ and market-specific prefix)
    market = get_market(args.market)
    _pfx = market.env_prefix  # e.g. "BTC15_", "ETH5_"
    if args.entry_start is not None:
        os.environ["PAPER_ENTRY_START_SEC"] = str(args.entry_start)
        os.environ[f"{_pfx}ENTRY_START_SEC"] = str(args.entry_start)
    if args.max_ask is not None:
        os.environ["PAPER_MAX_ENTRY_PRICE"] = str(args.max_ask)
        os.environ[f"{_pfx}MAX_ENTRY_PRICE"] = str(args.max_ask)
    if args.stake is not None:
        os.environ["PAPER_FIXED_STAKE"] = str(args.stake)
        os.environ[f"{_pfx}FIXED_STAKE"] = str(args.stake)
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
    if args.require_bb_extreme:
        os.environ["REPLAY_REQUIRE_BB_EXTREME"] = "1"
        os.environ[f"{_pfx}REQUIRE_BB_EXTREME"] = "true"
    if args.bb_threshold is not None:
        os.environ["REPLAY_BB_THRESHOLD"] = str(args.bb_threshold)
        os.environ[f"{_pfx}BB_THRESHOLD"] = str(args.bb_threshold)
    if args.require_vwap_agree:
        os.environ["REPLAY_REQUIRE_VWAP_AGREE"] = "1"
        os.environ[f"{_pfx}REQUIRE_VWAP_AGREE"] = "true"
    if args.require_btc_still_moving:
        os.environ["PAPER_REQUIRE_BTC_STILL_MOVING"] = "true"
        os.environ[f"{_pfx}REQUIRE_BTC_STILL_MOVING"] = "true"
    if args.btc_still_lookback is not None:
        os.environ["PAPER_BTC_STILL_LOOKBACK"] = str(args.btc_still_lookback)
        os.environ[f"{_pfx}BTC_STILL_LOOKBACK"] = str(args.btc_still_lookback)
    # Novel filters
    if args.btc_leads_sec is not None:
        os.environ["REPLAY_BTC_LEADS_SEC"] = str(args.btc_leads_sec)
    if args.btc_leads_min_pct is not None:
        os.environ["REPLAY_BTC_LEADS_MIN_PCT"] = str(args.btc_leads_min_pct)
    if args.clob_fade_sec is not None:
        os.environ["REPLAY_CLOB_FADE_SEC"] = str(args.clob_fade_sec)
    if args.require_vol_agree:
        os.environ["REPLAY_REQUIRE_VOL_AGREE"] = "1"
    if args.vol_agree_lookback is not None:
        os.environ["REPLAY_VOL_AGREE_LOOKBACK"] = str(args.vol_agree_lookback)
    # Novel strategies
    if args.loss_streak_reduce is not None:
        os.environ["REPLAY_LOSS_STREAK_REDUCE"] = str(args.loss_streak_reduce)
    if args.profit_take_bid is not None:
        os.environ["REPLAY_PROFIT_TAKE_BID"] = str(args.profit_take_bid)
    if args.btc_active_min_range is not None:
        os.environ["REPLAY_BTC_ACTIVE_MIN_RANGE"] = str(args.btc_active_min_range)
    if args.btc_active_lookback is not None:
        os.environ["REPLAY_BTC_ACTIVE_LOOKBACK"] = str(args.btc_active_lookback)
    if args.btc_override_sec is not None:
        os.environ["REPLAY_BTC_OVERRIDE_SEC"] = str(args.btc_override_sec)
    if args.btc_override_min_pct is not None:
        os.environ["REPLAY_BTC_OVERRIDE_MIN_PCT"] = str(args.btc_override_min_pct)
    if args.btc_early_entry:
        os.environ["REPLAY_BTC_EARLY_ENTRY"] = "1"
    if args.btc_early_sec is not None:
        os.environ["REPLAY_BTC_EARLY_SEC"] = str(args.btc_early_sec)
    if args.btc_early_min_pct is not None:
        os.environ["REPLAY_BTC_EARLY_MIN_PCT"] = str(args.btc_early_min_pct)
    if args.confidence_sizing:
        os.environ["REPLAY_CONFIDENCE_SIZING"] = "1"
    if args.conf_sizing_btc_mult is not None:
        os.environ["REPLAY_CONF_SIZING_BTC_MULT"] = str(args.conf_sizing_btc_mult)
    if args.conf_sizing_full_mult is not None:
        os.environ["REPLAY_CONF_SIZING_FULL_MULT"] = str(args.conf_sizing_full_mult)
    if args.scl_only:
        os.environ["REPLAY_SCL_ONLY"] = "1"
    logger.info("Market: %s (interval=%ds, price_table=%s, slug=%s)",
                market.label, market.interval_seconds, market.price_table,
                market.slug_prefix)

    conn = connect_db()
    end_ts = _time_mod.time()
    start_ts = end_ts - args.last_hours * 3600

    replay = PaperReplayMulti(
        conn, market,
        equity=args.equity,
        start_ts=start_ts,
        end_ts=end_ts,
        stake_override=args.stake or 0,
    )
    trades = replay.run(start_ts, end_ts)

    if not trades:
        print("\nNo trades in replay period.")
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

    print(f"\n{'=' * 60}")
    print(f" PAPER REPLAY ({market.label}) - {args.last_hours}h ({len(trades)} trades)")
    print(f"{'=' * 60}")
    print(f"  Trades:        {len(trades)}")
    print(f"  Win rate:      {wins}/{len(trades)} ({wins / len(trades) * 100:.1f}%)")
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
            print(f"  {d}:   {len(dt)}t {dw}W/{len(dt) - dw}L PnL=${dp:+.2f}")

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
        dt_str = datetime.fromtimestamp(t.opened_at).strftime("%m-%d %H:%M")
        m = "W" if t.won else "L"
        print(f"    {dt_str} {t.direction:4s} {m} ${t.pnl:+8.2f} @{t.entry_price:.3f} stk=${t.stake:.0f} | {t.close_reason[:50]}")

    print(f"{'=' * 60}")
    conn.close()


if __name__ == "__main__":
    main()
