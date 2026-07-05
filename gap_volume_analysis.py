"""Binance-RTDS gap delta + buy/sell volume ratio analysis (CLAUDE.md TODO).

gap_delta(t) = gap(t) - mean(gap over trailing 60s), per window.
1) Pure signal: does gap_delta at time T predict window outcome?
2) Volume: does buy_sell_ratio at time T predict outcome?
3) Overlay: do gap_delta / bsr at entry split the recommended-config trades?
"""
import os, sys, datetime
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv(".env.secrets")
load_dotenv("env/runtime.public.env", override=True)
import pymysql

FEE = 0.03
STAKE = 10.0
START_D = sys.argv[1] if len(sys.argv) > 1 else "2026-04-01"
END_D = sys.argv[2] if len(sys.argv) > 2 else "2026-07-05"

pw = os.getenv("MARIADB_PASSWORD", "")
conn = pymysql.connect(host="127.0.0.1", port=3400, user="root", password=pw,
                       database="polymarket_btc_updown")
cur = conn.cursor()
start_ts = datetime.datetime.strptime(START_D, "%Y-%m-%d").timestamp()
end_ts = datetime.datetime.strptime(END_D, "%Y-%m-%d").timestamp()
cur.execute(
    "SELECT window_start, actual_outcome FROM market_windows "
    "WHERE window_start >= %s AND slug LIKE 'btc-updown-5m%%' AND actual_outcome IN ('UP','DOWN')",
    (start_ts - 300,))
outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
cur.close()

scur = conn.cursor(pymysql.cursors.SSCursor)
scur.execute(
    "SELECT ts, window_start, seconds_elapsed, binance_rtds_gap, buy_sell_ratio, "
    "btc_move_pct, up_ask, down_ask, guards_passed, ask_drift, path_r2, bb_pos "
    "FROM signal_cache_log WHERE ts >= %s AND ts < %s ORDER BY ts",
    (start_ts, end_ts))

SAMPLE_T = [90, 150, 210]
samples = []          # (ws, T, gap_delta, bsr, outcome)
trades = []           # recommended cfg trades with gap_delta/bsr at entry
gap_hist = defaultdict(list)   # ws -> [(elapsed, gap)]
sampled = defaultdict(set)     # ws -> set(T)
done_ws = set()

CFG = dict(START=100, END=240, DRIFT=0.08, R2_MIN=0.10, BB_MIN=0.9)

for ts, ws, el, gap, bsr, mv, ua, da, guards, dr, r2, bb in scur:
    ws = int(ws or 0)
    if ws <= 0 or ws not in outcomes:
        continue
    el = float(el or 0)
    out = outcomes[ws]

    gd = None
    if gap is not None:
        g = float(gap)
        hist = gap_hist[ws]
        hist.append((el, g))
        trail = [x[1] for x in hist if x[0] >= el - 60]
        if len(trail) >= 10 and el >= 30:
            gd = g - sum(trail) / len(trail)

    for T in SAMPLE_T:
        if T <= el < T + 15 and T not in sampled[ws]:
            sampled[ws].add(T)
            samples.append((ws, T, gd, float(bsr) if bsr is not None else None, out,
                            float(ua) if ua else None, float(da) if da else None))
            break

    # inline replay of recommended config to tag trades with gap_delta/bsr
    if ws in done_ws or mv is None:
        continue
    mvf = float(mv)
    if abs(mvf) < 0.02:
        continue
    d = "UP" if mvf > 0 else "DOWN"
    if el < CFG["START"] or el > CFG["END"] or 300 - el < 30:
        continue
    if d == "DOWN" and el > 200:
        continue
    uaf, daf = float(ua or 0), float(da or 0)
    ep = uaf if d == "UP" else daf
    if ep <= 0 or ep >= 1:
        continue
    if d == "DOWN" and ep < 0.35:
        continue
    if uaf > 0 and daf > 0 and abs(uaf - daf) > 0.20:
        continue
    if not int(guards or 0):
        continue
    if dr is not None and float(dr) > CFG["DRIFT"]:
        continue
    if r2 is not None and el < 150 and float(r2) < 0.10:
        continue
    if bb is None:
        continue
    bbv = float(bb)
    if d == "UP" and not (CFG["BB_MIN"] <= bbv < 2.5):
        continue
    if d == "DOWN" and not (-2.5 < bbv < -CFG["BB_MIN"]):
        continue
    if r2 is None or float(r2) < CFG["R2_MIN"]:
        continue
    won = d == out
    pnl = (STAKE / ep - STAKE) * (1 - FEE) if won else -STAKE
    trades.append(dict(ws=ws, dir=d, ep=ep, won=won, pnl=pnl, gd=gd,
                       bsr=float(bsr) if bsr is not None else None))
    done_ws.add(ws)

scur.close()
conn.close()

print(f"samples={len(samples):,} trades={len(trades):,} windows={len(outcomes):,}")

print("\n=== 1) GAP DELTA as directional signal (gap_delta>+thr -> UP, <-thr -> DOWN) ===")
print(f"{'T':>4} {'thr$':>6} {'n':>6} {'WR':>6} {'avg_ask':>8} {'EV/$10':>8}")
for T in SAMPLE_T:
    sub = [s for s in samples if s[1] == T and s[2] is not None]
    for thr in [1, 2, 3, 5, 8, 12, 20]:
        sig = []
        for s in sub:
            sgn = 1 if s[2] > 0 else -1
            if abs(s[2]) < thr:
                continue
            ask = s[5] if sgn > 0 else s[6]
            if ask is None or ask <= 0 or ask >= 1:
                continue
            won = (sgn > 0) == (s[4] == "UP")
            pnl = (STAKE / ask - STAKE) * (1 - FEE) if won else -STAKE
            sig.append((won, ask, pnl))
        if len(sig) < 30:
            continue
        hit = sum(1 for w, a, p in sig if w)
        avg_ask = sum(a for w, a, p in sig) / len(sig)
        ev = sum(p for w, a, p in sig) / len(sig)
        print(f"{T:>4} {thr:>6} {len(sig):>6} {100*hit/len(sig):>5.1f}% {avg_ask:>8.3f} {ev:>+8.3f}")

print("\n=== 2) BUY/SELL RATIO as directional signal (bsr>thr -> UP, bsr<1/thr -> DOWN) ===")
for T in SAMPLE_T:
    sub = [s for s in samples if s[1] == T and s[3] is not None and s[3] > 0]
    for thr in [1.1, 1.3, 1.5, 2.0, 3.0]:
        sig = []
        for s in sub:
            sgn = 1 if s[3] >= thr else (-1 if s[3] <= 1.0 / thr else 0)
            if sgn == 0:
                continue
            ask = s[5] if sgn > 0 else s[6]
            if ask is None or ask <= 0 or ask >= 1:
                continue
            won = (sgn > 0) == (s[4] == "UP")
            pnl = (STAKE / ask - STAKE) * (1 - FEE) if won else -STAKE
            sig.append((won, ask, pnl))
        if len(sig) < 30:
            continue
        hit = sum(1 for w, a, p in sig if w)
        avg_ask = sum(a for w, a, p in sig) / len(sig)
        ev = sum(p for w, a, p in sig) / len(sig)
        print(f"{T:>4} {thr:>6} {len(sig):>6} {100*hit/len(sig):>5.1f}% {avg_ask:>8.3f} {ev:>+8.3f}")

def split(label, sub):
    n = len(sub)
    if n == 0:
        print(f"  {label:<26} none")
        return
    w = sum(1 for t in sub if t["won"])
    pnl = sum(t["pnl"] for t in sub)
    print(f"  {label:<26} n={n:<4d} WR={100*w/n:5.1f}%  PnL=${pnl:+8.2f}")

print("\n=== 3) OVERLAY on recommended-config trades ===")
gd_t = [t for t in trades if t["gd"] is not None]
split("gap_delta agrees w/ dir", [t for t in gd_t if (t["gd"] > 0) == (t["dir"] == "UP")])
split("gap_delta disagrees", [t for t in gd_t if (t["gd"] > 0) != (t["dir"] == "UP")])
for thr in [2, 5]:
    split(f"agrees & |gd|>={thr}", [t for t in gd_t if abs(t["gd"]) >= thr and (t["gd"] > 0) == (t["dir"] == "UP")])
    split(f"disagrees & |gd|>={thr}", [t for t in gd_t if abs(t["gd"]) >= thr and (t["gd"] > 0) != (t["dir"] == "UP")])
bs_t = [t for t in trades if t["bsr"] is not None and t["bsr"] > 0]
split("bsr agrees w/ dir", [t for t in bs_t if (t["bsr"] > 1) == (t["dir"] == "UP")])
split("bsr disagrees", [t for t in bs_t if (t["bsr"] > 1) != (t["dir"] == "UP")])
for thr in [1.3, 2.0]:
    split(f"bsr agrees strong {thr}", [t for t in bs_t if ((t["bsr"] >= thr) and t["dir"] == "UP") or ((t["bsr"] <= 1/thr) and t["dir"] == "DOWN")])
    split(f"bsr disagr strong {thr}", [t for t in bs_t if ((t["bsr"] >= thr) and t["dir"] == "DOWN") or ((t["bsr"] <= 1/thr) and t["dir"] == "UP")])
