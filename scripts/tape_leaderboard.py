"""Smart-money leaderboard + walk-forward mirror backtest from the harvested tape.

Recency-first design: decay-weighted PnL (half-life 5 days), wallets must have
traded within the last 48h and >=15 trades in the trailing 7 days. Walk-forward:
top-K selected only from data strictly BEFORE each test day.

Usage: python scripts/tape_leaderboard.py [--asset btc5] [--top 25] [--k 5]
"""
import argparse
import datetime
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(".env.secrets")
import pymysql

FEE = 0.03
STAKE = 10.0
HALF_LIFE_DAYS = 5.0


def day_of(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc5")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=3400, user="root",
                           password=os.getenv("MARIADB_PASSWORD", ""),
                           database="polymarket_btc_updown")
    cur = conn.cursor()
    cur.execute("SELECT window_start, outcome FROM tape_windows WHERE asset=%s AND outcome IN ('UP','DOWN')",
                (args.asset,))
    outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
    print(f"resolved windows: {len(outcomes):,}")

    cur.execute("SELECT ts, window_start, wallet, pseudonym, side, outcome_side, size, price "
                "FROM poly_trades WHERE asset=%s ORDER BY ts", (args.asset,))
    rows = cur.fetchall()
    print(f"trades: {len(rows):,}")
    cur.close()

    now_ts = max(float(r[0]) for r in rows) if rows else 0

    # ---- per (wallet, window) position accounting ----
    pos = {}
    names = {}
    for ts, ws, w, pseudo, side, osd, sz, px in rows:
        ws = int(ws)
        if ws not in outcomes:
            continue
        key = (w, ws)
        p = pos.get(key)
        if p is None:
            p = pos[key] = {"cash": 0.0, "up": 0.0, "down": 0.0, "first_buy": None,
                            "buy_cost": 0.0, "n": 0}
        sz = float(sz); px = float(px); ts = float(ts)
        sgn = 1.0 if side == "BUY" else -1.0
        p["cash"] -= sgn * px * sz
        if osd.upper() == "UP":
            p["up"] += sgn * sz
        else:
            p["down"] += sgn * sz
        p["n"] += 1
        if side == "BUY":
            p["buy_cost"] += px * sz
            if p["first_buy"] is None:
                p["first_buy"] = (ts, osd.upper(), px, sz)
        names[w] = pseudo

    # ---- wallet-day pnl (exclude phantom: negative final share balance = out-of-tape acquisition) ----
    wallet_day = defaultdict(lambda: defaultdict(float))       # wallet -> day -> pnl
    wallet_windows = defaultdict(list)                          # wallet -> [(ws, pnl, won)]
    wallet_last_ts = defaultdict(float)
    wallet_trades7 = defaultdict(int)
    phantom = 0
    for (w, ws), p in pos.items():
        if p["up"] < -0.01 or p["down"] < -0.01:
            phantom += 1
            continue
        settle = p["up"] * (1.0 if outcomes[ws] == "UP" else 0.0) + \
                 p["down"] * (1.0 if outcomes[ws] == "DOWN" else 0.0)
        pnl = p["cash"] + settle
        d = day_of(ws)
        wallet_day[w][d] += pnl
        wallet_windows[w].append((ws, pnl, pnl > 0))
        if p["first_buy"]:
            wallet_last_ts[w] = max(wallet_last_ts[w], p["first_buy"][0])
            if now_ts - ws <= 7 * 86400:
                wallet_trades7[w] += p["n"]
    print(f"phantom wallet-windows excluded (mint/merge sellers): {phantom:,}")

    # ---- recency-decay score + filters ----
    def decay_score(w, asof_ts):
        s = 0.0
        for ws, pnl, _ in wallet_windows[w]:
            if ws >= asof_ts:
                continue
            age_d = (asof_ts - ws) / 86400.0
            s += pnl * math.pow(0.5, age_d / HALF_LIFE_DAYS)
        return s

    def eligible(w, asof_ts):
        recent = [x for x in wallet_windows[w] if asof_ts - 7 * 86400 <= x[0] < asof_ts]
        if len(recent) < 10:
            return False
        last = max((x[0] for x in wallet_windows[w] if x[0] < asof_ts), default=0)
        return (asof_ts - last) <= 48 * 3600

    print(f"\n=== LEADERBOARD (decay-weighted, half-life {HALF_LIFE_DAYS:.0f}d; "
          f"filters: >=10 windows in 7d, active within 48h) ===")
    scored = []
    for w in wallet_windows:
        if not eligible(w, now_ts):
            continue
        wins = [x for x in wallet_windows[w]]
        n = len(wins)
        tot = sum(x[1] for x in wins)
        wr = 100.0 * sum(1 for x in wins if x[2]) / n
        n7 = [x for x in wins if now_ts - x[0] <= 7 * 86400]
        pnl7 = sum(x[1] for x in n7)
        scored.append((decay_score(w, now_ts), w, n, wr, tot, pnl7, len(n7)))
    scored.sort(reverse=True)
    print(f"{'rank':<5}{'wallet':<14}{'pseudonym':<26}{'score':>9}{'7d_pnl':>9}{'7d_n':>6}"
          f"{'all_n':>7}{'WR%':>7}{'all_pnl':>10}")
    for i, (sc, w, n, wr, tot, pnl7, n7) in enumerate(scored[:args.top]):
        print(f"{i+1:<5}{w[:12]:<14}{str(names.get(w))[:24]:<26}{sc:>9.0f}{pnl7:>9.0f}{n7:>6}"
              f"{n:>7}{wr:>6.1f}%{tot:>10.0f}")

    # ---- walk-forward mirror: top-K asof each test day, mirror their first BUY per window ----
    print(f"\n=== WALK-FORWARD MIRROR (top-{args.k} by decay score asof day start, "
          f"mirror first BUY <=240s, price band splits, +1c slip, ${STAKE:.0f}) ===")
    days = sorted({day_of(ws) for ws in outcomes})
    test_days = days[-7:]
    grand = defaultdict(lambda: [0, 0, 0.0])
    for d in test_days:
        # UTC day start (day_of buckets by UTC) -- local tz here would leak
        # hours of the test day into the selection window (lookahead bias)
        day_start = datetime.datetime.strptime(f"2026-{d}", "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        picks = []
        for w in wallet_windows:
            if eligible(w, day_start):
                picks.append((decay_score(w, day_start), w))
        picks.sort(reverse=True)
        chosen = {w for _, w in picks[:args.k]}
        done_ws = set()
        stats = defaultdict(lambda: [0, 0, 0.0])
        for (w, ws), p in pos.items():
            if w not in chosen or day_of(ws) != d or ws in done_ws or not p["first_buy"]:
                continue
            ts, side, px, sz = p["first_buy"]
            el = ts - ws
            if el > 240 or px > 0.92:
                continue
            done_ws.add(ws)
            ep = min(px + 0.01, 0.99)
            won = outcomes[ws] == side
            pnl = (STAKE / ep - STAKE) * (1 - FEE) if won else -STAKE
            band = "lotto<=0.10" if px <= 0.10 else ("mid0.10-0.60" if px <= 0.60 else "fav>0.60")
            for k2 in (band, "ALL"):
                stats[k2][0] += 1
                stats[k2][1] += int(won)
                stats[k2][2] += pnl
                grand[k2][0] += 1
                grand[k2][1] += int(won)
                grand[k2][2] += pnl
        s = stats["ALL"]
        wrs = f"{100*s[1]/s[0]:.0f}%" if s[0] else "-"
        print(f"  {d}: picks={len(chosen)}  n={s[0]:<4} WR={wrs:<5} PnL=${s[2]:+8.2f}")
    print("  ---- totals by entry-price band ----")
    for band in ("ALL", "lotto<=0.10", "mid0.10-0.60", "fav>0.60"):
        s = grand[band]
        if s[0]:
            print(f"  {band:<14} n={s[0]:<5} WR={100*s[1]/s[0]:5.1f}%  PnL=${s[2]:+9.2f}  EV/t=${s[2]/s[0]:+.2f}")

    # ---- late-lottery maker study: actual BUY fills at <=0.05 in final 30s ----
    print("\n=== LATE-LOTTERY FILLS (BUY price<=0.05, elapsed>=270s) -- real tape, no slippage ===")
    agg = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for ts, ws, w, pseudo, side, osd, sz, px in rows:
        ws = int(ws)
        if ws not in outcomes or side != "BUY":
            continue
        px = float(px); sz = float(sz); ts = float(ts)
        el = ts - ws
        if el < 270 or px > 0.05:
            continue
        won = outcomes[ws] == osd.upper()
        cost = px * sz
        payout = sz if won else 0.0
        b = "p<=0.02" if px <= 0.02 else "p0.03-0.05"
        for k2 in (b, "ALL"):
            a = agg[k2]
            a[0] += 1
            a[1] += int(won)
            a[2] += cost
            a[3] += payout
    for k2 in ("ALL", "p<=0.02", "p0.03-0.05"):
        a = agg[k2]
        if a[0]:
            roi = (a[3] - a[2]) / a[2] * 100 if a[2] > 0 else 0
            print(f"  {k2:<11} fills={a[0]:<6} winrate={100*a[1]/a[0]:5.2f}%  cost=${a[2]:,.0f}  "
                  f"payout=${a[3]:,.0f}  ROI={roi:+.1f}%")
    conn.close()


if __name__ == "__main__":
    main()
