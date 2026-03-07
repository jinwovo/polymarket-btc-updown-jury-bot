"""
Live paper-trading simulator for BTC Up/Down 5m signals.

What it does:
- Watches real-time signal from dashboard_server.build_snapshot()
- On actionable signal, enters one virtual trade per 5m window
- Uses REAL current orderbook ask price (UP/DOWN) as entry
- Calculates pay-to-win metrics for a virtual stake (default $1000)
- Resolves trade when market_windows.actual_outcome is available

Usage:
    python paper_trade_sim.py
    python paper_trade_sim.py --stake 1000 --interval 2
    python paper_trade_sim.py --status
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dashboard_server import build_snapshot
from db_config import (
    connect_db,
    db_label,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    is_sqlite_backend,
)
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("paper_sim")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


PAPER_RISK_FRACTION = float(os.getenv("PAPER_RISK_FRACTION", "0.20"))
PAPER_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.060"))
PAPER_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.80"))
PAPER_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.35"))
PAPER_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.58"))
PAPER_MIN_BET = float(os.getenv("PAPER_MIN_BET", "25"))
PAPER_MAX_BET_FRAC = float(os.getenv("PAPER_MAX_BET_FRAC", "0.12"))
PAPER_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "75"))
PAPER_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "240"))
PAPER_MIN_SECONDS_REMAINING = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "45"))
PAPER_MIN_TICK_SAMPLES = int(os.getenv("PAPER_MIN_TICK_SAMPLES", "180"))
PAPER_MIN_ODDS_SAMPLES = int(os.getenv("PAPER_MIN_ODDS_SAMPLES", "24"))
PAPER_RECENT_MOVE_LOOKBACK_SEC = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
PAPER_MIN_RECENT_MOVE_PCT = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.006"))
# Dynamic minimum trade gap:
# - base gap is permissive enough to allow a strong signal in the next 5m window
# - adaptive gap expands toward target gap when performance deteriorates
PAPER_BASE_TRADE_GAP_SEC = float(os.getenv("PAPER_BASE_TRADE_GAP_SEC", "300"))
PAPER_TARGET_TRADE_GAP_SEC = float(os.getenv("PAPER_TARGET_TRADE_GAP_SEC", "1800"))
PAPER_MAX_DRAWDOWN_STOP_PCT = float(os.getenv("PAPER_MAX_DRAWDOWN_STOP_PCT", "0.20"))
PAPER_RECENT_PERF_WINDOW = int(os.getenv("PAPER_RECENT_PERF_WINDOW", "8"))
PAPER_MIN_RECENT_WIN_RATE = float(os.getenv("PAPER_MIN_RECENT_WIN_RATE", "0.55"))
PAPER_REQUIRE_UNANIMOUS = os.getenv("PAPER_REQUIRE_UNANIMOUS", "false").lower() == "true"
PAPER_HIGH_QUALITY_EV = float(os.getenv("PAPER_HIGH_QUALITY_EV", "0.12"))
PAPER_HIGH_QUALITY_CONF = float(os.getenv("PAPER_HIGH_QUALITY_CONF", "0.50"))
PAPER_SIZING_MODE = str(os.getenv("PAPER_SIZING_MODE", "adaptive")).strip().lower()


def init_paper_table(conn):
    if is_sqlite_backend():
        sql = """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start INTEGER NOT NULL UNIQUE,
            window_end INTEGER NOT NULL,
            direction TEXT NOT NULL,
            stake REAL NOT NULL,
            entry_price REAL NOT NULL,
            payout_multiple REAL NOT NULL,
            shares REAL NOT NULL,
            potential_win_pnl REAL NOT NULL,
            signal_confidence REAL NOT NULL,
            signal_reason TEXT,
            initial_capital REAL,
            risk_fraction REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            opened_at REAL NOT NULL,
            closed_at REAL,
            actual_outcome TEXT,
            won INTEGER,
            pnl REAL,
            roi_pct REAL
        )
        """
    else:
        sql = """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            window_start BIGINT NOT NULL UNIQUE,
            window_end BIGINT NOT NULL,
            direction VARCHAR(16) NOT NULL,
            stake DOUBLE NOT NULL,
            entry_price DOUBLE NOT NULL,
            payout_multiple DOUBLE NOT NULL,
            shares DOUBLE NOT NULL,
            potential_win_pnl DOUBLE NOT NULL,
            signal_confidence DOUBLE NOT NULL,
            signal_reason TEXT NULL,
            initial_capital DOUBLE NULL,
            risk_fraction DOUBLE NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            opened_at DOUBLE NOT NULL,
            closed_at DOUBLE NULL,
            actual_outcome VARCHAR(16) NULL,
            won TINYINT NULL,
            pnl DOUBLE NULL,
            roi_pct DOUBLE NULL,
            INDEX idx_paper_status (status),
            INDEX idx_paper_closed (closed_at)
        ) ENGINE=InnoDB
        """
    execute_write(conn, sql)
    # Lightweight schema migration for existing deployments.
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN initial_capital REAL")
    except Exception:
        pass
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN risk_fraction REAL")
    except Exception:
        pass
    conn.commit()


def _equity_snapshot(conn, initial_capital: float) -> tuple[float, float]:
    closed_pnl_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status='CLOSED'",
    )
    open_notional_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(stake), 0) FROM paper_trades WHERE status='OPEN'",
    )
    closed_pnl = float(closed_pnl_row[0] or 0.0) if closed_pnl_row else 0.0
    open_notional = float(open_notional_row[0] or 0.0) if open_notional_row else 0.0
    realized_equity = initial_capital + closed_pnl
    available_equity = max(0.0, realized_equity - open_notional)
    return realized_equity, available_equity


def _compute_bet_size(
    available_equity: float,
    initial_capital: float,
    expected_roi: float,
    risk_fraction: float,
) -> float:
    if available_equity <= 0:
        return 0.0
    rf = _clamp(risk_fraction, 0.01, 1.0)
    edge_scale = _clamp(expected_roi / 0.10, 0.4, 1.4)
    target_frac = min(PAPER_MAX_BET_FRAC, rf * edge_scale)
    raw_size = available_equity * target_frac
    hard_cap = max(PAPER_MIN_BET, initial_capital * PAPER_MAX_BET_FRAC)
    sized = min(raw_size, available_equity, hard_cap)
    return round(max(0.0, sized), 2)


def _recent_risk_state(conn) -> tuple[int, float]:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl
           FROM paper_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END DESC,
             id DESC
           LIMIT 6""",
    )
    if not rows:
        return 0, 0.0

    loss_streak = 0
    for row in rows:
        pnl = float(row.get("pnl") or 0.0)
        if pnl < 0.0:
            loss_streak += 1
        else:
            break

    losses = sum(1 for row in rows if float(row.get("pnl") or 0.0) < 0.0)
    loss_rate = losses / float(len(rows))
    return loss_streak, loss_rate


def _recent_performance(conn, limit: int) -> tuple[int, float, float]:
    lim = max(1, int(limit))
    rows = fetch_all_dicts(
        conn,
        """SELECT won, pnl
           FROM paper_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END DESC,
             id DESC
           LIMIT ?""",
        (lim,),
    )
    if not rows:
        return 0, 0.0, 0.0
    wins = sum(1 for row in rows if int(row.get("won") or 0) == 1)
    win_rate = wins / float(len(rows))
    pnl_sum = sum(float(row.get("pnl") or 0.0) for row in rows)
    return len(rows), win_rate, pnl_sum


def _last_opened_at(conn) -> float:
    row = fetch_one(conn, "SELECT MAX(opened_at) FROM paper_trades")
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except Exception:
        return 0.0


def _equity_drawdown_pct(conn, initial_capital: float) -> float:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl
           FROM paper_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END ASC,
             id ASC""",
    )
    if not rows:
        return 0.0

    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for row in rows:
        equity += float(row.get("pnl") or 0.0)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _window_sample_counts(conn, window_start: int, now_ts: float) -> tuple[int, int]:
    tick_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?""",
        (float(window_start), float(now_ts)),
    )
    odds_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM poly_odds
           WHERE window_start = ? AND ts <= ?""",
        (int(window_start), float(now_ts)),
    )
    tick_cnt = int(tick_row[0] or 0) if tick_row else 0
    odds_cnt = int(odds_row[0] or 0) if odds_row else 0
    return tick_cnt, odds_cnt


def _recent_move_pct(conn, window_start: int, now_ts: float, lookback_sec: float) -> float | None:
    lo_ts = max(float(window_start), float(now_ts) - max(1.0, float(lookback_sec)))
    hi_ts = float(now_ts)
    first_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    last_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts DESC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    if not first_row or not last_row:
        return None
    try:
        p0 = float(first_row[0])
        p1 = float(last_row[0])
        if p0 <= 0.0:
            return None
        return ((p1 - p0) / p0) * 100.0
    except Exception:
        return None


def _price_at_or_near(conn, ts: float, *, prefer_before: bool) -> float | None:
    if prefer_before:
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
    else:
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
    return None


def _backfill_unresolved_windows(conn) -> int:
    now_ts = time.time()
    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, window_end, btc_start_price
           FROM market_windows
           WHERE window_end < ?
             AND (actual_outcome IS NULL OR actual_outcome NOT IN ('UP', 'DOWN'))
           ORDER BY window_start ASC
           LIMIT 40""",
        (now_ts,),
    )
    if not rows:
        return 0

    updated = 0
    for row in rows:
        ws = int(row["window_start"])
        we = int(row["window_end"])

        start_price = float(row["btc_start_price"]) if row.get("btc_start_price") is not None else None
        if start_price is None:
            start_price = _price_at_or_near(conn, float(ws), prefer_before=False)
        end_price = _price_at_or_near(conn, float(we), prefer_before=True)

        if start_price is None or end_price is None:
            continue

        outcome = "UP" if end_price >= start_price else "DOWN"
        execute_write(
            conn,
            """UPDATE market_windows
               SET btc_start_price = COALESCE(btc_start_price, ?),
                   btc_end_price = ?,
                   actual_outcome = ?
               WHERE window_start = ?""",
            (float(start_price), float(end_price), outcome, ws),
        )
        updated += 1

    if updated:
        conn.commit()
        logger.warning("Backfilled %s unresolved window outcomes from btc_ticks", updated)
    return updated


def open_trade_if_signal(
    conn,
    initial_capital: float,
    risk_fraction: float,
    sizing_mode: str,
) -> bool:
    snap = build_snapshot()
    if not snap.get("ok"):
        logger.warning("Snapshot unavailable: %s", snap.get("error", "unknown"))
        return False

    signal = snap.get("signal") or {}
    window = snap.get("window") or {}
    market = snap.get("market") or {}

    actionable = bool(signal.get("actionable"))
    direction = str(signal.get("direction", "NO_TRADE"))
    window_start = window.get("window_start")
    window_end = window.get("window_end")

    if not actionable or direction not in ("UP", "DOWN"):
        return False
    if window_start is None or window_end is None:
        return False
    now_ts = time.time()
    seconds_elapsed = window.get("seconds_elapsed")
    if seconds_elapsed is None:
        return False
    seconds_elapsed = float(seconds_elapsed)
    seconds_remaining = max(0.0, float(window_end) - now_ts)
    if seconds_elapsed < PAPER_ENTRY_START_SEC or seconds_elapsed > PAPER_ENTRY_END_SEC:
        return False
    if seconds_remaining < PAPER_MIN_SECONDS_REMAINING:
        return False

    tick_samples, odds_samples = _window_sample_counts(conn, int(window_start), now_ts)
    if tick_samples < PAPER_MIN_TICK_SAMPLES or odds_samples < PAPER_MIN_ODDS_SAMPLES:
        logger.warning(
            "Skip low sample window ws=%s: ticks=%s/%s odds=%s/%s elapsed=%.1fs",
            window_start,
            tick_samples,
            PAPER_MIN_TICK_SAMPLES,
            odds_samples,
            PAPER_MIN_ODDS_SAMPLES,
            seconds_elapsed,
        )
        return False

    exists = fetch_one(
        conn,
        "SELECT id FROM paper_trades WHERE window_start = ? LIMIT 1",
        (int(window_start),),
    )
    if exists:
        return False

    entry_price = market.get("up_ask") if direction == "UP" else market.get("down_ask")
    if entry_price is None:
        logger.warning("No %s ask price available; skipping trade", direction)
        return False
    entry_price = float(entry_price)
    if entry_price <= 0.0 or entry_price >= 1.0:
        logger.warning("Invalid entry ask %.6f for %s; skipping trade", entry_price, direction)
        return False

    btc_now = market.get("btc_price")
    btc_start = market.get("btc_start_price")
    sec_elapsed = window.get("seconds_elapsed")
    judges = signal.get("judges") or []
    if btc_now is None or btc_start is None or sec_elapsed is None:
        logger.warning("Missing market context for entry gate; skipping trade")
        return False
    support_votes = sum(1 for j in judges if str(j.get("vote")) == direction)
    support_ratio = (support_votes / float(len(judges))) if judges else 0.0
    confidence = float(signal.get("avg_confidence") or 0.0)
    recent_move_pct = _recent_move_pct(
        conn,
        int(window_start),
        now_ts=now_ts,
        lookback_sec=PAPER_RECENT_MOVE_LOOKBACK_SEC,
    )
    if recent_move_pct is None:
        return False
    if direction == "UP" and recent_move_pct < PAPER_MIN_RECENT_MOVE_PCT:
        logger.warning(
            "Skip weak short momentum ws=%s dir=%s: move=%.4f%% < +%.4f%%",
            window_start,
            direction,
            recent_move_pct,
            PAPER_MIN_RECENT_MOVE_PCT,
        )
        return False
    if direction == "DOWN" and recent_move_pct > -PAPER_MIN_RECENT_MOVE_PCT:
        logger.warning(
            "Skip weak short momentum ws=%s dir=%s: move=%.4f%% > -%.4f%%",
            window_start,
            direction,
            recent_move_pct,
            PAPER_MIN_RECENT_MOVE_PCT,
        )
        return False

    gate = evaluate_entry_gate(
        direction=direction,
        entry_price=float(entry_price),
        current_price=float(btc_now),
        start_price=float(btc_start),
        seconds_elapsed=float(sec_elapsed),
        jury_confidence=confidence,
        support_ratio=float(support_ratio),
    )
    if not gate.allow:
        logger.warning("Entry gate blocked ws=%s dir=%s: %s", window_start, direction, gate.reason)
        return False
    loss_streak, recent_loss_rate = _recent_risk_state(conn)
    perf_count, perf_wr, perf_pnl = _recent_performance(conn, PAPER_RECENT_PERF_WINDOW)
    realized_equity, available_equity = _equity_snapshot(conn, initial_capital)
    drawdown_pct = _equity_drawdown_pct(conn, initial_capital)

    # Adaptive strictness in [0, 1]: increases after losses/drawdown/weak performance.
    strictness = 0.0
    strictness += min(0.45, loss_streak * 0.15)
    strictness += max(0.0, recent_loss_rate - 0.50) * 0.70
    strictness += min(0.40, drawdown_pct * 0.80)
    if perf_count >= 4 and perf_pnl < 0.0:
        strictness += 0.10
    strictness = _clamp(strictness, 0.0, 1.0)

    adaptive_min_ev = PAPER_MIN_EXPECTED_ROI * (1.0 + 0.9 * strictness) + min(loss_streak, 3) * 0.005
    adaptive_min_support = _clamp(PAPER_MIN_SUPPORT_RATIO + 0.20 * strictness, PAPER_MIN_SUPPORT_RATIO, 1.0)
    adaptive_min_conf = _clamp(PAPER_MIN_CONFIDENCE + 0.18 * strictness, PAPER_MIN_CONFIDENCE, 0.80)
    adaptive_max_ask = _clamp(PAPER_MAX_ENTRY_PRICE - 0.08 * strictness, 0.45, PAPER_MAX_ENTRY_PRICE)

    dynamic_gap = PAPER_BASE_TRADE_GAP_SEC + strictness * (
        max(PAPER_BASE_TRADE_GAP_SEC, PAPER_TARGET_TRADE_GAP_SEC) - PAPER_BASE_TRADE_GAP_SEC
    )
    dynamic_gap = max(0.0, dynamic_gap)
    last_opened_at = _last_opened_at(conn)
    since_last = (now_ts - last_opened_at) if last_opened_at > 0 else 999999.0
    high_quality_override = (
        gate.expected_roi >= PAPER_HIGH_QUALITY_EV
        and confidence >= PAPER_HIGH_QUALITY_CONF
        and support_ratio >= max(adaptive_min_support, 0.80)
    )
    if since_last < dynamic_gap and not high_quality_override:
        return False

    if gate.expected_roi < adaptive_min_ev:
        logger.warning(
            "Skip weak EV ws=%s dir=%s: net_ev=%+.3f%% < %.3f%% (loss_streak=%s, recent_loss_rate=%.0f%%)",
            window_start,
            direction,
            gate.expected_roi * 100.0,
            adaptive_min_ev * 100.0,
            loss_streak,
            recent_loss_rate * 100.0,
        )
        return False
    if support_ratio < adaptive_min_support:
        logger.warning(
            "Skip weak jury ws=%s dir=%s: support=%.1f%% < %.1f%%",
            window_start,
            direction,
            support_ratio * 100.0,
            adaptive_min_support * 100.0,
        )
        return False
    require_unanimous = PAPER_REQUIRE_UNANIMOUS or strictness >= 0.65
    if require_unanimous and support_ratio < 1.0:
        logger.warning(
            "Skip non-unanimous ws=%s dir=%s: strictness=%.2f support=%.1f%%",
            window_start,
            direction,
            strictness,
            support_ratio * 100.0,
        )
        return False
    if confidence < adaptive_min_conf:
        logger.warning(
            "Skip low confidence ws=%s dir=%s: conf=%.3f < %.3f",
            window_start,
            direction,
            confidence,
            adaptive_min_conf,
        )
        return False
    if entry_price > adaptive_max_ask:
        logger.warning(
            "Skip expensive entry ws=%s dir=%s: ask=%.3f > %.3f",
            window_start,
            direction,
            entry_price,
            adaptive_max_ask,
        )
        return False

    if realized_equity <= (initial_capital * (1.0 - PAPER_MAX_DRAWDOWN_STOP_PCT)):
        logger.warning(
            "Risk stop active: equity=$%.2f below stop level $%.2f",
            realized_equity,
            initial_capital * (1.0 - PAPER_MAX_DRAWDOWN_STOP_PCT),
        )
        return False

    if perf_count >= max(4, PAPER_RECENT_PERF_WINDOW) and perf_wr < PAPER_MIN_RECENT_WIN_RATE and perf_pnl < 0.0:
        logger.warning(
            "Pause by weak recent performance: trades=%s wr=%.1f%% pnl=$%+.2f",
            perf_count,
            perf_wr * 100.0,
            perf_pnl,
        )
        return False

    if sizing_mode == "all_in_fixed":
        stake = round(initial_capital, 2)
    elif sizing_mode == "all_in_equity":
        stake = round(max(0.0, available_equity), 2)
    else:
        if available_equity < PAPER_MIN_BET:
            logger.warning(
                "Insufficient available equity ws=%s: available=$%.2f",
                window_start,
                available_equity,
            )
            return False

        stake = _compute_bet_size(
            available_equity=available_equity,
            initial_capital=initial_capital,
            expected_roi=gate.expected_roi,
            risk_fraction=risk_fraction,
        )
        if loss_streak > 0:
            # Adaptive de-risking after losses.
            stake = round(stake * (0.65 ** min(loss_streak, 3)), 2)
    if stake < PAPER_MIN_BET:
        logger.warning("Computed bet too small ws=%s: $%.2f", window_start, stake)
        return False

    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
    opened_at = time.time()
    conf = confidence
    reason = str(signal.get("reason") or "")
    if reason:
        reason = f"{reason} | {gate.reason}"
    else:
        reason = gate.reason

    execute_write(
        conn,
        """INSERT INTO paper_trades
           (window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
            potential_win_pnl, signal_confidence, signal_reason, initial_capital, risk_fraction, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
        (
            int(window_start),
            int(window_end),
            direction,
            float(stake),
            entry_price,
            payout_multiple,
            shares,
            potential_win_pnl,
            conf,
            reason,
            float(initial_capital),
            float(risk_fraction),
            opened_at,
        ),
    )
    conn.commit()

    logger.warning(
        "OPEN  ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% eq=$%.2f avail=$%.2f strict=%.2f gap=%.0fs mode=%s",
        window_start,
        direction,
        stake,
        entry_price,
        gate.expected_roi * 100.0,
        realized_equity,
        available_equity,
        strictness,
        dynamic_gap,
        sizing_mode,
    )
    return True


def resolve_open_trades(conn) -> int:
    _backfill_unresolved_windows(conn)

    open_rows = fetch_all_dicts(
        conn,
        """SELECT id, window_start, direction, stake, shares
           FROM paper_trades
           WHERE status = 'OPEN'
           ORDER BY window_start ASC""",
    )
    if not open_rows:
        return 0

    resolved = 0
    for row in open_rows:
        ws = int(row["window_start"])
        outcome_row = fetch_one(
            conn,
            "SELECT actual_outcome FROM market_windows WHERE window_start = ?",
            (ws,),
        )
        if not outcome_row:
            continue
        outcome = outcome_row[0]
        if outcome not in ("UP", "DOWN"):
            continue

        direction = str(row["direction"])
        stake = float(row["stake"])
        shares = float(row["shares"])
        won = 1 if outcome == direction else 0
        raw_pnl = (shares - stake) if won else (-stake)
        pnl = apply_fee_to_pnl(raw_pnl, stake)
        roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
        closed_at = time.time()

        execute_write(
            conn,
            """UPDATE paper_trades
               SET status='CLOSED',
                   closed_at=?,
                   actual_outcome=?,
                   won=?,
                   pnl=?,
                   roi_pct=?
               WHERE id=?""",
            (closed_at, outcome, won, pnl, roi_pct, int(row["id"])),
        )
        resolved += 1

        if pnl > 0:
            logger.warning(
                "PROFIT ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%%",
                ws,
                direction,
                outcome,
                pnl,
                roi_pct,
            )
        elif pnl < 0:
            logger.warning(
                "LOSS   ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%%",
                ws,
                direction,
                outcome,
                pnl,
                roi_pct,
            )
        else:
            logger.warning(
                "FLAT   ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%%",
                ws,
                direction,
                outcome,
                pnl,
                roi_pct,
            )

    if resolved:
        conn.commit()
    return resolved


def show_status(conn):
    try:
        cap_row = fetch_one(
            conn,
            "SELECT initial_capital FROM paper_trades WHERE initial_capital IS NOT NULL ORDER BY window_start ASC LIMIT 1",
        )
    except Exception:
        cap_row = None
    if cap_row and cap_row[0] is not None:
        initial_capital = float(cap_row[0])
    else:
        initial_capital = 1000.0

    stats = fetch_one(
        conn,
        """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_cnt,
               SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_cnt,
               SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END) AS losses,
               COALESCE(SUM(pnl), 0) AS total_pnl
           FROM paper_trades""",
    )

    total = int(stats[0] or 0)
    open_cnt = int(stats[1] or 0)
    closed_cnt = int(stats[2] or 0)
    wins = int(stats[3] or 0)
    losses = int(stats[4] or 0)
    total_pnl = float(stats[5] or 0.0)
    win_rate = (wins / closed_cnt * 100.0) if closed_cnt > 0 else 0.0
    equity = initial_capital + total_pnl

    print(f"""
{'='*64}
 PAPER TRADE STATUS
{'='*64}
 DB:          {db_label()}
 Seed Cap:    ${initial_capital:.2f}
 Equity:      ${equity:+.2f}
 Total:       {total}
 Open:        {open_cnt}
 Closed:      {closed_cnt}
 Wins/Losses: {wins}/{losses}  (WR={win_rate:.1f}%)
 Total PnL:   ${total_pnl:+.2f}
{'='*64}
""")

    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, direction, stake, entry_price, payout_multiple,
                  potential_win_pnl, status, actual_outcome, pnl, roi_pct
           FROM paper_trades
           ORDER BY window_start DESC
           LIMIT 12""",
    )
    if rows:
        print("Recent trades:")
        for r in rows:
            ws = int(r["window_start"])
            dt = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            status = str(r["status"])
            pnl = r["pnl"]
            pnl_s = f"${float(pnl):+.2f}" if pnl is not None else "-"
            roi = r["roi_pct"]
            roi_s = f"{float(roi):+.2f}%" if roi is not None else "-"
            print(
                f"  {dt} | {status:6s} | {r['direction']:4s} | "
                f"ask={float(r['entry_price']):.4f} payx={float(r['payout_multiple']):.3f} | "
                f"potWin={float(r['potential_win_pnl']):+.2f} | pnl={pnl_s} roi={roi_s}"
            )


def run_loop(stake: float, interval_sec: float, sizing_mode: str):
    conn = connect_db()
    init_paper_table(conn)

    initial_capital = max(50.0, float(stake))
    risk_fraction = _clamp(PAPER_RISK_FRACTION, 0.01, 1.0)
    mode = str(sizing_mode or "adaptive").strip().lower()
    if mode not in ("adaptive", "all_in_fixed", "all_in_equity"):
        mode = "adaptive"
    logger.warning(
        "Paper simulator running: initial=$%.2f mode=%s risk_frac=%.2f base_ev=%.2f%% min_support=%.0f%% max_ask=%.2f "
        "entry=%.0f~%.0fs remain>=%.0fs samples(t/o)=%d/%d gap=%.0f~%.0fs unanim=%s",
        initial_capital,
        mode,
        risk_fraction,
        PAPER_MIN_EXPECTED_ROI * 100.0,
        PAPER_MIN_SUPPORT_RATIO * 100.0,
        PAPER_MAX_ENTRY_PRICE,
        PAPER_ENTRY_START_SEC,
        PAPER_ENTRY_END_SEC,
        PAPER_MIN_SECONDS_REMAINING,
        PAPER_MIN_TICK_SAMPLES,
        PAPER_MIN_ODDS_SAMPLES,
        PAPER_BASE_TRADE_GAP_SEC,
        PAPER_TARGET_TRADE_GAP_SEC,
        PAPER_REQUIRE_UNANIMOUS,
    )

    try:
        while True:
            resolve_open_trades(conn)
            open_trade_if_signal(
                conn,
                initial_capital=initial_capital,
                risk_fraction=risk_fraction,
                sizing_mode=mode,
            )
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("Paper simulator stopped by user")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Live paper-trading simulator")
    parser.add_argument("--stake", type=float, default=1000.0, help="Paper seed capital in USD (default: 1000)")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds (default: 2)")
    parser.add_argument(
        "--sizing-mode",
        type=str,
        default=PAPER_SIZING_MODE,
        help="adaptive | all_in_fixed | all_in_equity",
    )
    parser.add_argument("--status", action="store_true", help="Show paper trade status")
    args = parser.parse_args()

    conn = connect_db()
    init_paper_table(conn)
    conn.close()

    if args.status:
        conn = connect_db()
        show_status(conn)
        conn.close()
        return

    run_loop(
        stake=float(args.stake),
        interval_sec=float(args.interval),
        sizing_mode=str(args.sizing_mode),
    )


if __name__ == "__main__":
    main()
