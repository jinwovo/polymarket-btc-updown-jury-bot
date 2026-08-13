"""PAPER mirror runner: real-time specialist-mirror simulation on BTC5.

Implements the pre-registered next step from the 2026-08-10 tape study:
poll the public trade tape for the CURRENT window, detect the first BUY by a
top-5 walk-forward specialist, and log a SIMULATED entry with real detection
latency and real book ask -- so modeled +2c slippage can be compared against
what a live FAK would actually have paid. No orders are placed.

Selection (recomputed at startup and each UTC day rollover, tape harvested first):
  10 <= windows in trailing 7d <= 210, median per-window buy cost (7d) >= $100,
  median first-buy entry (7d) >= 60s, active within 48h, top-5 by decay PnL (hl=5d).
Mirror rule: first detected chosen-wallet BUY per window, elapsed <= 240s,
px <= 0.92, wallet-day activity cap 30 windows. $10 stake, 3% fee on wins.

Usage:
  python scripts/mirror_paper_runner.py            # run forever
  python scripts/mirror_paper_runner.py --probe    # select wallets, print, exit
Log: bot_mirror_paper.log   Table: mirror_paper_trades
"""
import argparse
import datetime
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(".env.secrets")
import httpx
import pymysql

ASSET = "btc5"
SLUG_PREFIX = "btc-updown-5m"
K = 5
FEE = 0.03
STAKE = 10.0
HALF_LIFE_DAYS = 5.0
EL_CAP = 240.0
PX_CAP = 0.92
WALLET_DAY_CAP = 30
POLL_SEC = 1.2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot_mirror_paper.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("mirror")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def db():
    return pymysql.connect(host="127.0.0.1", port=3400, user="root",
                           password=os.getenv("MARIADB_PASSWORD", ""),
                           database="polymarket_btc_updown", autocommit=True)


def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mirror_paper_trades (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            window_start BIGINT NOT NULL,
            wallet VARCHAR(48) NOT NULL,
            pseudonym VARCHAR(96) NULL,
            side VARCHAR(8) NOT NULL,
            spec_px DOUBLE NOT NULL,
            spec_ts DOUBLE NOT NULL,
            detect_ts DOUBLE NOT NULL,
            latency_sec DOUBLE NOT NULL,
            book_ask DOUBLE NULL,
            sim_entry_px DOUBLE NULL,
            modeled_px DOUBLE NOT NULL,
            stake DOUBLE NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            skip_reason VARCHAR(64) NULL,
            outcome VARCHAR(8) NULL,
            won TINYINT NULL,
            pnl DOUBLE NULL,
            UNIQUE KEY uq_mpt (window_start, wallet),
            INDEX idx_mpt_status (status)
        ) ENGINE=InnoDB""")
    cur.close()


# ---------- selection (mirrors scripts/tape_specialist_mirror.py) ----------

def harvest_recent(days=3):
    try:
        r = subprocess.run([sys.executable, "scripts/tape_harvest.py",
                            "--asset", ASSET, "--days", str(days)],
                           capture_output=True, text=True, timeout=1800)
        tail = (r.stdout or "").strip().splitlines()
        log.info("harvest: %s", tail[-1] if tail else r.returncode)
    except Exception as e:
        log.warning("harvest failed (selection uses existing tape): %s", e)


def select_specialists(conn, asof_ts):
    cur = conn.cursor()
    cur.execute("SELECT window_start, outcome FROM tape_windows "
                "WHERE asset=%s AND outcome IN ('UP','DOWN') AND window_start < %s",
                (ASSET, int(asof_ts)))
    outcomes = {int(r[0]): r[1] for r in cur.fetchall()}
    lo = int(asof_ts - 40 * 86400)
    cur.execute("""
        SELECT wallet, window_start,
               SUM(CASE WHEN side='BUY' THEN -price*size ELSE price*size END),
               SUM(CASE WHEN UPPER(outcome_side)='UP'
                        THEN (CASE WHEN side='BUY' THEN size ELSE -size END) ELSE 0 END),
               SUM(CASE WHEN UPPER(outcome_side)='DOWN'
                        THEN (CASE WHEN side='BUY' THEN size ELSE -size END) ELSE 0 END),
               SUM(CASE WHEN side='BUY' THEN price*size ELSE 0 END),
               MIN(CASE WHEN side='BUY' THEN ts END)
        FROM poly_trades WHERE asset=%s AND window_start BETWEEN %s AND %s
        GROUP BY wallet, window_start""", (ASSET, lo, int(asof_ts)))
    rows = cur.fetchall()
    cur.execute("SELECT wallet, MAX(pseudonym) FROM poly_trades "
                "WHERE asset=%s AND ts > %s GROUP BY wallet", (ASSET, lo))
    names = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()

    ww = defaultdict(list)
    for w, ws, cash, up_sh, dn_sh, buy_cost, fb_ts in rows:
        ws = int(ws)
        if ws not in outcomes:
            continue
        cash, up_sh, dn_sh = float(cash), float(up_sh), float(dn_sh)
        if up_sh < -0.01 or dn_sh < -0.01:
            continue
        settle = up_sh * (outcomes[ws] == "UP") + dn_sh * (outcomes[ws] == "DOWN")
        ww[w].append((ws, cash + settle, float(fb_ts) if fb_ts else None, float(buy_cost)))
    for w in ww:
        ww[w].sort()

    picks = []
    for w, lst in ww.items():
        wk = [r for r in lst if asof_ts - 7 * 86400 <= r[0] < asof_ts]
        if not (10 <= len(wk) <= 210):
            continue
        if asof_ts - lst[-1][0] > 48 * 3600:
            continue
        if median([r[3] for r in wk]) < 100.0:
            continue
        ent = [r[2] - r[0] for r in wk if r[2] is not None]
        if not ent or median(ent) < 60.0:
            continue
        score = sum(r[1] * math.pow(0.5, (asof_ts - r[0]) / 86400.0 / HALF_LIFE_DAYS)
                    for r in lst)
        picks.append((score, w))
    picks.sort(reverse=True)
    chosen = [(w, names.get(w)) for _, w in picks[:K]]
    for sc, w in picks[:K]:
        log.info("pick: %s (%s) score=%.0f", w[:12], str(names.get(w))[:24], sc)
    return chosen


def todays_activity(conn, day_start):
    """Windows entered today per wallet, from already-harvested tape (restart-safe)."""
    cur = conn.cursor()
    cur.execute("SELECT wallet, COUNT(DISTINCT window_start) FROM poly_trades "
                "WHERE asset=%s AND side='BUY' AND window_start >= %s GROUP BY wallet",
                (ASSET, int(day_start)))
    out = defaultdict(int, {r[0]: int(r[1]) for r in cur.fetchall()})
    cur.close()
    return out


# ---------- market plumbing ----------

def get_condition_id(http, conn, ws):
    cur = conn.cursor()
    cur.execute("SELECT condition_id FROM market_windows WHERE window_start=%s AND slug LIKE %s",
                (ws, SLUG_PREFIX + "%"))
    r = cur.fetchone()
    cur.close()
    if r and r[0]:
        return r[0]
    slug = f"{SLUG_PREFIX}-{ws}"
    try:
        resp = http.get(f"https://gamma-api.polymarket.com/events/keyset?slug={slug}", timeout=8)
        events = resp.json()
        events = events.get("events", []) if isinstance(events, dict) else events
        for ev in events:
            return ev["markets"][0].get("conditionId")
    except Exception as e:
        log.warning("gamma cid fail ws=%s: %s", ws, e)
    return None


def latest_ask(conn, ws, side):
    """Freshest collector-polled book ask for our side (must be <5s old)."""
    cur = conn.cursor()
    cur.execute("SELECT ts, up_best_ask, down_best_ask FROM poly_odds "
                "WHERE window_start=%s AND slug LIKE %s ORDER BY ts DESC LIMIT 1",
                (ws, SLUG_PREFIX + "%"))
    r = cur.fetchone()
    cur.close()
    if not r or time.time() - float(r[0]) > 5.0:
        return None
    return float(r[1]) if side == "UP" else float(r[2])


def resolve_outcome(http, ws):
    slug = f"{SLUG_PREFIX}-{ws}"
    try:
        resp = http.get(f"https://gamma-api.polymarket.com/events/keyset?slug={slug}", timeout=8)
        events = resp.json()
        events = events.get("events", []) if isinstance(events, dict) else events
        for ev in events:
            m = ev["markets"][0]
            if not m.get("closed"):
                return None
            op = m.get("outcomePrices")
            if isinstance(op, str):
                op = json.loads(op)
            if op and len(op) == 2:
                outs = m.get("outcomes", ["Up", "Down"])
                if isinstance(outs, str):
                    outs = json.loads(outs)
                return str(outs[0 if float(op[0]) > 0.5 else 1]).upper()
    except Exception:
        return None
    return None


def settle_open(http, conn):
    cur = conn.cursor()
    cur.execute("SELECT id, window_start, side, sim_entry_px, modeled_px, stake "
                "FROM mirror_paper_trades WHERE status='OPEN' AND window_start < %s",
                (int(time.time()) - 360,))
    rows = cur.fetchall()
    for tid, ws, side, sim_px, mod_px, stake in rows:
        outcome = resolve_outcome(http, int(ws))
        if outcome is None:
            continue
        won = int(outcome == side)
        px = float(sim_px) if sim_px else float(mod_px)
        pnl = (stake / px - stake) * (1 - FEE) if won else -stake
        cur.execute("UPDATE mirror_paper_trades SET status='SETTLED', outcome=%s, won=%s, pnl=%s "
                    "WHERE id=%s", (outcome, won, pnl, tid))
        log.info("SETTLED ws=%s %s -> %s won=%s pnl=%+.2f (sim_px=%.3f)",
                 ws, side, outcome, won, pnl, px)
    cur.close()


# ---------- main loop ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--no-harvest", action="store_true")
    args = ap.parse_args()

    conn = db()
    ensure_table(conn)
    if not args.no_harvest:
        harvest_recent(3)

    now = time.time()
    day_start = now - now % 86400
    chosen = select_specialists(conn, day_start)
    chosen_map = dict(chosen)
    log.info("selected %d specialists asof %s", len(chosen),
             datetime.datetime.fromtimestamp(day_start, datetime.timezone.utc).strftime("%Y-%m-%d"))
    if args.probe:
        return

    activity = todays_activity(conn, day_start)
    http = httpx.Client(timeout=8)
    cur_ws = 0
    cid = None
    seen_fb = set()          # (wallet, ws) first-buys already recorded
    mirrored_ws = set()      # windows already mirrored
    last_settle = 0.0

    while True:
        now = time.time()
        ds = now - now % 86400
        if ds != day_start:
            day_start = ds
            log.info("UTC day rollover -> re-harvest + re-select")
            if not args.no_harvest:
                harvest_recent(2)
            chosen = select_specialists(conn, day_start)
            chosen_map = dict(chosen)
            activity = todays_activity(conn, day_start)

        ws = int(now - now % 300)
        if ws != cur_ws:
            cur_ws = ws
            cid = get_condition_id(http, conn, ws)
            log.info("window %s cid=%s", ws, (cid or "?")[:16])

        elapsed = now - ws
        if cid and elapsed <= EL_CAP + 15:
            try:
                r = http.get("https://data-api.polymarket.com/trades",
                             params={"market": cid, "limit": 200})
                trades = r.json() if r.status_code == 200 else []
            except Exception:
                trades = []
            detect_ts = time.time()
            for t in sorted(trades, key=lambda x: float(x.get("timestamp", 0))):
                try:
                    w = str(t.get("proxyWallet", ""))
                    if w not in chosen_map or str(t.get("side")) != "BUY":
                        continue
                    fb_key = (w, ws)
                    if fb_key in seen_fb:
                        continue
                    seen_fb.add(fb_key)
                    activity[w] += 1
                    spec_ts = float(t.get("timestamp", 0))
                    px = float(t.get("price", 0))
                    side = str(t.get("outcome", "")).upper()
                    el = spec_ts - ws
                    latency = detect_ts - spec_ts
                    skip = None
                    if ws in mirrored_ws:
                        skip = "window_already_mirrored"
                    elif el > EL_CAP:
                        skip = "elapsed>%.0f" % EL_CAP
                    elif px > PX_CAP:
                        skip = "px>%.2f" % PX_CAP
                    elif activity[w] > WALLET_DAY_CAP:
                        skip = "wallet_day_cap"
                    ask = latest_ask(conn, ws, side)
                    status = "SKIPPED" if skip else "OPEN"
                    if not skip:
                        mirrored_ws.add(ws)
                    c2 = conn.cursor()
                    c2.execute(
                        "INSERT IGNORE INTO mirror_paper_trades (window_start, wallet, pseudonym, "
                        "side, spec_px, spec_ts, detect_ts, latency_sec, book_ask, sim_entry_px, "
                        "modeled_px, stake, status, skip_reason) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ws, w, str(t.get("pseudonym", ""))[:94], side, px, spec_ts, detect_ts,
                         latency, ask, ask, px + 0.02, STAKE, status, skip))
                    c2.close()
                    log.info("%s ws=%s %s (%s) px=%.3f el=%.0fs lat=%.1fs ask=%s%s",
                             "MIRROR" if not skip else "skip", ws, w[:10],
                             str(chosen_map.get(w))[:16], px, el, latency,
                             ("%.3f" % ask) if ask else "-",
                             "" if not skip else " [" + skip + "]")
                except Exception as e:
                    log.warning("trade parse: %s", e)

        if now - last_settle > 60:
            last_settle = now
            try:
                settle_open(http, conn)
            except Exception as e:
                log.warning("settle: %s", e)

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
