"""Walk-forward specialist-mirror backtest on the harvested tape (pre-registered filter).

Reproduces the 2026-08-10 validated result, then extends to freshly harvested days.
Pre-registered specialist filter (selection strictly BEFORE each UTC test day):
  - 10 <= windows in trailing 7d <= 210   (active but not an HFT grinder)
  - median per-window buy cost (7d) >= $100
  - median first-buy entry time (7d) >= 60s
  - last window traded within 48h
  - rank by recency-decay PnL (half-life 5d, all history), take top-K (default 5)
Mirror rule: earliest first-BUY among chosen wallets per window, elapsed <= 240s,
px <= 0.92, entry = px + slip (default +2c), $10 stake, 3% taker fee on wins.

Usage: python scripts/tape_specialist_mirror.py [--asset btc5] [--k 5] [--slip 0.02]
       [--max-ws EPOCH]   restrict tape to windows <= EPOCH (exact reproduction)
       [--baseline]       skip specialist constraints (decay-score top-K only)
"""
import argparse
import datetime
import math
import os
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(".env.secrets")
import pymysql

FEE = 0.03
STAKE = 10.0
HALF_LIFE_DAYS = 5.0
VALIDATION_CUTOFF_WS = 1786259700  # 2026-08-10 07:15 UTC = last window of the validated 30d harvest


def day_of(ws):
    return datetime.datetime.fromtimestamp(ws, datetime.timezone.utc).strftime("%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc5")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--slip", type=float, default=0.02)
    ap.add_argument("--max-ws", type=int, default=0)
    ap.add_argument("--px-cap", type=float, default=0.92)
    ap.add_argument("--el-cap", type=float, default=240.0)
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--warmup-days", type=int, default=7)
    ap.add_argument("--wallet-day-cap", type=int, default=0,
                    help="stop mirroring a wallet after it has entered N windows today "
                         "(profile-break guard; 0 = off). Uses only intraday-observable info.")
    args = ap.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=3400, user="root",
                           password=os.getenv("MARIADB_PASSWORD", ""),
                           database="polymarket_btc_updown")
    cur = conn.cursor()
    ws_cond = f" AND window_start <= {int(args.max_ws)}" if args.max_ws else ""
    cur.execute(f"SELECT window_start, outcome FROM tape_windows "
                f"WHERE asset=%s AND outcome IN ('UP','DOWN'){ws_cond}", (args.asset,))
    outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
    print(f"resolved windows: {len(outcomes):,}")

    # per (wallet, window) aggregates in SQL -- avoids loading millions of prints
    cur.execute(f"""
        SELECT wallet, window_start,
               SUM(CASE WHEN side='BUY' THEN -price*size ELSE price*size END),
               SUM(CASE WHEN UPPER(outcome_side)='UP'
                        THEN (CASE WHEN side='BUY' THEN size ELSE -size END) ELSE 0 END),
               SUM(CASE WHEN UPPER(outcome_side)='DOWN'
                        THEN (CASE WHEN side='BUY' THEN size ELSE -size END) ELSE 0 END),
               SUM(CASE WHEN side='BUY' THEN price*size ELSE 0 END),
               COUNT(*)
        FROM poly_trades WHERE asset=%s{ws_cond}
        GROUP BY wallet, window_start""", (args.asset,))
    agg = cur.fetchall()
    print(f"wallet-windows: {len(agg):,}")

    try:
        cur.execute(f"""
            SELECT wallet, window_start, ts, outcome_side, price FROM (
                SELECT wallet, window_start, ts, outcome_side, price,
                       ROW_NUMBER() OVER (PARTITION BY wallet, window_start ORDER BY ts, id) rn
                FROM poly_trades WHERE asset=%s AND side='BUY'{ws_cond}) t
            WHERE rn=1""", (args.asset,))
        fb_rows = cur.fetchall()
    except Exception:
        cur.execute(f"""
            SELECT p.wallet, p.window_start, p.ts, MIN(p.outcome_side), MIN(p.price)
            FROM poly_trades p JOIN (
                SELECT wallet, window_start, MIN(ts) mts FROM poly_trades
                WHERE asset=%s AND side='BUY'{ws_cond} GROUP BY wallet, window_start) m
              ON p.wallet=m.wallet AND p.window_start=m.window_start AND p.ts=m.mts
            WHERE p.asset=%s AND p.side='BUY'
            GROUP BY p.wallet, p.window_start, p.ts""", (args.asset, args.asset))
        fb_rows = cur.fetchall()
    first_buy = {(r[0], int(r[1])): (float(r[2]), str(r[3]).upper(), float(r[4])) for r in fb_rows}
    cur.close()
    conn.close()

    # settle pnl per wallet-window; exclude phantom (negative final balance = mint/merge seller)
    wallet_windows = defaultdict(list)  # w -> [(ws, pnl, fb_ts, fb_side, fb_px, buy_cost)]
    phantom = 0
    for w, ws, cash, up_sh, dn_sh, buy_cost, n in agg:
        ws = int(ws)
        if ws not in outcomes:
            continue
        cash, up_sh, dn_sh, buy_cost = float(cash), float(up_sh), float(dn_sh), float(buy_cost)
        if up_sh < -0.01 or dn_sh < -0.01:
            phantom += 1
            continue
        settle = up_sh * (1.0 if outcomes[ws] == "UP" else 0.0) + \
                 dn_sh * (1.0 if outcomes[ws] == "DOWN" else 0.0)
        fb = first_buy.get((w, ws))
        wallet_windows[w].append((ws, cash + settle, fb[0] if fb else None,
                                  fb[1] if fb else None, fb[2] if fb else None, buy_cost))
    print(f"phantom excluded: {phantom:,}   wallets: {len(wallet_windows):,}")

    for w in wallet_windows:
        wallet_windows[w].sort()

    def decay_score(w, asof_ts):
        s = 0.0
        for row in wallet_windows[w]:
            if row[0] >= asof_ts:
                break
            s += row[1] * math.pow(0.5, (asof_ts - row[0]) / 86400.0 / HALF_LIFE_DAYS)
        return s

    def is_specialist(w, asof_ts):
        wk = [r for r in wallet_windows[w] if asof_ts - 7 * 86400 <= r[0] < asof_ts]
        if not (10 <= len(wk) <= 210):
            return False
        last = 0
        for r in wallet_windows[w]:
            if r[0] < asof_ts:
                last = r[0]
            else:
                break
        if asof_ts - last > 48 * 3600:
            return False
        if median([r[5] for r in wk]) < 100.0:
            return False
        entries = [r[2] - r[0] for r in wk if r[2] is not None]
        if not entries or median(entries) < 60.0:
            return False
        return True

    def is_eligible_baseline(w, asof_ts):
        wk = [r for r in wallet_windows[w] if asof_ts - 7 * 86400 <= r[0] < asof_ts]
        if len(wk) < 10:
            return False
        last = 0
        for r in wallet_windows[w]:
            if r[0] < asof_ts:
                last = r[0]
            else:
                break
        return asof_ts - last <= 48 * 3600

    # ---- walk-forward ----
    days = sorted({day_of(ws) for ws in outcomes})
    test_days = days[args.warmup_days:]
    mode = "BASELINE top-%d (no specialist filter)" % args.k if args.baseline else \
           "SPECIALIST top-%d" % args.k
    print(f"\n=== WALK-FORWARD MIRROR [{mode}] slip=+{args.slip:.2f} "
          f"el<={args.el_cap:.0f}s px<={args.px_cap:.2f} ${STAKE:.0f} ===")
    windows_by_day = defaultdict(list)
    for ws in outcomes:
        windows_by_day[day_of(ws)].append(ws)

    total = [0, 0, 0.0]
    day_results = []
    all_trades = []
    for d in test_days:
        day_start = datetime.datetime.strptime(f"2026-{d}", "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        picks = []
        for w in wallet_windows:
            ok = is_eligible_baseline(w, day_start) if args.baseline else is_specialist(w, day_start)
            if ok:
                picks.append((decay_score(w, day_start), w))
        picks.sort(reverse=True)
        chosen = [w for _, w in picks[:args.k]]
        chosen_set = set(chosen)

        n, nw, pnl_d = 0, 0, 0.0
        day_activity = defaultdict(int)  # wallet -> windows entered so far today
        for ws in sorted(windows_by_day[d]):
            best = None  # earliest first-buy among chosen
            for w in chosen_set:
                fb = first_buy.get((w, ws))
                if fb is None:
                    continue
                # phantom check: that wallet-window must have survived exclusion
                if not any(r[0] == ws for r in wallet_windows[w]):
                    continue
                if args.wallet_day_cap:
                    day_activity[w] += 1
                    if day_activity[w] > args.wallet_day_cap:
                        continue
                if best is None or fb[0] < best[0]:
                    best = fb
            if best is None:
                continue
            fb_ts, fb_side, fb_px = best
            el = fb_ts - ws
            if el > args.el_cap or fb_px > args.px_cap:
                continue
            ep = min(fb_px + args.slip, 0.99)
            won = outcomes[ws] == fb_side
            pnl_t = (STAKE / ep - STAKE) * (1 - FEE) if won else -STAKE
            n += 1
            nw += int(won)
            pnl_d += pnl_t
            all_trades.append((ws, d, fb_px, ep, el, fb_side, won, pnl_t))
        total[0] += n
        total[1] += nw
        total[2] += pnl_d
        day_results.append((d, len(chosen), n, nw, pnl_d))
        fresh = "  [FRESH OOS]" if day_start > VALIDATION_CUTOFF_WS else ""
        wrs = f"{100.0*nw/n:.0f}%" if n else "-"
        print(f"  {d}: picks={len(chosen)}  n={n:<4} WR={wrs:<5} PnL=${pnl_d:+8.2f}{fresh}")

    n, nw, pnl = total
    if n:
        print(f"\n  TOTAL: {n}t  WR={100.0*nw/n:.1f}%  PnL=${pnl:+.2f}  EV/t=${pnl/n:+.2f}  "
              f"({len(test_days)} test days, {n/max(len(test_days),1):.1f} t/day)")
        pos_days = sum(1 for r in day_results if r[4] > 0)
        print(f"  positive days: {pos_days}/{len([r for r in day_results if r[2] > 0])} (of days with trades)")
        nb = max(1, (len(day_results) + 5) // 6)
        print("  week buckets: ", end="")
        for i in range(0, len(day_results), 6):
            b = day_results[i:i+6]
            print(f"[{b[0][0]}..{b[-1][0]} ${sum(x[4] for x in b):+.0f}] ", end="")
        print()
        # entry price bands
        bands = defaultdict(lambda: [0, 0, 0.0])
        for ws, d, px, ep, el, side, won, pnl_t in all_trades:
            b = "px<0.30" if px < 0.30 else ("0.30-0.60" if px < 0.60 else "0.60+")
            bands[b][0] += 1; bands[b][1] += int(won); bands[b][2] += pnl_t
        for b in sorted(bands):
            s = bands[b]
            print(f"  band {b:<10} n={s[0]:<4} WR={100.0*s[1]/s[0]:5.1f}%  PnL=${s[2]:+8.2f}  EV/t=${s[2]/s[0]:+.2f}")
    else:
        print("  no trades")


if __name__ == "__main__":
    main()
