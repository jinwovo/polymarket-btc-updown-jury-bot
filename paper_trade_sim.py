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


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("paper_sim")


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
    conn.commit()


def open_trade_if_signal(conn, stake: float) -> bool:
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

    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = shares - stake
    opened_at = time.time()
    conf = float(signal.get("avg_confidence") or 0.0)
    reason = str(signal.get("reason") or "")

    execute_write(
        conn,
        """INSERT INTO paper_trades
           (window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
            potential_win_pnl, signal_confidence, signal_reason, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
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
            opened_at,
        ),
    )
    conn.commit()

    # Intentionally quiet on open; report only profitable closes.
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
        pnl = (shares - stake) if won else (-stake)
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

    print(f"""
{'='*64}
 PAPER TRADE STATUS
{'='*64}
 DB:          {db_label()}
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

    logger.warning("Paper simulator running (quiet mode): only PROFIT and errors will be printed")

    try:
        while True:
            resolve_open_trades(conn)
            open_trade_if_signal(conn, stake=stake)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("Paper simulator stopped by user")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Live paper-trading simulator")
    parser.add_argument("--stake", type=float, default=1000.0, help="Virtual stake per trade (default: 1000)")
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
