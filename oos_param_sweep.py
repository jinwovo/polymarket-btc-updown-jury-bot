"""Param sweep on signal_cache_log candidate rows (BTC5 direct mode).

Loads candidate rows once (|move|>=0.02, elapsed 60-260, guards=1) then
evaluates filter configs in memory. Select on May-June, validate on April.
"""
import os, sys, json, datetime, itertools
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv(".env.secrets")
load_dotenv("env/runtime.public.env", override=True)
import pymysql

FEE = 0.03
STAKE = 10.0

pw = os.getenv("MARIADB_PASSWORD", "")
conn = pymysql.connect(host="127.0.0.1", port=3400, user="root", password=pw,
                       database="polymarket_btc_updown")
cur = conn.cursor()
start_ts = datetime.datetime.strptime("2026-04-01", "%Y-%m-%d").timestamp()
end_ts = datetime.datetime.strptime("2026-07-05", "%Y-%m-%d").timestamp()
cur.execute(
    "SELECT window_start, actual_outcome FROM market_windows "
    "WHERE window_start >= %s AND slug LIKE 'btc-updown-5m%%' AND actual_outcome IN ('UP','DOWN')",
    (start_ts - 300,))
outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
cur.close()

scur = conn.cursor(pymysql.cursors.SSCursor)
scur.execute(
    "SELECT ts, window_start, seconds_elapsed, btc_move_pct, up_ask, down_ask, "
    "ask_drift, path_r2, bb_pos FROM signal_cache_log "
    "WHERE ts >= %s AND ts < %s AND guards_passed = 1 "
    "AND ABS(btc_move_pct) >= 0.02 AND seconds_elapsed BETWEEN 60 AND 260 ORDER BY ts",
    (start_ts, end_ts))

wins = defaultdict(list)  # ws -> candidate rows
n_rows = 0
for ts, ws, el, mv, ua, da, dr, r2, bb in scur:
    n_rows += 1
    ws = int(ws)
    if ws not in outcomes or bb is None:
        continue
    wins[ws].append((float(el), float(mv), float(ua or 0), float(da or 0),
                     float(dr) if dr is not None else None,
                     float(r2) if r2 is not None else None, float(bb)))
scur.close()
conn.close()
print(f"rows={n_rows:,} windows_with_candidates={len(wins):,}", file=sys.stderr)


def evaluate(cfg, months):
    trades = []
    for ws, rows in wins.items():
        m = datetime.datetime.utcfromtimestamp(ws).strftime("%Y-%m")
        if m not in months:
            continue
        for el, mv, ua, da, dr, r2, bb in rows:
            if el < cfg["START"] or el > cfg["END"]:
                continue
            if 300 - el < 30:
                continue
            d = "UP" if mv > 0 else "DOWN"
            if d == "DOWN" and el > 200:
                continue
            ep = ua if d == "UP" else da
            if ep <= 0 or ep >= 1:
                continue
            if d == "DOWN" and ep < cfg["DOWN_MIN"]:
                continue
            if cfg["MAX_ASK"] > 0 and ep > cfg["MAX_ASK"]:
                continue
            if ua > 0 and da > 0 and abs(ua - da) > 0.20:
                continue
            if dr is not None and dr > cfg["DRIFT"]:
                continue
            if r2 is not None and el < 150 and r2 < 0.10:
                continue
            if d == "UP" and not (cfg["BB_MIN"] <= bb < 2.5):
                continue
            if d == "DOWN" and not (-2.5 < bb < -cfg["BB_MIN"]):
                continue
            if cfg["R2_MIN"] > 0 and (r2 is None or r2 < cfg["R2_MIN"]):
                continue
            won = d == outcomes[ws]
            pnl = (STAKE / ep - STAKE) * (1 - FEE) if won else -STAKE
            trades.append((ws, m, won, pnl))
            break
    return trades


def stats(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0, wr=0, pnl=0, pf=0)
    w = sum(1 for t in trades if t[2])
    pnl = sum(t[3] for t in trades)
    gw = sum(t[3] for t in trades if t[3] > 0)
    gl = -sum(t[3] for t in trades if t[3] < 0)
    return dict(n=n, wr=100 * w / n, pnl=pnl, pf=gw / gl if gl > 0 else 99)


grid = {
    "START": [80, 100, 120],
    "MAX_ASK": [0, 0.55, 0.60],
    "BB_MIN": [0.7, 0.9, 1.1],
    "R2_MIN": [0.05, 0.10],
    "DOWN_MIN": [0.35, 0.42],
    "DRIFT": [0.05, 0.08],
    "END": [240],
}
keys = list(grid)
results = []
for combo in itertools.product(*(grid[k] for k in keys)):
    cfg = dict(zip(keys, combo))
    sel = evaluate(cfg, {"2026-05", "2026-06"})
    s = stats(sel)
    if s["n"] < 40:  # too few trades to trust
        continue
    may = stats([t for t in sel if t[1] == "2026-05"])
    jun = stats([t for t in sel if t[1] == "2026-06"])
    if may["pnl"] <= 0 or jun["pnl"] <= 0:  # require stability
        continue
    val = stats(evaluate(cfg, {"2026-04"}))
    results.append((s["pnl"], cfg, s, may, jun, val))

results.sort(key=lambda r: -r[0])
print(f"\n{'rank':<4} {'cfg':<70} {'MayJun n/WR/PnL/PF':<28} {'May':<16} {'Jun':<16} {'Apr(val) n/WR/PnL/PF'}")
for i, (pnl, cfg, s, may, jun, val) in enumerate(results[:15]):
    cs = f"s{cfg['START']} ask{cfg['MAX_ASK']} bb{cfg['BB_MIN']} r2_{cfg['R2_MIN']} dm{cfg['DOWN_MIN']} dr{cfg['DRIFT']}"
    print(f"{i+1:<4} {cs:<70} {s['n']}/{s['wr']:.0f}%/${s['pnl']:+.0f}/{s['pf']:.2f}    "
          f"${may['pnl']:+.0f}/{may['pf']:.2f}   ${jun['pnl']:+.0f}/{jun['pf']:.2f}   "
          f"{val['n']}/{val['wr']:.0f}%/${val['pnl']:+.0f}/{val['pf']:.2f}")

# baseline = current config
base = dict(START=80, MAX_ASK=0, BB_MIN=0.9, R2_MIN=0.05, DOWN_MIN=0.35, DRIFT=0.05, END=240)
sel = evaluate(base, {"2026-05", "2026-06"})
s = stats(sel)
val = stats(evaluate(base, {"2026-04"}))
print(f"\nBASELINE (current): MayJun {s['n']}/{s['wr']:.0f}%/${s['pnl']:+.0f}/PF{s['pf']:.2f} | Apr {val['n']}/{val['wr']:.0f}%/${val['pnl']:+.0f}/PF{val['pf']:.2f}")
