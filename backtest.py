"""
Backtest engine for the BTC Up/Down 5m speed arbitrage strategy.

Uses REAL collected data from data_collector.py:
  - Actual Binance tick prices (100ms resolution)
  - Actual Polymarket UP/DOWN odds from the orderbook
  - Actual market outcomes

NO simulated odds. If you don't have real data, run data_collector.py first.

Usage:
    python backtest.py                  # backtest all collected data
    python backtest.py --last-hours 24  # last 24 hours only
    python backtest.py --sweep          # sweep min-edge values
    python backtest.py --csv            # export trades
"""
import argparse
import json
import math
import os
import time
import logging
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

# Load env files before config/db_config read os.getenv
from env_paths import PUBLIC_RUNTIME_ENV_PATH, SECRETS_ENV_PATH
from dotenv import load_dotenv
load_dotenv(SECRETS_ENV_PATH, override=True)
load_dotenv(PUBLIC_RUNTIME_ENV_PATH, override=True)

from config import config
from db_config import (
    connect_db,
    db_label,
    fetch_all_dicts,
    init_market_schema,
)
from judges import Jury, MarketContext, Vote
from risk_manager import RiskManager
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate
from exit_policy import ExitPolicyConfig, ExitPolicyInput, evaluate_exit_policy
from entry_parity import (
    ParityAdaptiveConfig,
    ParityAdaptiveState,
    compute_parity_thresholds,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


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


def _normalized_market_probs(
    up_ask: float | None,
    down_ask: float | None,
) -> tuple[float | None, float | None]:
    up = _safe_prob(up_ask)
    down = _safe_prob(down_ask)
    if up is None or down is None:
        return (None, None)
    total = float(up + down)
    if total <= 1e-9:
        return (None, None)
    up_prob = float(_clamp(up / total, 0.001, 0.999))
    return (up_prob, float(1.0 - up_prob))


def _recent_move_pct(
    prices: list[float],
    timestamps: list[float],
    now_ts: float,
    lookback_sec: float,
) -> float | None:
    n = min(len(prices), len(timestamps))
    if n <= 1:
        return None
    lo_ts = float(now_ts) - max(1.0, float(lookback_sec))
    p0 = None
    p1 = None
    for i in range(n):
        try:
            ts = float(timestamps[i])
            px = float(prices[i])
        except Exception:
            continue
        if ts < lo_ts or px <= 0.0:
            continue
        if p0 is None:
            p0 = px
        p1 = px
    if p0 is None or p1 is None or p0 <= 0.0:
        return None
    return float(((p1 - p0) / p0) * 100.0)

def load_data(
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load collected data from the database.
    Returns (ticks_df, odds_df, windows_df).
    """
    try:
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to connect database ({db_label()}): {e}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    where = ""
    params: list = []
    if start_ts:
        where += " AND ts >= ?"
        params.append(start_ts)
    if end_ts:
        where += " AND ts <= ?"
        params.append(end_ts)

    ticks_rows = fetch_all_dicts(
        conn,
        f"SELECT ts, price FROM btc_ticks WHERE 1=1 {where} ORDER BY ts",
        tuple(params),
    )
    ticks = pd.DataFrame(ticks_rows, columns=["ts", "price"])

    odds_rows = fetch_all_dicts(
        conn,
        f"SELECT ts, window_start, up_mid, down_mid, up_best_bid, up_best_ask, "
        f"down_best_bid, down_best_ask, spread_up, spread_down, overround "
        f"FROM poly_odds WHERE 1=1 {where} ORDER BY ts",
        tuple(params),
    )
    odds = pd.DataFrame(
        odds_rows,
        columns=[
            "ts",
            "window_start",
            "up_mid",
            "down_mid",
            "up_best_bid",
            "up_best_ask",
            "down_best_bid",
            "down_best_ask",
            "spread_up",
            "spread_down",
            "overround",
        ],
    )

    w_where = ""
    w_params: list = []
    if start_ts:
        w_where += " AND window_start >= ?"
        w_params.append(int(start_ts))
    if end_ts:
        w_where += " AND window_end <= ?"
        w_params.append(int(end_ts) + 300)

    windows_rows = fetch_all_dicts(
        conn,
        f"SELECT * FROM market_windows WHERE actual_outcome IS NOT NULL {w_where} "
        f"ORDER BY window_start",
        tuple(w_params),
    )
    windows = pd.DataFrame(windows_rows)

    conn.close()

    logger.info(
        f"Loaded: {len(ticks)} ticks, {len(odds)} odds records, "
        f"{len(windows)} resolved windows"
    )
    return ticks, odds, windows


# ---------------------------------------------------------------------------
# Backtest trade
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    window_start: int
    window_end: int
    entry_second: float     # seconds into the window
    direction: str
    amount: float
    entry_price: float      # actual Polymarket ask price we'd pay
    btc_at_entry: float
    btc_at_start: float
    btc_at_end: float
    btc_change_at_entry_pct: float
    real_poly_up: float     # REAL Polymarket UP mid price at entry
    real_poly_down: float   # REAL Polymarket DOWN mid price at entry
    actual_outcome: str
    won: bool
    pnl: float
    confidence: float
    unanimous: bool
    judge_votes: list[str]
    exit_reason: Optional[str] = None       # early exit reason if any
    exit_second: Optional[float] = None     # seconds into window when exit fired


# ---------------------------------------------------------------------------
# Backtester (real data)
# ---------------------------------------------------------------------------

def _backtest_exit_policy_config() -> ExitPolicyConfig:
    """Build exit policy config using paper env settings (with runtime.public.env overrides)."""
    return ExitPolicyConfig(
        enabled=os.getenv("PAPER_ENABLE_EARLY_EXIT", "true").lower() == "true",
        min_elapsed_sec=float(os.getenv("PAPER_EARLY_EXIT_MIN_ELAPSED_SEC", "25")),
        opposite_ask=float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_ASK", "0.78")),
        opposite_min_loss_roi_pct=float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT", "-20.0")),
        opposite_confirm_polls=int(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS", "3")),
        stop_loss_roi_pct=max(-45.0, float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT", "-40.0"))),
        stop_loss_min_hold_sec=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC", "35")),
        stop_loss_high_conf_cutoff=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF", "0.75")),
        stop_loss_high_conf_min_hold_sec=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC", "20")),
        stop_loss_low_conf_cutoff=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF", "0.60")),
        stop_loss_low_conf_relax_pct=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT", "15")),
        stop_loss_require_btc_adverse=os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE", "true").lower() == "true",
        stop_loss_btc_adverse_pct=float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT", "0.090")),
        max_hold_sec=float(os.getenv("PAPER_EARLY_EXIT_MAX_HOLD_SEC", "220")),
        timestop_max_remain_sec=float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC", "20")),
        timestop_max_roi_pct=float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT", "-8.0")),
        trailing_stop_drop_pct=min(35.0, float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_DROP_PCT", "18.0"))),
        trailing_stop_min_peak_pct=float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT", "10.0")),
        trailing_stop_min_hold_sec=float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC", "20")),
        profit_take_roi_pct=float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_ROI_PCT", "65.0")),
        profit_take_min_hold_sec=float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC", "20")),
        time_weight_enabled=os.getenv("PAPER_TIME_WEIGHTED_EXIT", "true").lower() == "true",
        early_opposite_ask_extra=float(os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA", "0.10")),
        early_opposite_loss_extra_pct=float(os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT", "18.0")),
        early_stop_loss_extra_pct=float(os.getenv("PAPER_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT", "12.0")),
        early_trailing_drop_extra_pct=float(os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT", "14.0")),
        early_trailing_peak_extra_pct=float(os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT", "18.0")),
        early_profit_take_extra_pct=float(os.getenv("PAPER_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT", "25.0")),
        strong_favor_sigma_mult=float(os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT", "0.90")),
        strong_favor_min_move_pct=float(os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT", "0.020")),
        favor_hold_min_remaining_sec=float(os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC", "60")),
        favor_hold_break_even_floor_roi_pct=float(os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT", "-8.0")),
        opposite_late_only_remaining_sec=float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC", "135")),
        opposite_severe_adverse_sigma_mult=float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT", "1.35")),
        opposite_severe_adverse_min_move_pct=float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT", "0.060")),
        trailing_late_only_remaining_sec=float(os.getenv("PAPER_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC", "140")),
        trailing_force_peak_pct=float(os.getenv("PAPER_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT", "95")),
        break_even_late_only_remaining_sec=float(os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC", "120")),
        break_even_force_peak_pct=float(os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT", "90")),
        profit_take_late_only_remaining_sec=float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC", "115")),
        profit_take_force_roi_pct=float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT", "110")),
    )


class Backtester:
    def __init__(
        self,
        ticks: pd.DataFrame,
        odds: pd.DataFrame,
        windows: pd.DataFrame,
        check_interval: float = 1.0,
        min_elapsed: float | None = None,
        initial_equity: float = 1000.0,
        smart_exit: bool = False,
        smart_exit_interval: float = 10.0,
        smart_exit_min_roi_pct: float = 0.0,
    ):
        self.ticks = ticks
        self.odds = odds
        self.windows = windows
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager(initial_equity=initial_equity)
        self.initial_equity = initial_equity
        self.check_interval = check_interval
        self.min_elapsed = min_elapsed if min_elapsed is not None else float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
        self.entry_end_sec = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
        self.exit_cfg = _backtest_exit_policy_config()
        # Smart mid-trade exit feature
        self.smart_exit = smart_exit
        self.smart_exit_interval = max(1.0, float(smart_exit_interval))
        self.smart_exit_min_roi_pct = float(smart_exit_min_roi_pct)

        self.trades: list[BacktestTrade] = []
        self.recent_results: list[str] = []
        self.windows_with_odds = 0
        self.windows_without_odds = 0
        self.last_trade_ts: float = 0.0  # for trade gap enforcement

        # Pre-compute sorted numpy arrays for O(log n) binary-search lookups
        self._tick_ts = ticks["ts"].values.astype(np.float64)
        self._tick_price = ticks["price"].values.astype(np.float64)
        self._odds_ws = odds["window_start"].values
        self._odds_ts = odds["ts"].values.astype(np.float64)
        # Pre-index odds by window_start for fast per-window lookup
        self._odds_by_window: dict[int, tuple[np.ndarray, pd.DataFrame]] = {}
        for ws_val, grp in odds.groupby("window_start"):
            self._odds_by_window[int(ws_val)] = (
                grp["ts"].values.astype(np.float64),
                grp,
            )

    def _get_btc_price(self, ts: float) -> Optional[float]:
        if len(self._tick_ts) == 0:
            return None
        idx = np.searchsorted(self._tick_ts, ts)
        # Check nearest of idx-1 and idx
        best_idx = idx
        best_diff = float("inf")
        for candidate in (idx - 1, idx):
            if 0 <= candidate < len(self._tick_ts):
                d = abs(self._tick_ts[candidate] - ts)
                if d < best_diff:
                    best_diff = d
                    best_idx = candidate
        if best_diff > 5.0:
            return None
        return float(self._tick_price[best_idx])

    def _get_btc_prices_range(self, start: float, end: float) -> list[float]:
        i = np.searchsorted(self._tick_ts, start, side="left")
        j = np.searchsorted(self._tick_ts, end, side="right")
        return self._tick_price[i:j].tolist()

    def _get_btc_timestamps_range(self, start: float, end: float) -> list[float]:
        i = np.searchsorted(self._tick_ts, start, side="left")
        j = np.searchsorted(self._tick_ts, end, side="right")
        return self._tick_ts[i:j].tolist()

    def _get_odds_at(self, window_start: int, ts: float) -> Optional[dict]:
        """Get the closest REAL Polymarket odds record near timestamp ts."""
        entry = self._odds_by_window.get(window_start)
        if entry is None:
            return None
        ts_arr, grp_df = entry
        if len(ts_arr) == 0:
            return None

        idx = np.searchsorted(ts_arr, ts)
        best_idx = idx
        best_diff = float("inf")
        for candidate in (idx - 1, idx):
            if 0 <= candidate < len(ts_arr):
                d = abs(ts_arr[candidate] - ts)
                if d < best_diff:
                    best_diff = d
                    best_idx = candidate

        if best_diff > 3.0:
            return None

        row = grp_df.iloc[best_idx]
        return {
            "up_mid": float(row["up_mid"]),
            "down_mid": float(row["down_mid"]),
            "up_ask": float(row["up_best_ask"]) if row["up_best_ask"] else float(row["up_mid"]) + 0.01,
            "down_ask": float(row["down_best_ask"]) if row["down_best_ask"] else float(row["down_mid"]) + 0.01,
            "up_bid": float(row["up_best_bid"]) if row["up_best_bid"] else float(row["up_mid"]) - 0.01,
            "down_bid": float(row["down_best_bid"]) if row["down_best_bid"] else float(row["down_mid"]) - 0.01,
        }

    def run(self) -> list[BacktestTrade]:
        if self.windows.empty:
            logger.error("No resolved windows in data. Collect more data first!")
            return []

        total = len(self.windows)
        logger.info(f"Backtesting {total} windows with REAL Polymarket odds")

        for i, (_, window) in enumerate(self.windows.iterrows()):
            ws = int(window["window_start"])
            we = int(window["window_end"])
            outcome = window["actual_outcome"]
            btc_start = window["btc_start_price"]
            btc_end = window["btc_end_price"]

            if not btc_start or not btc_end or not outcome:
                continue

            self.recent_results.append(outcome)
            if len(self.recent_results) > 50:
                self.recent_results = self.recent_results[-50:]

            self._process_window(ws, we, btc_start, btc_end, outcome)

            if (i + 1) % 50 == 0:
                wins = sum(1 for t in self.trades if t.won)
                wr = wins / max(len(self.trades), 1)
                pnl = sum(t.pnl for t in self.trades)
                logger.info(
                    f"  [{i+1}/{total}] trades={len(self.trades)} "
                    f"WR={wr:.1%} PnL=${pnl:+.2f} "
                    f"(odds_available={self.windows_with_odds})"
                )

        logger.info(
            f"Done: {total} windows | {len(self.trades)} trades | "
            f"odds_available={self.windows_with_odds} no_odds={self.windows_without_odds}"
        )
        return self.trades

    def _get_odds_range(self, window_start: int, start_ts: float, end_ts: float) -> pd.DataFrame:
        """Get all odds records for a window within a time range."""
        mask = (
            (self.odds["window_start"] == window_start)
            & (self.odds["ts"] >= start_ts)
            & (self.odds["ts"] <= end_ts)
        )
        return self.odds.loc[mask].sort_values("ts")

    def _simulate_exit(
        self,
        ws: int,
        we: int,
        entry_time: float,
        entry_price: float,
        bet_size: float,
        direction: str,
        btc_start: float,
        confidence: float,
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Simulate exit policy after entry. Scan subsequent odds/tick data every 2s.
        Returns (exit_reason, exit_pnl, exit_second) or (None, None, None) if no exit.
        """
        if not self.exit_cfg.enabled:
            return None, None, None

        shares = bet_size / entry_price
        peak_roi_pct = 0.0
        opposite_hits = 0
        btc_at_entry = self._get_btc_price(entry_time)
        if btc_at_entry is None:
            btc_at_entry = btc_start
        check_interval = 2.0  # match paper's ~2s poll interval

        t = entry_time + check_interval
        while t < float(we):
            hold_sec = t - entry_time
            seconds_elapsed = t - float(ws)
            seconds_remaining = float(we) - t

            # Get BTC price
            btc_now = self._get_btc_price(t)
            if btc_now is None:
                t += check_interval
                continue

            # Get odds at this time
            odds_now = self._get_odds_at(ws, t)
            if odds_now is None:
                t += check_interval
                continue

            # Mark-to-market: exit at bid price
            if direction == "UP":
                exit_bid = float(odds_now["up_bid"])
            else:
                exit_bid = float(odds_now["down_bid"])

            current_value = shares * exit_bid
            raw_pnl = current_value - bet_size
            mtm_roi_pct = (raw_pnl / bet_size) * 100.0 if bet_size > 0 else 0.0

            # Track peak
            peak_roi_pct = max(peak_roi_pct, mtm_roi_pct)

            # Opposite ask
            if direction == "UP":
                opp_ask = _safe_prob(float(odds_now["down_ask"]))
            else:
                opp_ask = _safe_prob(float(odds_now["up_ask"]))

            # BTC adverse check
            btc_move_from_entry_pct = (
                ((float(btc_now) - float(btc_at_entry)) / float(btc_at_entry)) * 100.0
                if float(btc_at_entry) > 0 else 0.0
            )
            btc_adverse_ok = True
            if self.exit_cfg.stop_loss_require_btc_adverse:
                adverse_thr = abs(float(self.exit_cfg.stop_loss_btc_adverse_pct))
                if direction == "UP":
                    btc_adverse_ok = btc_move_from_entry_pct <= -adverse_thr
                else:
                    btc_adverse_ok = btc_move_from_entry_pct >= adverse_thr

            # Recent prices for sigma estimation
            lookback_prices = self._get_btc_prices_range(t - 180.0, t)
            lookback_ts = self._get_btc_timestamps_range(t - 180.0, t)

            exit_decision = evaluate_exit_policy(
                ExitPolicyInput(
                    direction=direction,
                    hold_sec=float(hold_sec),
                    seconds_elapsed=float(seconds_elapsed),
                    seconds_remaining=float(seconds_remaining),
                    signal_confidence=float(confidence),
                    mtm_roi_pct=float(mtm_roi_pct),
                    current_price=float(btc_now),
                    start_price=float(btc_start),
                    peak_roi_pct=float(peak_roi_pct),
                    opposite_ask=float(opp_ask) if opp_ask is not None else None,
                    recent_prices=list(lookback_prices),
                    recent_timestamps=list(lookback_ts),
                    btc_adverse_ok=bool(btc_adverse_ok),
                    btc_move_from_entry_pct=float(btc_move_from_entry_pct),
                    opposite_hits=int(opposite_hits),
                ),
                self.exit_cfg,
            )

            opposite_hits = exit_decision.opposite_hits

            if exit_decision.reason is not None:
                # Exit triggered — compute PnL at exit bid
                exit_pnl = apply_fee_to_pnl(raw_pnl, bet_size) if raw_pnl > 0 else raw_pnl
                exit_second = seconds_elapsed
                return exit_decision.reason, exit_pnl, exit_second

            t += check_interval

        return None, None, None

    def _simulate_smart_exit(
        self,
        ws: int,
        we: int,
        entry_time: float,
        entry_price: float,
        bet_size: float,
        direction: str,
        btc_start: float,
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Smart mid-trade exit: re-evaluate Jury every smart_exit_interval seconds.
        If Jury flips direction (or says NO_TRADE) AND current ROI > smart_exit_min_roi_pct,
        exit at current bid price.

        Returns (exit_reason, exit_pnl, exit_second) or (None, None, None) if no exit.
        """
        if not self.smart_exit:
            return None, None, None

        shares = bet_size / entry_price
        t = entry_time + self.smart_exit_interval

        while t < float(we):
            seconds_elapsed = t - float(ws)
            seconds_remaining = float(we) - t

            # Get BTC price at this time
            btc_now = self._get_btc_price(t)
            if btc_now is None:
                t += self.smart_exit_interval
                continue

            # Get odds at this time
            odds_now = self._get_odds_at(ws, t)
            if odds_now is None:
                t += self.smart_exit_interval
                continue

            # Mark-to-market: what would we get at bid?
            if direction == "UP":
                exit_bid = float(odds_now["up_bid"])
            else:
                exit_bid = float(odds_now["down_bid"])

            current_value = shares * exit_bid
            raw_pnl = current_value - bet_size
            mtm_roi_pct = (raw_pnl / bet_size) * 100.0 if bet_size > 0 else 0.0

            # Only exit when in profit (or at configured threshold)
            if mtm_roi_pct <= self.smart_exit_min_roi_pct:
                t += self.smart_exit_interval
                continue

            # Build MarketContext for Jury re-evaluation
            lookback_prices = self._get_btc_prices_range(t - 1200.0, t)
            lookback_ts = self._get_btc_timestamps_range(t - 1200.0, t)

            if len(lookback_prices) < 10:
                t += self.smart_exit_interval
                continue

            ctx = MarketContext(
                current_binance_price=btc_now,
                market_start_price=btc_start,
                recent_prices=lookback_prices,
                recent_timestamps=lookback_ts,
                poly_up_price=float(odds_now["up_mid"]),
                poly_down_price=float(odds_now["down_mid"]),
                seconds_elapsed=seconds_elapsed,
                seconds_remaining=seconds_remaining,
                poly_up_bid=float(odds_now["up_bid"]),
                poly_up_ask=float(odds_now["up_ask"]),
                poly_down_bid=float(odds_now["down_bid"]),
                poly_down_ask=float(odds_now["down_ask"]),
                recent_results=None,
            )

            # Silently re-evaluate jury
            old_level = logging.getLogger("judges").level
            logging.getLogger("judges").setLevel(logging.WARNING)
            try:
                mid_decision = self.jury.deliberate(ctx)
            finally:
                logging.getLogger("judges").setLevel(old_level)

            # Check if jury has flipped: now saying opposite direction or NO_TRADE
            jury_flipped = (
                mid_decision.direction != direction
                and mid_decision.direction in ("UP", "DOWN", "NO_TRADE")
            )
            # Also require the flip to be to the OPPOSITE direction (not just NO_TRADE)
            # unless configured otherwise — here we exit on any non-original signal
            if jury_flipped:
                exit_pnl = apply_fee_to_pnl(raw_pnl, bet_size) if raw_pnl > 0 else raw_pnl
                flip_to = mid_decision.direction
                reason = f"smart_exit_jury_flip:{flip_to}@{seconds_elapsed:.0f}s roi={mtm_roi_pct:+.1f}%"
                return reason, exit_pnl, seconds_elapsed

            t += self.smart_exit_interval

        return None, None, None

    def _process_window(
        self, ws: int, we: int, btc_start: float, btc_end: float, outcome: str
    ) -> bool:
        check_time = float(ws) + self.min_elapsed
        cutoff = float(ws) + self.entry_end_sec

        # Check if we have any odds data for this window
        has_odds = not self.odds.loc[self.odds["window_start"] == ws].empty
        if has_odds:
            self.windows_with_odds += 1
        else:
            self.windows_without_odds += 1
            return False  # Skip windows without real odds

        while check_time < cutoff:
            seconds_elapsed = check_time - ws
            seconds_remaining = we - check_time

            btc_current = self._get_btc_price(check_time)
            if btc_current is None:
                check_time += self.check_interval
                continue

            # Quick filter
            btc_change_pct = ((btc_current - btc_start) / btc_start) * 100.0
            # Divergence risk: Binance-Chainlink gap can flip outcomes near start price.
            # DOWN needs wider boundary due to higher mean-reversion + Chainlink UP bias.
            _bt_min_boundary = float(os.getenv("PAPER_MIN_BOUNDARY_DIST_PCT", "0.040"))
            _bt_down_boundary = float(os.getenv("PAPER_DOWN_MIN_BOUNDARY_DIST_PCT", "0.050"))
            if abs(btc_change_pct) < _bt_min_boundary:
                check_time += self.check_interval
                continue

            # Get REAL Polymarket odds at this moment
            odds = self._get_odds_at(ws, check_time)
            if odds is None:
                check_time += self.check_interval
                continue

            # Risk check
            can_trade, _ = self.risk_mgr.can_trade()
            if not can_trade:
                check_time += self.check_interval
                continue

            # Build context with REAL data
            lookback = self._get_btc_prices_range(check_time - 1200, check_time)
            lookback_ts = self._get_btc_timestamps_range(check_time - 1200, check_time)

            # Data quality: min tick samples for meaningful lookback
            # NOTE: Backtest data density varies — using a lower threshold than
            # paper/live (which require 100 ticks + 16 odds) to avoid blocking
            # valid entries during sparse data periods.
            if len(lookback) < 10:
                check_time += self.check_interval
                continue

            ctx = MarketContext(
                current_binance_price=btc_current,
                market_start_price=btc_start,
                recent_prices=lookback,
                recent_timestamps=lookback_ts,
                poly_up_price=odds["up_mid"],
                poly_down_price=odds["down_mid"],
                seconds_elapsed=seconds_elapsed,
                seconds_remaining=seconds_remaining,
                poly_up_bid=odds["up_bid"],
                poly_up_ask=odds["up_ask"],
                poly_down_bid=odds["down_bid"],
                poly_down_ask=odds["down_ask"],
                recent_results=self.recent_results[-20:],
            )

            # Jury (quiet)
            old_level = logging.getLogger("judges").level
            logging.getLogger("judges").setLevel(logging.WARNING)
            decision = self.jury.deliberate(ctx)
            logging.getLogger("judges").setLevel(old_level)

            if decision.direction == "NO_TRADE":
                check_time += self.check_interval
                continue

            # DOWN-specific: wider boundary distance + entry time cutoff
            if decision.direction == "DOWN":
                if abs(btc_change_pct) < _bt_down_boundary:
                    check_time += self.check_interval
                    continue
                _bt_down_end = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "160"))
                if seconds_elapsed > _bt_down_end:
                    check_time += self.check_interval
                    continue

            support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
            support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
            confidence = float(decision.avg_confidence)

            # --- Price validity & implied probability gates (same as paper) ---
            up_ask = _safe_prob(float(odds["up_ask"]))
            down_ask = _safe_prob(float(odds["down_ask"]))
            entry_price = up_ask if decision.direction == "UP" else down_ask
            opposite_ask = down_ask if decision.direction == "UP" else up_ask
            if entry_price is None or entry_price <= 0.01 or entry_price >= 0.99:
                check_time += self.check_interval
                continue
            _bt_min_side = float(os.getenv("PAPER_MIN_ENTRY_SIDE_IMPLIED", "0.22"))
            # Use better of raw side_ask vs complement of opposite (same as paper/live)
            effective_side = entry_price
            if entry_price is not None and opposite_ask is not None:
                effective_side = max(entry_price, 1.0 - opposite_ask)
            if effective_side is not None and effective_side < _bt_min_side:
                check_time += self.check_interval
                continue
            _bt_down_min_price = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.38"))
            if decision.direction == "DOWN" and entry_price < _bt_down_min_price:
                check_time += self.check_interval
                continue
            _bt_max_opp = float(os.getenv("PAPER_MAX_OPPOSITE_IMPLIED", "0.78"))
            if opposite_ask is not None and opposite_ask > _bt_max_opp:
                check_time += self.check_interval
                continue

            # --- Momentum (short-term) ---
            _bt_move_lookback = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
            recent_move = _recent_move_pct(
                prices=list(lookback),
                timestamps=list(lookback_ts),
                now_ts=float(check_time),
                lookback_sec=_bt_move_lookback,
            )
            if recent_move is None:
                check_time += self.check_interval
                continue
            _bt_min_move = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.006"))

            btc_move_from_start_pct = (
                ((float(btc_current) - float(btc_start)) / float(btc_start)) * 100.0
                if float(btc_start) > 0.0
                else 0.0
            )
            # Strong start-move relaxation (same as paper/live)
            _directional_start_move = (
                btc_move_from_start_pct if decision.direction == "UP"
                else -btc_move_from_start_pct
            )
            _strong_start_factor = 1.0
            if _directional_start_move >= 0.06:
                _strong_start_factor = max(0.0, 1.0 - (_directional_start_move - 0.06) / 0.06)

            if decision.direction == "UP" and recent_move < _bt_min_move * _strong_start_factor:
                check_time += self.check_interval
                continue

            _bt_down_extra = float(os.getenv("PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA", "0.006"))
            down_move_thr = _bt_min_move
            if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
                down_move_thr += _bt_down_extra
            effective_down_thr = down_move_thr * _strong_start_factor
            if decision.direction == "DOWN" and recent_move > -effective_down_thr:
                check_time += self.check_interval
                continue

            # --- Trend alignment (medium-term) ---
            _bt_trend_lookback = float(os.getenv("PAPER_TREND_ALIGN_LOOKBACK_SEC", "75"))
            trend_move = _recent_move_pct(
                prices=list(lookback),
                timestamps=list(lookback_ts),
                now_ts=float(check_time),
                lookback_sec=_bt_trend_lookback,
            )
            if trend_move is None:
                check_time += self.check_interval
                continue
            _bt_trend_opp = float(os.getenv("PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT", "0.004"))
            if decision.direction == "UP" and trend_move < -_bt_trend_opp:
                check_time += self.check_interval
                continue
            if decision.direction == "DOWN" and trend_move > _bt_trend_opp:
                check_time += self.check_interval
                continue

            # --- Macro trend filter (aligned with paper/live) ---
            _bt_macro_lookback = float(os.getenv("PAPER_MACRO_TREND_LOOKBACK_SEC", "900"))
            _bt_macro_block = float(os.getenv("PAPER_MACRO_TREND_BLOCK_PCT", "0.040"))
            macro_move = _recent_move_pct(
                prices=list(lookback),
                timestamps=list(lookback_ts),
                now_ts=float(check_time),
                lookback_sec=_bt_macro_lookback,
            )
            if macro_move is not None:
                if decision.direction == "DOWN" and macro_move > _bt_macro_block:
                    check_time += self.check_interval
                    continue
                if decision.direction == "UP" and macro_move < -_bt_macro_block:
                    check_time += self.check_interval
                    continue

            # --- DOWN above-start block/penalty ---
            _bt_down_block = float(os.getenv("PAPER_DOWN_ABOVE_START_BLOCK_PCT", "0.050"))
            _bt_ev_penalty = float(os.getenv("PAPER_DOWN_ABOVE_START_EV_PENALTY", "0.020"))
            dynamic_min_roi = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.020"))
            if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
                if btc_move_from_start_pct >= _bt_down_block:
                    check_time += self.check_interval
                    continue
                ratio = btc_move_from_start_pct / max(_bt_down_block, 1e-9)
                dynamic_min_roi += _bt_ev_penalty * _clamp(ratio, 0.0, 1.0)

            # --- Entry gate evaluation ---
            gate = evaluate_entry_gate(
                direction=decision.direction,
                entry_price=float(entry_price),
                current_price=float(btc_current),
                start_price=float(btc_start),
                seconds_elapsed=float(seconds_elapsed),
                jury_confidence=float(confidence),
                support_ratio=float(support_ratio),
                seconds_remaining=float(seconds_remaining),
                recent_prices=list(lookback),
                recent_timestamps=list(lookback_ts),
                poly_up_ask=float(odds["up_ask"]),
                poly_down_ask=float(odds["down_ask"]),
                recent_results=list(self.recent_results[-20:]),
            )
            if not gate.allow:
                check_time += self.check_interval
                continue

            # --- Lag probability edge (matches paper/live) ---
            _bt_min_lag_edge = float(os.getenv("PAPER_MIN_LAG_PROB_EDGE", "0.020"))
            if up_ask is not None and down_ask is not None:
                _mkt_total = float(up_ask) + float(down_ask)
                if _mkt_total > 0:
                    side_imp = up_ask if decision.direction == "UP" else down_ask
                    _mkt_dir_prob = float(side_imp) / _mkt_total
                    _lag_edge = float(gate.model_prob) - _mkt_dir_prob
                    if _lag_edge < _bt_min_lag_edge:
                        check_time += self.check_interval
                        continue

            # --- Contra gap (matches paper/live) ---
            _bt_contra = float(os.getenv("PAPER_MAX_CONTRA_GAP", "0.50"))
            _bt_contra_prob = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB", "0.66"))
            _bt_contra_conf = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_CONF", "0.75"))
            if up_ask is not None and down_ask is not None:
                side_ask_v = up_ask if decision.direction == "UP" else down_ask
                opp_ask_v = down_ask if decision.direction == "UP" else up_ask
                contra_gap = float(opp_ask_v) - float(side_ask_v)
                if contra_gap > _bt_contra:
                    if not (
                        float(gate.model_prob) >= _bt_contra_prob
                        and float(confidence) >= _bt_contra_conf
                    ):
                        check_time += self.check_interval
                        continue

            # --- AGGRESSIVE mode ROI relaxation (matches old backtest) ---
            _aggressive_relax = float(os.getenv("PAPER_AGGRESSIVE_ENTRY_RELAX", "0.20"))
            dynamic_min_roi = max(0.0, dynamic_min_roi * (1.0 - _clamp(_aggressive_relax, 0.0, 0.60)))

            # --- EV threshold ---
            if gate.expected_roi < dynamic_min_roi:
                check_time += self.check_interval
                continue

            # --- Bet sizing ---
            bet_size = self.risk_mgr.compute_bet_size(
                decision.avg_confidence,
                decision.max_edge,
            )
            # Time-graduated sizing: closer to expiry = more certain = bigger bet
            # 150s: 1.0x, 200s: 1.25x, 240s: 1.5x
            _time_mult = 1.0 + _clamp((seconds_elapsed - 150) / 180, 0.0, 0.5)
            bet_size = bet_size * _time_mult
            if bet_size < config.trading.min_bet_size:
                check_time += self.check_interval
                continue

            # --- Settlement PnL (hold to expiry) ---
            won = (decision.direction == outcome)
            if won:
                shares = bet_size / entry_price
                raw_pnl = shares - bet_size
                settle_pnl = apply_fee_to_pnl(raw_pnl, bet_size)
            else:
                # Don't charge fee on losses
                settle_pnl = -bet_size

            # --- Exit policy simulation ---
            exit_reason, exit_pnl, exit_second = self._simulate_exit(
                ws=ws,
                we=we,
                entry_time=check_time,
                entry_price=entry_price,
                bet_size=bet_size,
                direction=decision.direction,
                btc_start=btc_start,
                confidence=decision.avg_confidence,
            )

            # --- Smart mid-trade exit (jury re-evaluation) ---
            smart_reason, smart_pnl, smart_second = self._simulate_smart_exit(
                ws=ws,
                we=we,
                entry_time=check_time,
                entry_price=entry_price,
                bet_size=bet_size,
                direction=decision.direction,
                btc_start=btc_start,
            )

            # Pick whichever exit fires first (smallest exit_second wins)
            if smart_reason is not None and smart_pnl is not None:
                use_smart = (
                    exit_second is None
                    or float(smart_second) < float(exit_second)
                )
                if use_smart:
                    exit_reason = smart_reason
                    exit_pnl = smart_pnl
                    exit_second = smart_second

            if exit_reason is not None and exit_pnl is not None:
                pnl = exit_pnl
            else:
                pnl = settle_pnl
                exit_reason = None
                exit_second = None

            trade = BacktestTrade(
                window_start=ws,
                window_end=we,
                entry_second=seconds_elapsed,
                direction=decision.direction,
                amount=bet_size,
                entry_price=entry_price,
                btc_at_entry=btc_current,
                btc_at_start=btc_start,
                btc_at_end=btc_end,
                btc_change_at_entry_pct=btc_change_pct,
                real_poly_up=odds["up_mid"],
                real_poly_down=odds["down_mid"],
                actual_outcome=outcome,
                won=won,
                pnl=pnl,
                confidence=decision.avg_confidence,
                unanimous=decision.unanimous,
                judge_votes=[v.vote.value for v in decision.verdicts],
                exit_reason=exit_reason,
                exit_second=exit_second,
            )
            self.trades.append(trade)
            self.last_trade_ts = check_time  # trade gap enforcement

            rm_trade = self.risk_mgr.record_trade(decision.direction, bet_size, entry_price)
            self.risk_mgr.resolve_trade(rm_trade, won, actual_pnl=pnl)
            return True

            check_time += self.check_interval

        return False


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def generate_report(trades: list[BacktestTrade], hours: float) -> str:
    if not trades:
        return "No trades executed. Need more data or adjust parameters."

    total = len(trades)
    wins = sum(1 for t in trades if t.won)
    losses = total - wins
    win_rate = wins / total
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = total_pnl / total
    avg_win = sum(t.pnl for t in trades if t.won) / max(wins, 1)
    avg_loss = sum(t.pnl for t in trades if not t.won) / max(losses, 1)

    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    profit_factor = gross_profit / max(gross_loss, 0.01)

    cum = []
    r = 0.0
    for t in trades:
        r += t.pnl
        cum.append(r)
    peak = cum[0]
    max_dd = 0.0
    for c in cum:
        peak = max(peak, c)
        max_dd = max(max_dd, peak - c)

    avg_entry_sec = np.mean([t.entry_second for t in trades])
    avg_btc_move = np.mean([abs(t.btc_change_at_entry_pct) for t in trades])

    up_t = [t for t in trades if t.direction == "UP"]
    dn_t = [t for t in trades if t.direction == "DOWN"]
    up_wr = sum(1 for t in up_t if t.won) / max(len(up_t), 1)
    dn_wr = sum(1 for t in dn_t if t.won) / max(len(dn_t), 1)

    unan = [t for t in trades if t.unanimous]
    maj = [t for t in trades if not t.unanimous]
    unan_wr = sum(1 for t in unan if t.won) / max(len(unan), 1)
    maj_wr = sum(1 for t in maj if t.won) / max(len(maj), 1)

    judge_names = Jury().judge_names
    jury_size = len(judge_names)
    majority_size = max(1, jury_size // 2 + 1)
    judge_acc = {}
    for i, name in enumerate(judge_names):
        correct = sum(1 for t in trades if i < len(t.judge_votes) and t.judge_votes[i] == t.actual_outcome)
        voted = sum(1 for t in trades if i < len(t.judge_votes) and t.judge_votes[i] != "ABSTAIN")
        judge_acc[name] = correct / max(voted, 1)

    report = f"""
{'='*70}
 BACKTEST REPORT - REAL DATA (collected by data_collector.py)
 Data: {hours:.1f} hours of live market data
{'='*70}

 OVERVIEW
 ─────────────────────────────────────────
  Total trades:         {total}
  Trades/hour:          {total / max(hours, 0.1):.1f}
  Win rate:             {win_rate:.1%} ({wins}W / {losses}L)
  Total PnL:            ${total_pnl:+.2f}
  Avg PnL/trade:        ${avg_pnl:+.4f}
  Avg win:              ${avg_win:+.4f}
  Avg loss:             ${avg_loss:+.4f}

 TIMING
 ─────────────────────────────────────────
  Avg entry time:       {avg_entry_sec:.0f}s into 5-min window
  Avg |BTC move|:       {avg_btc_move:.4f}%

 RISK
 ─────────────────────────────────────────
  Profit factor:        {profit_factor:.2f}
  Max drawdown:         ${max_dd:.2f}

 DIRECTION
 ─────────────────────────────────────────
  UP:   {len(up_t):4d} trades | WR: {up_wr:.1%} | PnL: ${sum(t.pnl for t in up_t):+.2f}
  DOWN: {len(dn_t):4d} trades | WR: {dn_wr:.1%} | PnL: ${sum(t.pnl for t in dn_t):+.2f}

 JURY
 ─────────────────────────────────────────
  Unanimous ({jury_size}/{jury_size}): {len(unan):4d} | WR: {unan_wr:.1%} | PnL: ${sum(t.pnl for t in unan):+.2f}
  Majority  ({majority_size}/{jury_size}): {len(maj):4d} | WR: {maj_wr:.1%} | PnL: ${sum(t.pnl for t in maj):+.2f}

 JUDGE ACCURACY
 ─────────────────────────────────────────"""

    for name, acc in judge_acc.items():
        report += f"\n  {name:25s} {acc:.1%}"

    report += f"""

 EQUITY CURVE
 ─────────────────────────────────────────"""

    if cum:
        min_c = min(cum)
        max_c = max(cum)
        range_c = max_c - min_c if max_c != min_c else 1.0
        width = 45
        step = max(1, len(cum) // 20)
        for i in range(0, len(cum), step):
            pos = int((cum[i] - min_c) / range_c * width)
            bar = "─" * pos + "●"
            report += f"\n  {i:4d} ${cum[i]:+8.2f} │{bar}"
        pos = int((cum[-1] - min_c) / range_c * width)
        bar = "─" * pos + "●"
        report += f"\n  {len(cum):4d} ${cum[-1]:+8.2f} │{bar}  ◄ FINAL"

    report += f"""

{'='*70}
Config: min_edge={config.trading.min_edge}, max_bet=${config.trading.max_bet_size},
         jury={config.trading.jury_threshold}/{jury_size},
         fee={config.trading.fee_rate:.3%}, min_expected_roi={config.trading.min_expected_roi:.3%}
{'='*70}
"""
    return report


def export_trades_csv(trades: list[BacktestTrade], filename: str = "backtest_trades.csv"):
    rows = []
    jury_size = len(Jury().judge_names)
    for t in trades:
        row = {
            "window_start": datetime.fromtimestamp(t.window_start, tz=timezone.utc).isoformat(),
            "entry_second": t.entry_second,
            "direction": t.direction,
            "amount": t.amount,
            "entry_price": t.entry_price,
            "btc_at_entry": t.btc_at_entry,
            "btc_change_pct": t.btc_change_at_entry_pct,
            "real_poly_up": t.real_poly_up,
            "real_poly_down": t.real_poly_down,
            "actual_outcome": t.actual_outcome,
            "won": t.won,
            "pnl": t.pnl,
            "confidence": t.confidence,
            "unanimous": t.unanimous,
            "exit_reason": t.exit_reason or "",
            "exit_second": t.exit_second if t.exit_second is not None else "",
        }
        for idx in range(jury_size):
            key = f"judge_{idx + 1}"
            row[key] = t.judge_votes[idx] if idx < len(t.judge_votes) else ""
        rows.append(row)
    pd.DataFrame(rows).to_csv(filename, index=False)
    logger.info(f"Exported to {filename}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _compute_trade_metrics(trades: list[BacktestTrade]) -> dict:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
        }

    total = len(trades)
    wins = sum(1 for t in trades if t.won)
    losses = total - wins
    win_rate = wins / max(total, 1)
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = total_pnl / max(total, 1)
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    profit_factor = gross_profit / max(gross_loss, 0.01)

    curve = []
    running = 0.0
    for t in trades:
        running += t.pnl
        curve.append(running)
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
    }


def _stability_score(metrics: dict) -> float:
    trades = float(metrics["trades"])
    win_rate = float(metrics["win_rate"])
    profit_factor = float(metrics["profit_factor"])
    total_pnl = float(metrics["total_pnl"])
    avg_pnl = float(metrics["avg_pnl"])
    max_dd = float(metrics["max_drawdown"])

    trade_score = min(trades / 50.0, 1.0)
    wr_score = win_rate
    pf_score = min(profit_factor / 2.0, 1.0)
    dd_base = max(10.0, abs(total_pnl))
    dd_score = 1.0 / (1.0 + (max_dd / dd_base))
    expectancy_score = 0.5 + 0.5 * math.tanh(avg_pnl / 2.0)

    score = (
        0.35 * wr_score
        + 0.25 * pf_score
        + 0.20 * dd_score
        + 0.10 * trade_score
        + 0.10 * expectancy_score
    )
    return max(0.0, min(score, 1.0))


def _parse_float_grid(raw: str) -> list[float]:
    vals: list[float] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        vals.append(float(p))
    return sorted(set(vals))


def _parse_int_grid(raw: str) -> list[int]:
    vals: list[int] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        vals.append(int(p))
    return sorted(set(vals))


def run_auto_sweep(
    ticks: pd.DataFrame,
    odds: pd.DataFrame,
    windows: pd.DataFrame,
    edge_grid: list[float],
    jury_grid: list[int],
    lag_grid: list[float],
    min_roi_grid: list[float],
    win_prob_grid: list[float],
    min_trades: int,
    top_n: int,
) -> list[dict]:
    original_edge = config.trading.min_edge
    original_jury = config.trading.jury_threshold
    original_min_roi = config.trading.min_expected_roi
    original_min_win = config.trading.min_win_probability
    original_lag_edge = os.getenv("PAPER_MIN_LAG_PROB_EDGE", "0.020")
    original_max_bet = config.trading.max_bet_size
    original_bt_level = logging.getLogger("backtest").level
    original_rm_level = logging.getLogger("risk_manager").level

    # Raise max_bet ceiling so adaptive sizing isn't capped (same as single run)
    config.trading.max_bet_size = max(config.trading.max_bet_size, 200.0)

    logging.getLogger("backtest").setLevel(logging.WARNING)
    logging.getLogger("risk_manager").setLevel(logging.WARNING)

    # If lag_grid has real values, sweep it; otherwise use single current value
    effective_lag_grid = lag_grid if lag_grid and any(v > 0 for v in lag_grid) else [float(original_lag_edge)]

    results: list[dict] = []
    total_combos = len(jury_grid) * len(edge_grid) * len(min_roi_grid) * len(win_prob_grid) * len(effective_lag_grid)
    combo_idx = 0
    try:
        for jury_threshold in jury_grid:
            for edge in edge_grid:
                for min_roi in min_roi_grid:
                    for min_win in win_prob_grid:
                        for lag_edge in effective_lag_grid:
                            combo_idx += 1
                            if combo_idx % 20 == 0 or combo_idx == 1:
                                print(f"  Sweep progress: {combo_idx}/{total_combos}...", flush=True)

                            config.trading.jury_threshold = int(jury_threshold)
                            config.trading.min_edge = float(edge)
                            config.trading.min_expected_roi = float(min_roi)
                            config.trading.min_win_probability = float(min_win)
                            os.environ["PAPER_MIN_LAG_PROB_EDGE"] = str(lag_edge)

                            bt = Backtester(ticks, odds, windows)
                            trades = bt.run()
                            metrics = _compute_trade_metrics(trades)
                            score = _stability_score(metrics)
                            eligible = (
                                metrics["trades"] >= min_trades
                                and metrics["win_rate"] >= 0.50
                                and metrics["profit_factor"] >= 1.00
                            )

                            results.append(
                                {
                                    "jury_threshold": int(jury_threshold),
                                    "min_edge": float(edge),
                                    "lag_edge": float(lag_edge),
                                    "min_expected_roi": float(min_roi),
                                    "min_win_probability": float(min_win),
                                    "stability_score": score,
                                    "eligible": eligible,
                                    **metrics,
                                }
                            )
    finally:
        config.trading.min_edge = original_edge
        config.trading.jury_threshold = original_jury
        config.trading.min_expected_roi = original_min_roi
        config.trading.min_win_probability = original_min_win
        config.trading.max_bet_size = original_max_bet
        os.environ["PAPER_MIN_LAG_PROB_EDGE"] = original_lag_edge
        logging.getLogger("backtest").setLevel(original_bt_level)
        logging.getLogger("risk_manager").setLevel(original_rm_level)

    results.sort(
        key=lambda r: (
            1 if r["eligible"] else 0,
            r["stability_score"],
            r["total_pnl"],
            r["profit_factor"],
            r["win_rate"],
        ),
        reverse=True,
    )

    print(f"\n{'='*145}")
    print(" AUTO SWEEP: JURY x EDGE x LAG_EDGE x MIN_ROI x MIN_WIN_PROB")
    print(f"{'='*145}")
    print(
        " rank | eligible | jury | edge  | lag   | minROI | minWin | trades | winrate | pnl       | pf    | maxDD    | score "
    )
    print("-" * 145)
    for idx, row in enumerate(results[:max(1, top_n)], start=1):
        print(
            f" {idx:>4d} | "
            f"{'Y' if row['eligible'] else 'N':>8s} | "
            f"{row['jury_threshold']:>4d} | "
            f"{row['min_edge']:.3f} | "
            f"{row.get('lag_edge', 0.0):.3f} | "
            f"{row['min_expected_roi']:.3f} | "
            f"{row['min_win_probability']:.3f} | "
            f"{row['trades']:>6d} | "
            f"{row['win_rate']:>7.1%} | "
            f"${row['total_pnl']:>+8.2f} | "
            f"{row['profit_factor']:>5.2f} | "
            f"${row['max_drawdown']:>+7.2f} | "
            f"{row['stability_score']:.3f}"
        )
    print(f"{'='*145}\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest with REAL collected data")
    parser.add_argument("--last-hours", type=float, help="Only use last N hours of data")
    parser.add_argument("--min-edge", type=float, default=None, help="Override min edge")
    parser.add_argument("--max-bet", type=float, default=None, help="Override max bet")
    parser.add_argument("--jury-threshold", type=int, default=None, help="Override jury threshold")
    parser.add_argument("--csv", action="store_true", help="Export trades")
    parser.add_argument("--sweep", action="store_true", help="Sweep min-edge values")
    parser.add_argument("--auto-sweep", action="store_true", help="Auto sweep JURY_THRESHOLD x MIN_EDGE")
    parser.add_argument("--edge-grid", type=str, default="0.04,0.06,0.08,0.10,0.12,0.15", help="Comma-separated min-edge values")
    parser.add_argument("--jury-grid", type=str, default="2,3,4,5", help="Comma-separated jury-threshold values")
    parser.add_argument("--lag-grid", type=str, default="0.020", help="Comma-separated PAPER_MIN_LAG_PROB_EDGE values to sweep")
    parser.add_argument("--roi-grid", type=str, default="0.002,0.003,0.004,0.006", help="Comma-separated MIN_EXPECTED_ROI values")
    parser.add_argument("--win-prob-grid", type=str, default="0.52,0.53,0.54,0.55", help="Comma-separated MIN_WIN_PROBABILITY values")
    parser.add_argument("--min-trades", type=int, default=10, help="Minimum trades for eligible combos")
    parser.add_argument("--top", type=int, default=10, help="Top rows to print for auto sweep")
    parser.add_argument("--json-out", type=str, default="sweep_best.json", help="Auto-sweep output json file")
    parser.add_argument("--equity", type=float, default=1000.0, help="Starting equity for adaptive sizing (default $1000)")
    parser.add_argument("--size-sweep", action="store_true", help="Sweep bet sizing parameters")
    parser.add_argument("--smart-exit", action="store_true", help="Enable smart mid-trade exit (jury re-eval every 10s, exit at bid if jury flips and ROI>0)")
    parser.add_argument("--smart-exit-interval", type=float, default=10.0, help="Seconds between jury re-evaluations during a trade (default 10)")
    parser.add_argument("--smart-exit-min-roi", type=float, default=0.0, help="Minimum ROI%% required before smart exit fires (default 0.0 = any profit)")
    parser.add_argument("--compare-smart-exit", action="store_true", help="Run backtest both without and with smart exit, print side-by-side comparison")
    args = parser.parse_args()

    if args.min_edge is not None:
        config.trading.min_edge = args.min_edge
    if args.max_bet is not None:
        config.trading.max_bet_size = args.max_bet
    if args.jury_threshold is not None:
        config.trading.jury_threshold = args.jury_threshold

    start_ts = None
    if args.last_hours:
        start_ts = time.time() - args.last_hours * 3600

    ticks, odds, windows = load_data(start_ts=start_ts)

    if ticks.empty or windows.empty:
        logger.error(
            "Not enough data. Run `python data_collector.py` for a few hours first!"
        )
        return

    if odds.empty:
        logger.error(
            "No Polymarket odds data found. Make sure data_collector.py "
            "is recording odds (needs active BTC Up/Down markets)."
        )
        return

    hours = (ticks["ts"].max() - ticks["ts"].min()) / 3600

    if args.auto_sweep:
        edge_grid = _parse_float_grid(args.edge_grid)
        jury_grid = _parse_int_grid(args.jury_grid)
        lag_grid = _parse_float_grid(args.lag_grid)
        roi_grid = _parse_float_grid(args.roi_grid)
        win_prob_grid = _parse_float_grid(args.win_prob_grid)
        if not edge_grid or not jury_grid or not roi_grid or not win_prob_grid:
            logger.error(
                "Invalid sweep grid. Check --edge-grid, --jury-grid, --roi-grid, --win-prob-grid."
            )
            return

        results = run_auto_sweep(
            ticks=ticks,
            odds=odds,
            windows=windows,
            edge_grid=edge_grid,
            jury_grid=jury_grid,
            lag_grid=lag_grid,
            min_roi_grid=roi_grid,
            win_prob_grid=win_prob_grid,
            min_trades=max(1, int(args.min_trades)),
            top_n=max(1, int(args.top)),
        )
        if not results:
            logger.error("No sweep results produced.")
            return

        best = next((r for r in results if r["eligible"]), results[0])
        print(
            "BEST COMBO => "
            f"jury={best['jury_threshold']} edge={best['min_edge']:.3f} "
            f"lag={best.get('lag_edge', 0.0):.3f} "
            f"min_roi={best['min_expected_roi']:.3f} "
            f"min_win={best['min_win_probability']:.3f} | "
            f"trades={best['trades']} wr={best['win_rate']:.1%} "
            f"pnl=${best['total_pnl']:+.2f} pf={best['profit_factor']:.2f} "
            f"maxDD=${best['max_drawdown']:.2f} score={best['stability_score']:.3f}"
        )

        payload = {
            "hours": hours,
            "best": best,
            "top": results[: max(1, int(args.top))],
            "grid": {
                "edge": edge_grid,
                "jury": jury_grid,
                "lag_edge": lag_grid,
                "min_expected_roi": roi_grid,
                "min_win_probability": win_prob_grid,
            },
            "min_trades": int(args.min_trades),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("Auto-sweep result saved to %s", args.json_out)
        return

    if args.sweep:
        edges = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
        print(f"\n{'='*60}")
        print(f" MIN-EDGE SWEEP ({hours:.1f} hours of real data)")
        print(f"{'='*60}")
        print(f" {'Edge':>6s} | {'Trades':>6s} | {'WR':>6s} | {'PnL':>10s} | {'PF':>5s}")
        print(f" {'─'*6} | {'─'*6} | {'─'*6} | {'─'*10} | {'─'*5}")

        for edge in edges:
            config.trading.min_edge = edge
            logging.getLogger("backtest").setLevel(logging.WARNING)
            logging.getLogger("risk_manager").setLevel(logging.WARNING)

            bt = Backtester(ticks, odds, windows)
            trades = bt.run()

            if trades:
                wr = sum(1 for t in trades if t.won) / len(trades)
                pnl = sum(t.pnl for t in trades)
                gp = sum(t.pnl for t in trades if t.pnl > 0)
                gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
                pf = gp / max(gl, 0.01)
            else:
                wr = pnl = pf = 0

            print(f" {edge:5.2f}  | {len(trades):6d} | {wr:5.1%} | ${pnl:+9.2f} | {pf:5.2f}")

        print(f"{'='*60}")
        return

    if getattr(args, 'size_sweep', False):
        print(f"\n{'='*80}")
        print(f" BET SIZING SWEEP ({hours:.1f} hours of real data)")
        print(f"{'='*80}")
        print(f" {'BetMin':>6s} | {'BetMax':>6s} | {'Trades':>6s} | {'WR':>6s} | {'PnL':>10s} | {'PF':>5s} | {'MaxDD':>10s} | {'FinalEq':>10s} | {'Return':>7s}")
        print("-" * 80)

        sizing_grid = [
            (0.05, 0.10),  # Conservative
            (0.05, 0.15),  # Current default
            (0.05, 0.20),  # Moderate
            (0.08, 0.20),  # Moderate-aggressive
            (0.10, 0.25),  # Aggressive
            (0.10, 0.30),  # Very aggressive
            (0.15, 0.30),  # High conviction
            (0.15, 0.35),  # Maximum
        ]

        logging.getLogger("backtest").setLevel(logging.WARNING)
        logging.getLogger("risk_manager").setLevel(logging.WARNING)
        config.trading.max_bet_size = max(config.trading.max_bet_size, args.equity * 0.40)

        for bet_min, bet_max in sizing_grid:
            RiskManager.BET_PCT_MIN = bet_min
            RiskManager.BET_PCT_MAX = bet_max

            bt = Backtester(ticks, odds, windows, initial_equity=args.equity)
            trades = bt.run()
            final_eq = bt.risk_mgr.equity

            if trades:
                wr = sum(1 for t in trades if t.won) / len(trades)
                pnl = sum(t.pnl for t in trades)
                gp = sum(t.pnl for t in trades if t.pnl > 0)
                gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
                pf = gp / max(gl, 0.01)
                curve = []
                r = 0.0
                for t in trades:
                    r += t.pnl
                    curve.append(r)
                peak = curve[0]
                max_dd = 0.0
                for v in curve:
                    peak = max(peak, v)
                    max_dd = max(max_dd, peak - v)
            else:
                wr = pnl = pf = max_dd = 0
                final_eq = args.equity

            ret = ((final_eq - args.equity) / args.equity) * 100
            print(f" {bet_min*100:5.0f}%  | {bet_max*100:5.0f}%  | {len(trades):6d} | {wr:5.1%} | ${pnl:+9.2f} | {pf:5.2f} | ${max_dd:>9.2f} | ${final_eq:>9.2f} | {ret:+6.1f}%")

        # Restore defaults
        RiskManager.BET_PCT_MIN = 0.05
        RiskManager.BET_PCT_MAX = 0.15
        print(f"{'='*80}")
        return

    # Single run — raise MAX_BET_SIZE ceiling so adaptive sizing isn't capped at $5
    original_max_bet = config.trading.max_bet_size
    if args.max_bet is None:
        config.trading.max_bet_size = max(config.trading.max_bet_size, args.equity * 0.20)

    logging.getLogger("risk_manager").setLevel(logging.WARNING)

    # --- Compare mode: run baseline vs smart-exit side-by-side ---
    if args.compare_smart_exit:
        logging.getLogger("backtest").setLevel(logging.WARNING)

        # Baseline run (no smart exit)
        bt_base = Backtester(ticks, odds, windows, initial_equity=args.equity, smart_exit=False)
        trades_base = bt_base.run()
        m_base = _compute_trade_metrics(trades_base)

        # Smart-exit run
        bt_smart = Backtester(
            ticks,
            odds,
            windows,
            initial_equity=args.equity,
            smart_exit=True,
            smart_exit_interval=args.smart_exit_interval,
            smart_exit_min_roi_pct=args.smart_exit_min_roi,
        )
        trades_smart = bt_smart.run()
        m_smart = _compute_trade_metrics(trades_smart)

        config.trading.max_bet_size = original_max_bet

        # Count smart-exit-triggered trades
        smart_triggered = [t for t in trades_smart if t.exit_reason and t.exit_reason.startswith("smart_exit")]
        smart_won = sum(1 for t in smart_triggered if t.pnl > 0)

        w = 72
        print(f"\n{'='*w}")
        print(f" SMART EXIT COMPARISON  ({hours:.1f}h of data)")
        print(f"{'='*w}")
        print(f"  Smart exit interval:  {args.smart_exit_interval:.0f}s  |  min ROI to trigger: {args.smart_exit_min_roi:+.1f}%")
        print(f"{'─'*w}")
        print(f"  {'Metric':<28}  {'Baseline':>14}  {'Smart Exit':>14}  {'Delta':>10}")
        print(f"{'─'*w}")

        def _fmt_delta(base_val, smart_val, fmt="{:+.2f}", pct=False):
            delta = smart_val - base_val
            if pct:
                return f"{delta:+.1f}pp"
            try:
                return fmt.format(delta)
            except Exception:
                return str(delta)

        rows = [
            ("Trades",        m_base["trades"],       m_smart["trades"],       "{:d}",    False),
            ("Win rate",      m_base["win_rate"]*100, m_smart["win_rate"]*100, "{:.1f}%", True),
            ("Total PnL ($)", m_base["total_pnl"],    m_smart["total_pnl"],    "${:+.2f}", False),
            ("Avg PnL ($)",   m_base["avg_pnl"],      m_smart["avg_pnl"],      "${:+.4f}", False),
            ("Profit factor", m_base["profit_factor"],m_smart["profit_factor"],"{:.2f}",  False),
            ("Max drawdown",  m_base["max_drawdown"], m_smart["max_drawdown"], "${:.2f}", False),
        ]
        for label, bv, sv, fmt, pct in rows:
            try:
                bstr = fmt.format(bv)
                sstr = fmt.format(sv)
            except Exception:
                bstr = str(bv)
                sstr = str(sv)
            delta_str = _fmt_delta(bv, sv, fmt, pct)
            print(f"  {label:<28}  {bstr:>14}  {sstr:>14}  {delta_str:>10}")

        print(f"{'─'*w}")
        print(f"  Smart exits triggered:  {len(smart_triggered)}  |  profitable: {smart_won}/{len(smart_triggered)}")

        # Break down smart exits by flip type
        flip_counts: dict[str, int] = {}
        for t in smart_triggered:
            key = t.exit_reason.split(":")[1].split("@")[0] if t.exit_reason and ":" in t.exit_reason else "unknown"
            flip_counts[key] = flip_counts.get(key, 0) + 1
        if flip_counts:
            print(f"  Flip breakdown: " + "  ".join(f"{k}={v}" for k, v in sorted(flip_counts.items())))

        print(f"{'='*w}\n")

        if args.csv:
            export_trades_csv(trades_base, "backtest_trades_baseline.csv")
            export_trades_csv(trades_smart, "backtest_trades_smart_exit.csv")
        return

    # --- Normal single run ---
    bt = Backtester(
        ticks,
        odds,
        windows,
        initial_equity=args.equity,
        smart_exit=args.smart_exit,
        smart_exit_interval=args.smart_exit_interval,
        smart_exit_min_roi_pct=args.smart_exit_min_roi,
    )
    trades = bt.run()
    final_equity = bt.risk_mgr.equity

    report = generate_report(trades, hours)
    report += f"\n ADAPTIVE SIZING\n {'─'*41}\n"
    report += f"  Start equity:       ${args.equity:.2f}\n"
    report += f"  Final equity:       ${final_equity:.2f}\n"
    report += f"  Return:             {((final_equity - args.equity) / args.equity) * 100:+.1f}%\n"
    report += f"  Bet range:          {RiskManager.BET_PCT_MIN*100:.0f}%-{RiskManager.BET_PCT_MAX*100:.0f}% of equity\n"
    if args.smart_exit:
        smart_triggered = [t for t in trades if t.exit_reason and t.exit_reason.startswith("smart_exit")]
        report += f"\n SMART EXIT\n {'─'*41}\n"
        report += f"  Interval:           {args.smart_exit_interval:.0f}s\n"
        report += f"  Min ROI to trigger: {args.smart_exit_min_roi:+.1f}%\n"
        report += f"  Triggered:          {len(smart_triggered)} trades\n"
        smart_won = sum(1 for t in smart_triggered if t.pnl > 0)
        report += f"  Profitable exits:   {smart_won}/{len(smart_triggered)}\n"

    config.trading.max_bet_size = original_max_bet

    with open("backtest_report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("ascii", errors="replace").decode("ascii"))
    logger.info("Report saved to backtest_report.txt")

    if args.csv:
        export_trades_csv(trades)


if __name__ == "__main__":
    main()
