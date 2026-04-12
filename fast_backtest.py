"""
Fast backtest engine: preload all data into RAM, then run strategies in-memory.
10-15 minutes → ~30 seconds.
"""
import os, time, sys, bisect
import numpy as np
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")

from db_config import connect_db, fetch_all_dicts

class FastData:
    """Preloads btc_ticks + poly_odds + signal_cache_log + market_windows into RAM."""

    def __init__(self, conn, hours=480):
        end_ts = time.time()
        start_ts = end_ts - hours * 3600
        print(f"Loading {hours}h of data...", end=" ", flush=True)

        t0 = time.time()
        cur = conn.cursor()

        # btc_ticks
        cur.execute("SELECT ts, price FROM btc_ticks WHERE ts >= %s ORDER BY ts", (int(start_ts),))
        rows = cur.fetchall()
        self.btc_ts = np.array([float(r[0]) for r in rows])
        self.btc_px = np.array([float(r[1]) for r in rows])
        t1 = time.time()

        # poly_odds
        cur.execute("SELECT ts, window_start, up_best_ask, down_best_ask FROM poly_odds WHERE ts >= %s ORDER BY ts", (int(start_ts),))
        rows2 = cur.fetchall()
        self.odds_ts = np.array([float(r[0]) for r in rows2])
        self.odds_ws = np.array([int(r[1]) for r in rows2])
        self.odds_up = np.array([float(r[2] or 0.5) for r in rows2])
        self.odds_dn = np.array([float(r[3] or 0.5) for r in rows2])
        t2 = time.time()

        # signal_cache_log (gate_allow=1 only)
        scl = fetch_all_dicts(conn, """
            SELECT ts, window_start, direction, avg_confidence, btc_move_pct, gate_ev, up_ask, down_ask
            FROM signal_cache_log WHERE gate_allow = 1 AND ts >= %s ORDER BY ts
        """, (int(start_ts),))
        self.scl = {}
        for s in scl:
            ws = int(s["window_start"])
            if ws not in self.scl:
                self.scl[ws] = []
            self.scl[ws].append(s)
        t3 = time.time()

        # market_windows
        mw = fetch_all_dicts(conn, """
            SELECT window_start, actual_outcome, btc_start_price, btc_end_price
            FROM market_windows WHERE actual_outcome IN ('UP','DOWN') AND btc_start_price > 0
              AND slug LIKE 'btc-updown-5m%%' AND window_start >= %s ORDER BY window_start
        """, (int(start_ts),))
        self.windows = mw
        t4 = time.time()

        print(f"Done in {t4-t0:.1f}s (btc:{t1-t0:.1f}s odds:{t2-t1:.1f}s scl:{t3-t2:.1f}s mw:{t4-t3:.1f}s)")
        print(f"  btc_ticks: {len(self.btc_ts):,}  poly_odds: {len(self.odds_ts):,}  windows: {len(self.windows)}")

    def btc_price_at(self, ts):
        """Get BTC price at or before timestamp. O(log n)."""
        idx = bisect.bisect_right(self.btc_ts, ts) - 1
        if idx < 0: return None
        return float(self.btc_px[idx])

    def btc_prices_range(self, ts_start, ts_end):
        """Get BTC prices in range. O(log n)."""
        i0 = bisect.bisect_left(self.btc_ts, ts_start)
        i1 = bisect.bisect_right(self.btc_ts, ts_end)
        return self.btc_ts[i0:i1], self.btc_px[i0:i1]

    def odds_at(self, ws, ts):
        """Get latest odds at or before timestamp for given window. O(log n)."""
        # Find odds for this window
        mask = self.odds_ws == ws
        if not np.any(mask):
            return None, None
        w_ts = self.odds_ts[mask]
        w_up = self.odds_up[mask]
        w_dn = self.odds_dn[mask]
        idx = bisect.bisect_right(w_ts, ts) - 1
        if idx < 0: return None, None
        return float(w_up[idx]), float(w_dn[idx])

    def odds_at_fast(self, ws, ts):
        """Faster odds lookup using global index."""
        idx = bisect.bisect_right(self.odds_ts, ts) - 1
        while idx >= 0 and self.odds_ws[idx] != ws:
            idx -= 1
            if self.odds_ts[idx] < ts - 300:
                return None, None
        if idx < 0: return None, None
        return float(self.odds_up[idx]), float(self.odds_dn[idx])


def run_strategy(data, strategy, params=None):
    """Run a strategy on preloaded data. Returns (trades, wins, losses, pnl)."""
    params = params or {}
    stake = float(params.get("stake", 15))
    btc_thr = float(params.get("btc_thr", 0.04))
    max_ask = float(params.get("max_ask", 0.50))
    e_start = float(params.get("entry_start", 60))
    e_end = float(params.get("entry_end", 200))
    min_conf = float(params.get("min_conf", 0.55))
    drift_sec = float(params.get("drift", 0.3))

    wins = losses = 0
    total_pnl = 0.0

    for w in data.windows:
        ws = int(w["window_start"])
        sp = float(w["btc_start_price"])
        outcome = w["actual_outcome"]

        direction = None
        entry_price = None

        if strategy in ("lag", "lag+conf", "lag+judge", "lag+momentum", "lag_or_judge"):
            # Lag arb signal
            ts_arr, px_arr = data.btc_prices_range(ws + e_start, ws + e_end)
            for i in range(0, len(ts_arr), 5):
                btc = float(px_arr[i])
                ts = float(ts_arr[i])
                move_pct = (btc - sp) / sp * 100
                if abs(move_pct) < btc_thr:
                    continue
                d = "UP" if move_pct > 0 else "DOWN"
                ua, da = data.odds_at_fast(ws, ts)
                if ua is None: continue
                ep = ua if d == "UP" else da
                if ep <= max_ask and 0.01 < ep < 0.99:
                    # Drift check
                    if drift_sec > 0:
                        ua2, da2 = data.odds_at_fast(ws, ts + drift_sec)
                        if ua2 is not None:
                            ep2 = ua2 if d == "UP" else da2
                            if ep2 > max_ask: continue
                            if 0.01 < ep2 < 0.99: ep = ep2
                    direction = d
                    entry_price = ep
                    break

        # Judge signal
        judge_dir = None
        judge_ep = None
        judge_conf = 0
        judge_btc = 0
        scl_entries = data.scl.get(ws, [])
        if scl_entries:
            s = scl_entries[0]
            judge_dir = s["direction"]
            judge_conf = float(s.get("avg_confidence") or 0)
            judge_btc = float(s.get("btc_move_pct") or 0)
            ua = float(s.get("up_ask") or 0.5)
            da = float(s.get("down_ask") or 0.5)
            jep = ua if judge_dir == "UP" else da
            if 0.01 < jep < 0.99 and jep <= 0.54:
                judge_ep = jep

        # Apply strategy filters
        if strategy == "lag+conf":
            if not (direction and judge_dir == direction and judge_conf >= min_conf):
                direction = None
        elif strategy == "lag+judge":
            if not (direction and judge_dir == direction):
                direction = None
        elif strategy == "lag+momentum":
            if direction:
                if (direction == "UP" and judge_btc < 0) or (direction == "DOWN" and judge_btc > 0):
                    direction = None
        elif strategy == "judge":
            direction = judge_dir if judge_conf >= min_conf else None
            entry_price = judge_ep
        elif strategy == "lag_or_judge":
            if direction is None and judge_dir and judge_ep and judge_conf >= min_conf:
                direction = judge_dir
                entry_price = judge_ep

        if direction and entry_price and 0.01 < entry_price < 0.99:
            shares = stake / entry_price
            won = outcome == direction
            if won:
                pnl = shares - stake - stake * 0.03
                wins += 1
            else:
                pnl = -stake
                losses += 1
            total_pnl += pnl

    return wins + losses, wins, losses, total_pnl


if __name__ == "__main__":
    conn = connect_db()
    data = FastData(conn, hours=480)

    strategies = [
        ("lag",           "Lag Arb only"),
        ("lag+conf",      "Lag + conf>=0.55"),
        ("lag+momentum",  "Lag + momentum"),
        ("lag+judge",     "Lag + judge dir"),
        ("judge",         "Judge only"),
        ("lag_or_judge",  "Lag OR Judge"),
    ]

    periods = [24, 48, 72, 120, 240, 480]

    print(f"\n{'Strategy':<18s}", end="")
    for h in periods:
        print(f" | {h}h".ljust(25), end="")
    print()
    print("-" * 170)

    for key, label in strategies:
        # Reload data for each period
        print(f"{label:<18s}", end="", flush=True)
        for h in periods:
            d = FastData(conn, hours=h) if h != 480 else data
            t0 = time.time()
            total, w, l, pnl = run_strategy(d, key)
            elapsed = time.time() - t0
            if total > 0:
                wr = w / total * 100
                gw = pnl + l * 15
                pf = gw / (l * 15) if l > 0 else 999
                cell = f"{total}t {wr:.0f}% ${pnl:+.0f} PF{pf:.1f}"
            else:
                cell = "0t"
            print(f" | {cell:<23s}", end="", flush=True)
        print()

    conn.close()
    print("\nDone!")
