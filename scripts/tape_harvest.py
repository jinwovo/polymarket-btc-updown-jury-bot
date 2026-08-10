"""Harvest the public Polymarket trade tape for updown windows into MariaDB.

Server-side data (Gamma resolution + data-api trades) -- works regardless of
local collector uptime. Resumable: windows already harvested are skipped.

Usage: python scripts/tape_harvest.py [--asset btc5|eth5|sol5] [--days 14]

Tables:
  tape_windows(window_start, asset, slug, condition_id, outcome, n_trades)
  poly_trades(ts, window_start, asset, wallet, pseudonym, side, outcome_side,
              size, price, tx_hash, dedup_key UNIQUE)
"""
import argparse
import asyncio
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env.secrets")
import httpx
import pymysql

SLUG_PREFIX = {"btc5": "btc-updown-5m", "eth5": "eth-updown-5m", "sol5": "sol-updown-5m"}
GAMMA_BATCH = 20
CONCURRENCY = 10
MAX_PAGES = 4  # x1000 rows per window (desc order: page cap drops the EARLIEST trades)


def db_connect():
    return pymysql.connect(host="127.0.0.1", port=3400, user="root",
                           password=os.getenv("MARIADB_PASSWORD", ""),
                           database="polymarket_btc_updown", autocommit=True)


def ensure_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tape_windows (
            window_start BIGINT NOT NULL,
            asset VARCHAR(8) NOT NULL,
            slug VARCHAR(64) NOT NULL,
            condition_id VARCHAR(80) NULL,
            outcome VARCHAR(8) NULL,
            n_trades INT NULL,
            PRIMARY KEY (window_start, asset)
        ) ENGINE=InnoDB""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS poly_trades (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts DOUBLE NOT NULL,
            window_start BIGINT NOT NULL,
            asset VARCHAR(8) NOT NULL,
            wallet VARCHAR(48) NOT NULL,
            pseudonym VARCHAR(96) NULL,
            side VARCHAR(4) NOT NULL,
            outcome_side VARCHAR(8) NOT NULL,
            size DOUBLE NOT NULL,
            price DOUBLE NOT NULL,
            tx_hash VARCHAR(80) NULL,
            dedup_key CHAR(40) NOT NULL,
            UNIQUE KEY uq_pt_dedup (dedup_key),
            INDEX idx_pt_wallet_ts (wallet, ts),
            INDEX idx_pt_ws (window_start, asset)
        ) ENGINE=InnoDB""")
    cur.close()


async def gamma_resolve(http, slugs):
    """Batch-resolve slugs -> {slug: (condition_id, outcome|None)}. outcome None = unresolved."""
    out = {}
    qs = "&".join(f"slug={s}" for s in slugs)
    for attempt in range(4):
        try:
            r = await http.get(f"https://gamma-api.polymarket.com/events/keyset?{qs}")
            if r.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            p = r.json()
            events = p.get("events", []) if isinstance(p, dict) else p
            for ev in events:
                try:
                    m = ev["markets"][0]
                    slug = ev.get("slug") or m.get("slug")
                    cid = m.get("conditionId")
                    outcome = None
                    if m.get("closed"):
                        op = m.get("outcomePrices")
                        if isinstance(op, str):
                            import json as _j
                            op = _j.loads(op)
                        if op and len(op) == 2:
                            outcomes = m.get("outcomes", ["Up", "Down"])
                            if isinstance(outcomes, str):
                                import json as _j
                                outcomes = _j.loads(outcomes)
                            win_idx = 0 if float(op[0]) > 0.5 else 1
                            outcome = str(outcomes[win_idx]).upper()
                    out[slug] = (cid, outcome)
                except Exception:
                    continue
            return out
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))
    return out


async def fetch_trades(http, sem, cid):
    rows = []
    async with sem:
        for page in range(MAX_PAGES):
            for attempt in range(4):
                try:
                    r = await http.get("https://data-api.polymarket.com/trades",
                                       params={"market": cid, "limit": 1000, "offset": page * 1000})
                    if r.status_code == 429:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    batch = r.json()
                    break
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    batch = None
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
    return rows


def insert_trades(conn, asset, ws, trades):
    if not trades:
        return 0
    cur = conn.cursor()
    vals = []
    for t in trades:
        try:
            tx = str(t.get("transactionHash", ""))[:78]
            w = str(t.get("proxyWallet", ""))[:46]
            side = str(t.get("side", ""))[:4]
            osd = str(t.get("outcome", ""))[:8]
            sz = float(t.get("size", 0))
            px = float(t.get("price", 0))
            ts = float(t.get("timestamp", 0))
            if not w or sz <= 0 or px <= 0:
                continue
            dk = hashlib.sha1(f"{tx}|{w}|{osd}|{side}|{sz}|{px}|{ts}".encode()).hexdigest()
            vals.append((ts, ws, asset, w, str(t.get("pseudonym", ""))[:94], side, osd, sz, px, tx, dk))
        except Exception:
            continue
    if not vals:
        return 0
    cur.executemany(
        "INSERT IGNORE INTO poly_trades (ts, window_start, asset, wallet, pseudonym, side, "
        "outcome_side, size, price, tx_hash, dedup_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        vals)
    cur.close()
    return len(vals)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc5", choices=list(SLUG_PREFIX))
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    prefix = SLUG_PREFIX[args.asset]
    conn = db_connect()
    ensure_tables(conn)
    cur = conn.cursor()
    cur.execute("SELECT window_start FROM tape_windows WHERE asset=%s AND n_trades IS NOT NULL", (args.asset,))
    done = {int(r[0]) for r in cur.fetchall()}
    cur.close()

    now = int(time.time())
    end_ws = now - now % 300 - 600  # last surely-resolved window
    start_ws = end_ws - args.days * 86400
    # newest first: recent days matter most (recency-weighted leaderboard) and
    # land in the DB early, so analysis can start before the tail backfills
    all_ws = [w for w in range(end_ws, start_ws - 1, -300) if w not in done]
    print(f"[harvest] {args.asset}: {len(all_ws)} windows to do ({len(done)} already done)", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()
    n_windows = 0
    n_trades_total = 0

    async with httpx.AsyncClient(timeout=25) as http:
        for i in range(0, len(all_ws), GAMMA_BATCH):
            chunk = all_ws[i:i + GAMMA_BATCH]
            slugs = [f"{prefix}-{w}" for w in chunk]
            resolved = await gamma_resolve(http, slugs)

            async def handle(ws_val):
                nonlocal n_windows, n_trades_total
                slug = f"{prefix}-{ws_val}"
                cid, outcome = resolved.get(slug, (None, None))
                c2 = conn.cursor()
                if cid is None:
                    c2.execute("INSERT IGNORE INTO tape_windows (window_start, asset, slug, n_trades) "
                               "VALUES (%s,%s,%s,0)", (ws_val, args.asset, slug))
                    c2.close()
                    return
                trades = await fetch_trades(http, sem, cid)
                n = insert_trades(conn, args.asset, ws_val, trades)
                c2.execute(
                    "INSERT INTO tape_windows (window_start, asset, slug, condition_id, outcome, n_trades) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "condition_id=VALUES(condition_id), outcome=VALUES(outcome), n_trades=VALUES(n_trades)",
                    (ws_val, args.asset, slug, cid, outcome, n))
                c2.close()
                n_windows += 1
                n_trades_total += n

            await asyncio.gather(*(handle(w) for w in chunk))
            if (i // GAMMA_BATCH) % 10 == 0:
                el = time.time() - t0
                rate = (i + len(chunk)) / el if el > 0 else 0
                eta = (len(all_ws) - i - len(chunk)) / rate / 60 if rate > 0 else -1
                print(f"[harvest] {i + len(chunk)}/{len(all_ws)} windows, {n_trades_total:,} trades, "
                      f"eta {eta:.0f}min", flush=True)

    print(f"[harvest] DONE: {n_windows} windows, {n_trades_total:,} trades in {(time.time()-t0)/60:.1f}min", flush=True)
    conn.close()

if __name__ == "__main__":
    asyncio.run(main())
