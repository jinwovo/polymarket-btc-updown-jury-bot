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
PAPER_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.030"))
PAPER_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.80"))
PAPER_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.28"))
PAPER_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.62"))
PAPER_MIN_BET = float(os.getenv("PAPER_MIN_BET", "25"))
PAPER_MAX_BET_FRAC = float(os.getenv("PAPER_MAX_BET_FRAC", "0.20"))
PAPER_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "20"))
PAPER_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "220"))


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


def open_trade_if_signal(conn, initial_capital: float, risk_fraction: float) -> bool:
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
    seconds_elapsed = window.get("seconds_elapsed")
    if seconds_elapsed is None:
        return False
    seconds_elapsed = float(seconds_elapsed)
    if seconds_elapsed < PAPER_ENTRY_START_SEC or seconds_elapsed > PAPER_ENTRY_END_SEC:
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
    gate = evaluate_entry_gate(
        direction=direction,
        entry_price=float(entry_price),
        current_price=float(btc_now),
        start_price=float(btc_start),
        seconds_elapsed=float(sec_elapsed),
        jury_confidence=float(signal.get("avg_confidence") or 0.0),
        support_ratio=float(support_ratio),
    )
    if not gate.allow:
        logger.warning("Entry gate blocked ws=%s dir=%s: %s", window_start, direction, gate.reason)
        return False
    loss_streak, recent_loss_rate = _recent_risk_state(conn)
    adaptive_min_ev = PAPER_MIN_EXPECTED_ROI + min(loss_streak, 3) * 0.01
    if recent_loss_rate >= 0.60:
        adaptive_min_ev += 0.005

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
    if support_ratio < PAPER_MIN_SUPPORT_RATIO:
        logger.warning(
            "Skip weak jury ws=%s dir=%s: support=%.1f%% < %.1f%%",
            window_start,
            direction,
            support_ratio * 100.0,
            PAPER_MIN_SUPPORT_RATIO * 100.0,
        )
        return False
    if loss_streak >= 2 and support_ratio < 1.0:
        logger.warning(
            "Skip non-unanimous after losses ws=%s dir=%s: streak=%s support=%.1f%%",
            window_start,
            direction,
            loss_streak,
            support_ratio * 100.0,
        )
        return False
    confidence = float(signal.get("avg_confidence") or 0.0)
    if confidence < PAPER_MIN_CONFIDENCE:
        logger.warning(
            "Skip low confidence ws=%s dir=%s: conf=%.3f < %.3f",
            window_start,
            direction,
            confidence,
            PAPER_MIN_CONFIDENCE,
        )
        return False
    if entry_price > PAPER_MAX_ENTRY_PRICE:
        logger.warning(
            "Skip expensive entry ws=%s dir=%s: ask=%.3f > %.3f",
            window_start,
            direction,
            entry_price,
            PAPER_MAX_ENTRY_PRICE,
        )
        return False

    realized_equity, available_equity = _equity_snapshot(conn, initial_capital)
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
        "OPEN  ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% eq=$%.2f avail=$%.2f streak=%s",
        window_start,
        direction,
        stake,
        entry_price,
        gate.expected_roi * 100.0,
        realized_equity,
        available_equity,
        loss_streak,
    )
    return True


def resolve_open_trades(conn) -> int:
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


def run_loop(stake: float, interval_sec: float):
    conn = connect_db()
    init_paper_table(conn)

    initial_capital = max(50.0, float(stake))
    risk_fraction = _clamp(PAPER_RISK_FRACTION, 0.01, 1.0)
    logger.warning(
        "Paper simulator running: initial=$%.2f risk_frac=%.2f min_ev=%.2f%% min_support=%.0f%% max_ask=%.2f",
        initial_capital,
        risk_fraction,
        PAPER_MIN_EXPECTED_ROI * 100.0,
        PAPER_MIN_SUPPORT_RATIO * 100.0,
        PAPER_MAX_ENTRY_PRICE,
    )

    try:
        while True:
            resolve_open_trades(conn)
            open_trade_if_signal(conn, initial_capital=initial_capital, risk_fraction=risk_fraction)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("Paper simulator stopped by user")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Live paper-trading simulator")
    parser.add_argument("--stake", type=float, default=1000.0, help="Paper seed capital in USD (default: 1000)")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds (default: 2)")
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

    run_loop(stake=float(args.stake), interval_sec=float(args.interval))


if __name__ == "__main__":
    main()
