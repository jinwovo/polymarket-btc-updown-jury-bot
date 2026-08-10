"""Live fill slippage report: realized entry_price vs signal ask at entry time.

Purpose: gate for the stake-scaling ladder ($10 -> $25 -> $50 -> $100).
Per CLAUDE.md (2026-07-04): up to $22 stakes, mean slip +0.7c / median 0c /
p90 +5c, no stake-size effect. Re-run this after every stake step; if p90
slip stays <= +5c and mean <= +1.5c, the next step is safe.

Usage: python scripts/slippage_report.py [--days 30]
ASCII-only output (cp949 console).
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402,F401  (loads .env.secrets before any DB call)
from db_config import connect_db  # noqa: E402

MARKETS = [
    ("btc5", "live_trades", "signal_cache_log"),
    ("eth5", "live_trades_eth5", "signal_cache_log_eth5"),
    ("btc15", "live_trades_btc15", None),  # btc15 has no signal log table mapped
]
STAKE_BUCKETS = [(0, 12.5), (12.5, 30), (30, 60), (60, 1e9)]


def to_epoch(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).timestamp()
        return v.timestamp()
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def pctl(sorted_vals, p):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def report(conn, market, trades_table, sig_table, days):
    cur = conn.cursor()
    since = time.time() - days * 86400
    cur.execute(
        f"SELECT window_start, direction, stake, entry_price, opened_at, won, pnl "
        f"FROM {trades_table} WHERE entry_price IS NOT NULL ORDER BY id DESC LIMIT 2000"
    )
    rows = cur.fetchall()
    samples = []
    for ws, direction, stake, entry_price, opened_at, won, pnl in rows:
        opened_ep = to_epoch(opened_at)
        if opened_ep is None or opened_ep < since:
            continue
        if sig_table is None:
            continue
        cur.execute(
            f"SELECT up_ask, down_ask FROM {sig_table} "
            f"WHERE window_start = %s AND ts <= %s ORDER BY ts DESC LIMIT 1",
            (int(ws), opened_ep + 2.0),
        )
        sig = cur.fetchone()
        if not sig:
            continue
        sig_ask = float(sig[0]) if str(direction).upper() == "UP" else float(sig[1])
        if sig_ask <= 0:
            continue
        samples.append({
            "stake": float(stake or 0),
            "slip": float(entry_price) - sig_ask,
            "won": won,
            "pnl": float(pnl or 0),
        })

    print("=" * 62)
    print("%s: %d matched live trades in last %dd" % (market.upper(), len(samples), days))
    if not samples:
        return
    slips = sorted(s["slip"] for s in samples)
    mean = sum(slips) / len(slips)
    at_or_below = sum(1 for s in slips if s <= 0.0001) / len(slips) * 100
    print("  slip vs signal ask: mean %+.4f | median %+.4f | p90 %+.4f | at-or-below %0.f%%"
          % (mean, pctl(slips, 50), pctl(slips, 90), at_or_below))
    for lo, hi in STAKE_BUCKETS:
        b = sorted(s["slip"] for s in samples if lo < s["stake"] <= hi)
        if not b:
            continue
        label = "$%g-%g" % (lo, hi) if hi < 1e8 else "$%g+" % lo
        print("  stake %-9s n=%-4d mean %+.4f  p90 %+.4f"
              % (label, len(b), sum(b) / len(b), pctl(b, 90)))
    wins = [s for s in samples if s["won"] in (1, True)]
    total_pnl = sum(s["pnl"] for s in samples)
    print("  WR %.1f%% | total pnl $%+.2f" % (100.0 * len(wins) / len(samples), total_pnl))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    conn = connect_db()
    for market, trades_table, sig_table in MARKETS:
        try:
            report(conn, market, trades_table, sig_table, args.days)
        except Exception as e:
            print("%s: ERROR %r" % (market, e))
    conn.close()


if __name__ == "__main__":
    main()
