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
import time
import logging
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import config
from db_config import (
    connect_db,
    db_label,
    fetch_all_dicts,
    init_market_schema,
    is_sqlite_backend,
    sqlite_db_path,
)
from judges import Jury, MarketContext, Vote, JuryDecision
from risk_manager import RiskManager
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backtest")

DB_PATH = sqlite_db_path()


# ---------------------------------------------------------------------------
# Data loading from SQLite
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
    db_path: Path = DB_PATH,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load collected data from SQLite.
    Returns (ticks_df, odds_df, windows_df).
    """
    if is_sqlite_backend() and not db_path.exists():
        logger.error(f"Database not found: {db_path}")
        logger.error("Run `python data_collector.py` first to collect real data!")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

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
        f"down_best_bid, down_best_ask FROM poly_odds WHERE 1=1 {where} ORDER BY ts",
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


# ---------------------------------------------------------------------------
# Backtester (real data)
# ---------------------------------------------------------------------------

class Backtester:
    def __init__(
        self,
        ticks: pd.DataFrame,
        odds: pd.DataFrame,
        windows: pd.DataFrame,
        check_interval: float = 1.0,
        min_elapsed: float = 10.0,
    ):
        self.ticks = ticks
        self.odds = odds
        self.windows = windows
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager()
        self.check_interval = check_interval
        self.min_elapsed = min_elapsed

        self.trades: list[BacktestTrade] = []
        self.recent_results: list[str] = []
        self.windows_with_odds = 0
        self.windows_without_odds = 0
        raw_pos = str(config.trading.position_mode or "BOTH").strip().upper()
        self.position_mode = raw_pos if raw_pos in {"BOTH", "UP_ONLY", "DOWN_ONLY"} else "BOTH"
        raw_profit = str(getattr(config.trading, "live_profit_mode", "BALANCED")).strip().upper()
        self.live_profit_mode = raw_profit if raw_profit in {"AGGRESSIVE", "BALANCED"} else "BALANCED"

    def _get_btc_price(self, ts: float) -> Optional[float]:
        if self.ticks.empty:
            return None
        idx = (self.ticks["ts"] - ts).abs().idxmin()
        diff = abs(self.ticks.loc[idx, "ts"] - ts)
        if diff > 5.0:  # no data within 5 seconds
            return None
        return float(self.ticks.loc[idx, "price"])

    def _get_btc_prices_range(self, start: float, end: float) -> list[float]:
        mask = (self.ticks["ts"] >= start) & (self.ticks["ts"] <= end)
        return self.ticks.loc[mask, "price"].tolist()

    def _get_btc_timestamps_range(self, start: float, end: float) -> list[float]:
        mask = (self.ticks["ts"] >= start) & (self.ticks["ts"] <= end)
        return self.ticks.loc[mask, "ts"].tolist()

    def _get_odds_at(self, window_start: int, ts: float) -> Optional[dict]:
        """Get the closest REAL Polymarket odds record near timestamp ts."""
        mask = self.odds["window_start"] == window_start
        window_odds = self.odds.loc[mask]
        if window_odds.empty:
            return None

        idx = (window_odds["ts"] - ts).abs().idxmin()
        row = window_odds.loc[idx]

        # Only use if within 3 seconds
        if abs(row["ts"] - ts) > 3.0:
            return None

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

    def _process_window(
        self, ws: int, we: int, btc_start: float, btc_end: float, outcome: str
    ) -> bool:
        check_time = float(ws) + self.min_elapsed
        cutoff = float(we) - config.trading.cutoff_before_close_seconds

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
            if abs(btc_change_pct) < 0.02 and seconds_elapsed < 120:
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
            lookback = self._get_btc_prices_range(check_time - 600, check_time)
            lookback_ts = self._get_btc_timestamps_range(check_time - 600, check_time)

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

            support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
            support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
            required_min_edge = float(config.trading.min_edge)
            required_support_ratio = float(config.trading.live_min_support_ratio)
            if self.live_profit_mode == "AGGRESSIVE":
                required_min_edge *= (
                    1.0
                    - _clamp(float(config.trading.live_aggressive_min_edge_relax), 0.0, 0.60)
                )
                required_support_ratio -= _clamp(
                    float(config.trading.live_aggressive_support_relax),
                    0.0,
                    0.25,
                )
            required_min_edge = _clamp(required_min_edge, 0.02, 0.95)
            required_support_ratio = _clamp(required_support_ratio, 0.50, 1.0)

            if decision.avg_confidence < required_min_edge:
                check_time += self.check_interval
                continue
            if self.position_mode == "UP_ONLY" and decision.direction != "UP":
                check_time += self.check_interval
                continue
            if self.position_mode == "DOWN_ONLY" and decision.direction != "DOWN":
                check_time += self.check_interval
                continue
            if support_ratio < required_support_ratio:
                check_time += self.check_interval
                continue
            if bool(config.trading.live_require_unanimous) and not decision.unanimous:
                check_time += self.check_interval
                continue

            up_ask = _safe_prob(float(odds["up_ask"]))
            down_ask = _safe_prob(float(odds["down_ask"]))
            entry_price = up_ask if decision.direction == "UP" else down_ask
            opposite_ask = down_ask if decision.direction == "UP" else up_ask
            if entry_price is None or entry_price <= 0.01 or entry_price >= 0.99:
                check_time += self.check_interval
                continue
            if entry_price < float(config.trading.live_min_entry_side_implied):
                check_time += self.check_interval
                continue
            if (
                opposite_ask is not None
                and opposite_ask > float(config.trading.live_max_opposite_implied)
            ):
                check_time += self.check_interval
                continue

            recent_move = _recent_move_pct(
                prices=list(lookback),
                timestamps=list(lookback_ts),
                now_ts=float(check_time),
                lookback_sec=float(config.trading.live_recent_move_lookback_sec),
            )
            if recent_move is None:
                check_time += self.check_interval
                continue
            base_move_thr = float(config.trading.live_min_recent_move_pct)
            if decision.direction == "UP" and recent_move < base_move_thr:
                check_time += self.check_interval
                continue

            btc_move_from_start_pct = (
                ((float(btc_current) - float(btc_start)) / float(btc_start)) * 100.0
                if float(btc_start) > 0.0
                else 0.0
            )
            down_move_thr = base_move_thr
            if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
                down_move_thr += float(config.trading.live_down_above_start_momentum_extra)
            if decision.direction == "DOWN" and recent_move > -down_move_thr:
                check_time += self.check_interval
                continue

            trend_move = _recent_move_pct(
                prices=list(lookback),
                timestamps=list(lookback_ts),
                now_ts=float(check_time),
                lookback_sec=float(config.trading.live_trend_align_lookback_sec),
            )
            if trend_move is None:
                check_time += self.check_interval
                continue
            trend_opp_thr = abs(float(config.trading.live_trend_align_max_opposing_move_pct))
            if decision.direction == "UP" and trend_move < -trend_opp_thr:
                check_time += self.check_interval
                continue
            if decision.direction == "DOWN" and trend_move > trend_opp_thr:
                check_time += self.check_interval
                continue

            dynamic_min_roi = float(config.trading.min_expected_roi)
            if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
                block_thr = float(config.trading.live_down_above_start_block_pct)
                if btc_move_from_start_pct >= block_thr:
                    check_time += self.check_interval
                    continue
                ratio = btc_move_from_start_pct / max(block_thr, 1e-9)
                dynamic_min_roi += float(config.trading.live_down_above_start_ev_penalty) * _clamp(ratio, 0.0, 1.0)
            if self.live_profit_mode == "AGGRESSIVE":
                relax = _clamp(float(config.trading.live_aggressive_entry_relax), 0.0, 0.60)
                dynamic_min_roi = max(0.0, dynamic_min_roi * (1.0 - relax))

            gate = evaluate_entry_gate(
                direction=decision.direction,
                entry_price=float(entry_price),
                current_price=float(btc_current),
                start_price=float(btc_start),
                seconds_elapsed=float(seconds_elapsed),
                jury_confidence=float(decision.avg_confidence),
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

            market_up_prob, market_down_prob = _normalized_market_probs(up_ask, down_ask)
            if market_up_prob is not None and market_down_prob is not None:
                market_dir_prob = (
                    float(market_up_prob)
                    if decision.direction == "UP"
                    else float(market_down_prob)
                )
                lag_prob_edge = float(gate.model_prob) - float(market_dir_prob)
                if lag_prob_edge < float(config.trading.live_min_lag_prob_edge):
                    check_time += self.check_interval
                    continue

            if up_ask is not None and down_ask is not None:
                side_ask = up_ask if decision.direction == "UP" else down_ask
                opp_ask = down_ask if decision.direction == "UP" else up_ask
                contra_gap = float(opp_ask) - float(side_ask)
                if contra_gap > float(config.trading.live_max_contra_gap):
                    if not (
                        float(gate.model_prob) >= float(config.trading.live_contra_override_min_model_prob)
                        and float(decision.avg_confidence) >= float(config.trading.live_contra_override_min_conf)
                    ):
                        check_time += self.check_interval
                        continue

            if gate.expected_roi < dynamic_min_roi:
                check_time += self.check_interval
                continue

            bet_size = self.risk_mgr.compute_bet_size(
                decision.avg_confidence,
                decision.max_edge,
            )
            if bet_size < config.trading.min_bet_size:
                check_time += self.check_interval
                continue

            won = (decision.direction == outcome)
            if won:
                shares = bet_size / entry_price
                raw_pnl = shares - bet_size
            else:
                raw_pnl = -bet_size
            pnl = apply_fee_to_pnl(raw_pnl, bet_size)

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
            )
            self.trades.append(trade)

            rm_trade = self.risk_mgr.record_trade(decision.direction, bet_size, entry_price)
            self.risk_mgr.resolve_trade(rm_trade, won)
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
         kelly={config.trading.kelly_fraction}, jury={config.trading.jury_threshold}/{jury_size},
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
    original_lag = config.trading.live_min_lag_prob_edge
    original_min_roi = config.trading.min_expected_roi
    original_min_win = config.trading.min_win_probability
    original_bt_level = logging.getLogger("backtest").level
    original_rm_level = logging.getLogger("risk_manager").level

    logging.getLogger("backtest").setLevel(logging.WARNING)
    logging.getLogger("risk_manager").setLevel(logging.WARNING)

    results: list[dict] = []
    try:
        for jury_threshold in jury_grid:
            for edge in edge_grid:
                for lag_edge in lag_grid:
                    for min_roi in min_roi_grid:
                        for min_win in win_prob_grid:
                            config.trading.jury_threshold = int(jury_threshold)
                            config.trading.min_edge = float(edge)
                            config.trading.live_min_lag_prob_edge = float(lag_edge)
                            config.trading.min_expected_roi = float(min_roi)
                            config.trading.min_win_probability = float(min_win)

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
                                    "min_lag_prob_edge": float(lag_edge),
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
        config.trading.live_min_lag_prob_edge = original_lag
        config.trading.min_expected_roi = original_min_roi
        config.trading.min_win_probability = original_min_win
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

    print(f"\n{'='*140}")
    print(" AUTO SWEEP: JURY x EDGE x LAG_EDGE x MIN_ROI x MIN_WIN_PROB")
    print(f"{'='*140}")
    print(
        " rank | eligible | jury | edge  | lag   | minROI | minWin | trades | winrate | pnl       | pf    | maxDD    | score "
    )
    print("-" * 140)
    for idx, row in enumerate(results[:max(1, top_n)], start=1):
        print(
            f" {idx:>4d} | "
            f"{'Y' if row['eligible'] else 'N':>8s} | "
            f"{row['jury_threshold']:>4d} | "
            f"{row['min_edge']:.3f} | "
            f"{row['min_lag_prob_edge']:.3f} | "
            f"{row['min_expected_roi']:.3f} | "
            f"{row['min_win_probability']:.3f} | "
            f"{row['trades']:>6d} | "
            f"{row['win_rate']:>7.1%} | "
            f"${row['total_pnl']:>+8.2f} | "
            f"{row['profit_factor']:>5.2f} | "
            f"${row['max_drawdown']:>+7.2f} | "
            f"{row['stability_score']:.3f}"
        )
    print(f"{'='*140}\n")

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
    parser.add_argument("--lag-grid", type=str, default="0.015,0.020,0.025,0.030", help="Comma-separated LIVE_MIN_LAG_PROB_EDGE values")
    parser.add_argument("--roi-grid", type=str, default="0.002,0.003,0.004,0.006", help="Comma-separated MIN_EXPECTED_ROI values")
    parser.add_argument("--win-prob-grid", type=str, default="0.52,0.53,0.54,0.55", help="Comma-separated MIN_WIN_PROBABILITY values")
    parser.add_argument("--min-trades", type=int, default=10, help="Minimum trades for eligible combos")
    parser.add_argument("--top", type=int, default=10, help="Top rows to print for auto sweep")
    parser.add_argument("--json-out", type=str, default="sweep_best.json", help="Auto-sweep output json file")
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
        if not edge_grid or not jury_grid or not lag_grid or not roi_grid or not win_prob_grid:
            logger.error(
                "Invalid sweep grid. Check --edge-grid, --jury-grid, --lag-grid, --roi-grid, --win-prob-grid."
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
            f"lag={best['min_lag_prob_edge']:.3f} "
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
                "lag": lag_grid,
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

    # Single run
    logging.getLogger("risk_manager").setLevel(logging.WARNING)
    bt = Backtester(ticks, odds, windows)
    trades = bt.run()

    report = generate_report(trades, hours)
    print(report)

    with open("backtest_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("Report saved to backtest_report.txt")

    if args.csv:
        export_trades_csv(trades)


if __name__ == "__main__":
    main()
