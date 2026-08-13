"""OOS replay from signal_cache_log - exact parity with paper_trade_sim.py direct mode.

Replays logged signal_cache rows (the SAME values paper/live read at runtime)
instead of re-deriving BB/R2 from ticks like paper_replay_direct.py does.

Usage: python oos_replay_signal_cache.py [btc5|eth5] [start_date] [end_date] [overrides_json]
  e.g. python oos_replay_signal_cache.py btc5 2026-04-01 2026-07-05
       python oos_replay_signal_cache.py btc5 2026-05-01 2026-07-05 "{\"MAX_ASK\": 0.55}"
"""
import os, sys, json, datetime
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv(".env.secrets")
load_dotenv("env/runtime.public.env", override=True)
import pymysql

MARKET = sys.argv[1] if len(sys.argv) > 1 else "btc5"
START = sys.argv[2] if len(sys.argv) > 2 else "2026-04-01"
END = sys.argv[3] if len(sys.argv) > 3 else "2026-07-05"
OVR = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}

FEE = float(os.getenv("TAKER_FEE_RATE", "0.03"))
STAKE = 10.0

if MARKET == "btc5":
    P = {
        "ENTRY_START": float(os.getenv("PAPER_ENTRY_START_SEC", "80")),
        "ENTRY_END": float(os.getenv("PAPER_ENTRY_END_SEC", "240")),
        "DOWN_ENTRY_END": float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "200")),
        "MIN_REMAINING": float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30")),
        "DOWN_MIN_PRICE": float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.35")),
        "MAX_SPREAD": float(os.getenv("PAPER_MAX_ODDS_SPREAD", "0.20")),
        "MAX_ASK_DRIFT": float(os.getenv("PAPER_MAX_ASK_DRIFT", "0.05")),
        "R2_MIN_FILTER": float(os.getenv("PAPER_R2_MIN_FILTER", "0.10")),
        "R2_BYPASS_LATE": os.getenv("PAPER_R2_BYPASS_LATE", "true").lower() == "true",
        "DIRECT_R2_MIN": float(os.getenv("PAPER_DIRECT_R2_MIN", "0.05")),
        "BB_MIN": float(os.getenv("PAPER_DIRECT_BB_MIN_ABS", "0.9")),
        "BB_MAX": float(os.getenv("PAPER_DIRECT_BB_MAX", "2.5")),
        "MOVE_MIN": 0.02,
        "MAX_ASK": 0.0,  # 0 = no cap (matches paper_trade_sim direct mode: adaptive_max_ask skipped)
        "MIN_MOVE_PCT": float(os.getenv("PAPER_MIN_BTC_MOVE_PCT", "0")),
        "MAX_MOVE_PCT": float(os.getenv("PAPER_MAX_BTC_MOVE_PCT", "0")),
        "REQUIRE_GUARDS": 1,
    }
    SC_TABLE = "signal_cache_log"
    SLUG = "btc-updown-5m%"
else:
    P = {
        "ENTRY_START": float(os.getenv("ETH5_ENTRY_START_SEC", "160")),
        "ENTRY_END": float(os.getenv("ETH5_ENTRY_END_SEC", "240")),
        "DOWN_ENTRY_END": float(os.getenv("ETH5_DOWN_ENTRY_END_SEC", "200")),
        "MIN_REMAINING": float(os.getenv("ETH5_MIN_SECONDS_REMAINING", "30")),
        "DOWN_MIN_PRICE": float(os.getenv("ETH5_DOWN_MIN_ENTRY_PRICE", "0.35")),
        "MAX_SPREAD": float(os.getenv("ETH5_MAX_ODDS_SPREAD", "0.20")),
        "MAX_ASK_DRIFT": float(os.getenv("ETH5_MAX_ASK_DRIFT", "999")),
        "R2_MIN_FILTER": 0.0,
        "R2_BYPASS_LATE": True,
        "DIRECT_R2_MIN": 0.0,
        "BB_MIN": float(os.getenv("ETH5_BB_MIN_ABS", "0.7")),
        "BB_MAX": float(os.getenv("ETH5_BB_MAX_ABS", "2.5")),
        "MOVE_MIN": float(os.getenv("ETH5_DIRECT_MOVE_THRESHOLD", "0.05")),
        "MAX_ASK": float(os.getenv("ETH5_MAX_ENTRY_PRICE", "0.55")),
        "MIN_MOVE_PCT": 0.0,
        "MAX_MOVE_PCT": 0.0,
        # paper_sim_eth5 runtime SKIPS guards ("too strict for ETH") -- replay must match
        "REQUIRE_GUARDS": 0,
    }
    SC_TABLE = "signal_cache_log_eth5"
    SLUG = "eth-updown-5m%"

P.update(OVR)

start_ts = datetime.datetime.strptime(START, "%Y-%m-%d").timestamp()
end_ts = datetime.datetime.strptime(END, "%Y-%m-%d").timestamp()

pw = os.getenv("MARIADB_PASSWORD", "")
conn = pymysql.connect(host="127.0.0.1", port=3400, user="root", password=pw,
                       database="polymarket_btc_updown")
cur = conn.cursor()
cur.execute(
    "SELECT window_start, actual_outcome FROM market_windows "
    "WHERE window_start >= %s AND window_start < %s AND slug LIKE %s AND actual_outcome IN ('UP','DOWN')",
    (start_ts - 300, end_ts, SLUG))
outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
cur.close()

scur = conn.cursor(pymysql.cursors.SSCursor)
scur.execute(
    f"SELECT ts, window_start, seconds_elapsed, btc_move_pct, up_ask, down_ask, "
    f"guards_passed, ask_drift, path_r2, bb_pos, binance_rtds_gap, buy_sell_ratio "
    f"FROM {SC_TABLE} WHERE ts >= %s AND ts < %s ORDER BY ts",
    (start_ts, end_ts))

trades = []
done_ws = set()
rows_seen = 0
for row in scur:
    rows_seen += 1
    ts, ws, elapsed, move, ua, da, guards, drift, r2, bb, gap, bsr = row
    ws = int(ws or 0)
    if ws <= 0 or ws in done_ws or ws not in outcomes:
        continue
    if move is None:
        continue
    move = float(move)
    if abs(move) < P["MOVE_MIN"]:
        continue
    direction = "UP" if move > 0 else "DOWN"

    elapsed = float(elapsed or 0)
    remaining = 300.0 - elapsed
    if elapsed < P["ENTRY_START"] or elapsed > P["ENTRY_END"]:
        continue
    if remaining < P["MIN_REMAINING"]:
        continue
    if direction == "DOWN" and elapsed > P["DOWN_ENTRY_END"]:
        continue

    if P["MIN_MOVE_PCT"] > 0 and abs(move) < P["MIN_MOVE_PCT"]:
        continue
    if P["MAX_MOVE_PCT"] > 0 and abs(move) > P["MAX_MOVE_PCT"]:
        continue

    ua = float(ua) if ua else None
    da = float(da) if da else None
    ep = ua if direction == "UP" else da
    if ep is None or ep <= 0.0 or ep >= 1.0:
        continue
    if direction == "DOWN" and ep < P["DOWN_MIN_PRICE"]:
        continue
    if P["MAX_ASK"] > 0 and ep > P["MAX_ASK"]:
        continue
    if ua is not None and da is not None and abs(ua - da) > P["MAX_SPREAD"]:
        continue

    if P["REQUIRE_GUARDS"] and not int(guards or 0):
        continue

    if P["MAX_ASK_DRIFT"] > 0 and drift is not None and float(drift) > P["MAX_ASK_DRIFT"]:
        continue

    # R2 filter (all modes): bypass when elapsed >= 150s
    if P["R2_MIN_FILTER"] > 0 and r2 is not None:
        is_late = P["R2_BYPASS_LATE"] and elapsed >= 150
        if not is_late and float(r2) < P["R2_MIN_FILTER"]:
            continue
    # Direct-mode wide BB
    if bb is None:
        continue
    bbv = float(bb)
    if direction == "UP":
        if not (P["BB_MIN"] <= bbv < P["BB_MAX"]):
            continue
    else:
        if not (-P["BB_MAX"] < bbv < -P["BB_MIN"]):
            continue
    # Direct-mode R2 (always, no late bypass)
    if P["DIRECT_R2_MIN"] > 0:
        if r2 is None or float(r2) < P["DIRECT_R2_MIN"]:
            continue

    won = direction == outcomes[ws]
    pnl = (STAKE / ep - STAKE) * (1 - FEE) if won else -STAKE
    trades.append({
        "ws": ws, "dir": direction, "ep": ep, "elapsed": elapsed,
        "won": won, "pnl": pnl, "gap": float(gap) if gap is not None else None,
        "bsr": float(bsr) if bsr is not None else None,
        "hour": datetime.datetime.utcfromtimestamp(ws).hour,
    })
    done_ws.add(ws)

scur.close()
conn.close()


def bucket(trades_subset, label):
    n = len(trades_subset)
    if n == 0:
        print(f"  {label:<12} no trades")
        return
    w = sum(1 for t in trades_subset if t["won"])
    pnl = sum(t["pnl"] for t in trades_subset)
    gross_w = sum(t["pnl"] for t in trades_subset if t["pnl"] > 0)
    gross_l = -sum(t["pnl"] for t in trades_subset if t["pnl"] < 0)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    avg_ep = sum(t["ep"] for t in trades_subset) / n
    print(f"  {label:<12} n={n:<5d} WR={100*w/n:5.1f}%  PnL=${pnl:+9.2f}  PF={pf:5.2f}  avg_ask={avg_ep:.3f}")


print(f"\n{MARKET.upper()} signal_cache replay {START} ~ {END}  (stake=${STAKE}, fee={FEE})")
print(f"params: {json.dumps(P)}")
print(f"rows scanned: {rows_seen:,}, windows with outcome: {len(outcomes):,}\n")

by_month = defaultdict(list)
for t in trades:
    m = datetime.datetime.utcfromtimestamp(t["ws"]).strftime("%Y-%m")
    by_month[m].append(t)
for m in sorted(by_month):
    bucket(by_month[m], m)
bucket(trades, "TOTAL")

up = [t for t in trades if t["dir"] == "UP"]
dn = [t for t in trades if t["dir"] == "DOWN"]
bucket(up, "UP only")
bucket(dn, "DOWN only")

if os.getenv("OOS_DUMP_TRADES"):
    with open(f"oos_trades_{MARKET}.json", "w") as f:
        json.dump(trades, f)
    print(f"\ntrades dumped to oos_trades_{MARKET}.json")
