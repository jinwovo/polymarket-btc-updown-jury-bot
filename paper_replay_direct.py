"""Direct-trigger paper replay - parity with paper_trade_sim.py + paper_sim_eth5.py.
Same filter logic, same 500ms delay, same fee structure.

Usage: python paper_replay_direct.py [btc5|eth5] [hours]
"""
import os, time, bisect, sys
import numpy as np
from dotenv import load_dotenv
load_dotenv(".env.secrets")
load_dotenv("env/runtime.public.env", override=True)
import pymysql

pw = os.getenv("MARIADB_PASSWORD", "")
conn = pymysql.connect(host="127.0.0.1", port=3400, user="root", password=pw, database="polymarket_btc_updown")
cur = conn.cursor()
now_ts = time.time()

# Args
market = sys.argv[1] if len(sys.argv) > 1 else "all"
max_h = int(sys.argv[2]) if len(sys.argv) > 2 else 480

STAKE = float(os.getenv("PAPER_FIXED_STAKE", "10.0"))
ETH5_STAKE = float(os.getenv("ETH5_FIXED_STAKE", "10.0"))
FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.03"))
DELAY_SEC = 0.5  # 500ms execution delay


def load_market(slug, ticks_tbl, max_h):
    cur.execute(f"SELECT ts, price, volume FROM {ticks_tbl} WHERE ts>=%s AND ts<=%s ORDER BY ts",
                (now_ts-max_h*3600-600, now_ts+300))
    rows = cur.fetchall()
    t_ts = [float(r[0]) for r in rows]; t_px = [float(r[1]) for r in rows]; t_vol = [float(r[2] or 0) for r in rows]
    cur.execute(f"SELECT window_start, actual_outcome, btc_start_price FROM market_windows WHERE window_start>=%s AND slug LIKE %s AND actual_outcome IS NOT NULL",
                (now_ts-max_h*3600-300, slug))
    outs = {int(r[0]): {"o": r[1], "sp": float(r[2]) if r[2] else None} for r in cur.fetchall()}
    cur.execute("SELECT ts, window_start, up_best_ask, down_best_ask FROM poly_odds WHERE slug LIKE %s AND ts>=%s AND ts<=%s ORDER BY ts",
                (slug, now_ts-max_h*3600-60, now_ts+60))
    od = cur.fetchall()
    return {
        "t_ts": t_ts, "t_px": t_px, "t_vol": t_vol, "outs": outs,
        "o_ts": [float(r[0]) for r in od], "o_ws": [int(r[1]) for r in od],
        "o_ua": [float(r[2] or 0.5) for r in od], "o_da": [float(r[3] or 0.5) for r in od],
    }


def make_helpers(d):
    t_ts, t_px, t_vol = d["t_ts"], d["t_px"], d["t_vol"]
    o_ts, o_ws, o_ua, o_da = d["o_ts"], d["o_ws"], d["o_ua"], d["o_da"]

    def price_at(ts):
        i = bisect.bisect_right(t_ts, ts) - 1
        return t_px[i] if i >= 0 else None

    def odds_at(ws, ts):
        idx = bisect.bisect_right(o_ts, ts) - 1
        while idx >= 0:
            if o_ws[idx] == ws: return o_ua[idx], o_da[idx]
            if o_ts[idx] < ts - 10: break
            idx -= 1
        return None, None

    def bb_pos(ts, window=60):
        i1 = bisect.bisect_right(t_ts, ts); i0 = bisect.bisect_left(t_ts, ts - 120)
        if i1 - i0 < 30: return None
        px = t_px[max(i0, i1-window):i1]
        if len(px) < 20: return None
        m = sum(px)/len(px); s = (sum((p-m)**2 for p in px)/len(px))**0.5
        return (t_px[i1-1]-m)/(2*s) if s > 0.01 else None

    def path_r2(ws, ts):
        i0 = bisect.bisect_left(t_ts, float(ws)); i1 = bisect.bisect_right(t_ts, ts)
        px = t_px[i0:i1]
        if len(px) < 20: return 0
        x = np.arange(len(px), dtype=float); y = np.array(px, dtype=float)
        xm, ym = x.mean(), y.mean()
        sxy = np.sum((x-xm)*(y-ym)); sxx = np.sum((x-xm)**2); syy = np.sum((y-ym)**2)
        return (sxy**2)/(sxx*syy) if sxx > 0 and syy > 0 else 0

    return price_at, odds_at, bb_pos, path_r2


def replay_btc5(max_h):
    """Replay BTC5 with paper_trade_sim direct-mode logic."""
    print(f"\n{'='*100}\nBTC5 PAPER_REPLAY (direct-trigger, stake=${STAKE}, max_h={max_h}h)\n{'='*100}")
    d = load_market("btc-updown-5m%%", "btc_ticks", max_h)
    price_at, odds_at, bb_pos, path_r2 = make_helpers(d)

    # Match paper_trade_sim.py settings
    entry_start = float(os.getenv("PAPER_ENTRY_START_SEC", "100"))
    entry_end = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
    down_entry_end = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "200"))
    min_remaining = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
    max_ask = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.50"))
    down_min = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.35"))
    r2_min = float(os.getenv("PAPER_DIRECT_R2_MIN", "0.15"))
    move_thresh = 0.02  # min direction-detection threshold

    trades = []
    for ws, oi in sorted(d["outs"].items()):
        if ws < now_ts - max_h*3600: continue
        if oi["o"] not in ("UP","DOWN"): continue
        sp = oi.get("sp") or price_at(float(ws))
        if not sp or sp <= 0: continue

        # Scan every 10s like data_collector does (paper polls signal_cache every 0.1s
        # but gate is computed on tick boundary; 10s is reasonable approximation)
        for sec in range(int(entry_start), int(entry_end)+1, 10):
            ts = float(ws) + sec
            remaining = 300 - sec
            if remaining < min_remaining: break
            pnow = price_at(ts)
            if not pnow: continue
            move = (pnow - sp)/sp*100
            if abs(move) < move_thresh: continue
            direction = "UP" if move > 0 else "DOWN"

            # Down entry cutoff
            if direction == "DOWN" and sec > down_entry_end: continue

            ua, da = odds_at(ws, ts)
            if ua is None: continue
            ep = ua if direction == "UP" else da
            if ep > max_ask: continue
            if direction == "DOWN" and ep < down_min: continue

            # Wide BB filter
            bb = bb_pos(ts)
            if bb is None: continue
            if direction == "UP" and not (0.3 <= bb < 1.5): continue
            if direction == "DOWN" and not (-1.5 < bb < -0.3): continue

            # R2 filter
            r2 = path_r2(ws, ts)
            if r2 < r2_min: continue

            # 500ms delay
            ua2, da2 = odds_at(ws, ts + DELAY_SEC)
            actual_ep = ep
            if ua2 is not None:
                dp = ua2 if direction == "UP" else da2
                if 0.01 < dp < 0.99: actual_ep = dp
            if actual_ep > max_ask: continue
            if direction == "DOWN" and actual_ep < down_min: continue

            won = (direction == oi["o"])
            pnl = ((STAKE/actual_ep - STAKE) * (1 - FEE_RATE)) if won else -STAKE
            trades.append({"ws": ws, "won": won, "pnl": pnl, "ep": actual_ep, "dir": direction})
            break  # one trade per window

    return trades


def replay_eth5(max_h):
    """Replay ETH5 with paper_sim_eth5 direct-mode logic."""
    print(f"\n{'='*100}\nETH5 PAPER_REPLAY (direct-trigger, stake=${ETH5_STAKE}, max_h={max_h}h)\n{'='*100}")
    d = load_market("eth-updown-5m%%", "eth_ticks", max_h)
    price_at, odds_at, bb_pos, path_r2 = make_helpers(d)

    entry_start = float(os.getenv("ETH5_ENTRY_START_SEC", "80"))
    entry_end = float(os.getenv("ETH5_ENTRY_END_SEC", "260"))
    down_entry_end = float(os.getenv("ETH5_DOWN_ENTRY_END_SEC", "200"))
    min_remaining = float(os.getenv("ETH5_MIN_SECONDS_REMAINING", "30"))
    max_ask = float(os.getenv("ETH5_MAX_ENTRY_PRICE", "0.60"))
    move_thresh = float(os.getenv("ETH5_DIRECT_MOVE_THRESHOLD", "0.04"))

    trades = []
    for ws, oi in sorted(d["outs"].items()):
        if ws < now_ts - max_h*3600: continue
        if oi["o"] not in ("UP","DOWN"): continue
        sp = oi.get("sp") or price_at(float(ws))
        if not sp or sp <= 0: continue

        for sec in range(int(entry_start), int(entry_end)+1, 10):
            ts = float(ws) + sec
            remaining = 300 - sec
            if remaining < min_remaining: break
            pnow = price_at(ts)
            if not pnow: continue
            move = (pnow - sp)/sp*100
            if abs(move) < 0.02: continue  # min detection threshold
            direction = "UP" if move > 0 else "DOWN"
            if direction == "DOWN" and sec > down_entry_end: continue

            ua, da = odds_at(ws, ts)
            if ua is None: continue
            ep = ua if direction == "UP" else da
            if ep > max_ask or ep < 0.35: continue

            # Wide BB filter
            bb = bb_pos(ts)
            if bb is None: continue
            if direction == "UP" and not (0.3 <= bb < 1.5): continue
            if direction == "DOWN" and not (-1.5 < bb < -0.3): continue

            # ETH5 specific: require strong move
            if abs(move) < move_thresh: continue

            # 500ms delay
            ua2, da2 = odds_at(ws, ts + DELAY_SEC)
            actual_ep = ep
            if ua2 is not None:
                dp = ua2 if direction == "UP" else da2
                if 0.01 < dp < 0.99: actual_ep = dp
            if actual_ep > max_ask: continue

            won = (direction == oi["o"])
            pnl = ((ETH5_STAKE/actual_ep - ETH5_STAKE) * (1 - FEE_RATE)) if won else -ETH5_STAKE
            trades.append({"ws": ws, "won": won, "pnl": pnl, "ep": actual_ep, "dir": direction})
            break

    return trades


def report(trades, label, max_h):
    if not trades:
        print(f"  No trades for {label}")
        return
    print(f"\n  Period   Trades  /hour     WR        PnL      $/day")
    for h in [12, 24, 48, 72, 120, 240, 480]:
        if h > max_h: break
        cutoff = now_ts - h*3600
        pt = [t for t in trades if t["ws"] >= cutoff]
        if not pt:
            print(f"  {h:>5d}h        -")
            continue
        n = len(pt); w = sum(1 for t in pt if t["won"])
        pnl = sum(t["pnl"] for t in pt)
        wr = w/n*100
        per_hr = n/h
        per_day = pnl/(h/24)
        mark = "+" if pnl > 0 else "-"
        print(f"  {h:>5d}h  {n:>5d}  {per_hr:>4.2f}  {wr:>5.1f}%  ${pnl:>+8.2f}  ${per_day:>+7.2f}/day {mark}")


# Run
if market in ("btc5", "all"):
    btc_trades = replay_btc5(max_h)
    report(btc_trades, "BTC5", max_h)

if market in ("eth5", "all"):
    eth_trades = replay_eth5(max_h)
    report(eth_trades, "ETH5", max_h)

if market == "all":
    print(f"\n{'='*100}\nCOMBINED (BTC5 + ETH5)\n{'='*100}")
    print(f"\n  Period   Trades  /hour     WR        PnL      $/day")
    for h in [12, 24, 48, 72, 120, 240, 480]:
        if h > max_h: break
        cutoff = now_ts - h*3600
        bt = [t for t in btc_trades if t["ws"] >= cutoff]
        et = [t for t in eth_trades if t["ws"] >= cutoff]
        n = len(bt) + len(et)
        if n == 0:
            print(f"  {h:>5d}h        -")
            continue
        w = sum(1 for t in bt if t["won"]) + sum(1 for t in et if t["won"])
        pnl = sum(t["pnl"] for t in bt) + sum(t["pnl"] for t in et)
        wr = w/n*100
        per_hr = n/h; per_day = pnl/(h/24)
        mark = "+" if pnl > 0 else "-"
        print(f"  {h:>5d}h  {n:>5d}  {per_hr:>4.2f}  {wr:>5.1f}%  ${pnl:>+8.2f}  ${per_day:>+7.2f}/day {mark}")

conn.close()
