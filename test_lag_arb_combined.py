"""
Lag Arb + Judge combinations test.
1. Lag Arb only
2. Lag Arb + conf>=0.55
3. Lag Arb + momentum agree
4. Lag Arb + judge direction match
5. Judge only (current strategy for comparison)
6. Lag Arb OR Judge (either signal triggers entry)
"""
import os, time
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")
from db_config import connect_db, fetch_all_dicts, fetch_one

conn = connect_db()
end_ts = time.time()

def run_test(hours, strategy, btc_thr=0.04, max_ask=0.50):
    start_ts = end_ts - hours * 3600
    windows = fetch_all_dicts(conn, """
        SELECT window_start, actual_outcome, btc_start_price
        FROM market_windows
        WHERE actual_outcome IN ('UP','DOWN') AND btc_start_price > 0
          AND window_start >= %s AND window_start <= %s
        ORDER BY window_start
    """, (int(start_ts), int(end_ts)))

    wins = losses = 0
    total_pnl = 0.0
    stake = 15.0

    for w in windows:
        ws = int(w["window_start"])
        sp = float(w["btc_start_price"])
        outcome = w["actual_outcome"]

        # Get signal_cache_log entries (judge signals)
        scl = fetch_all_dicts(conn, """
            SELECT ts, direction, avg_confidence, btc_move_pct, gate_allow, gate_ev,
                   up_ask, down_ask
            FROM signal_cache_log
            WHERE window_start = %s AND gate_allow = 1
            ORDER BY ts ASC LIMIT 1
        """, (ws,))

        # Get BTC ticks for lag detection
        ticks = fetch_all_dicts(conn, """
            SELECT ts, price FROM btc_ticks
            WHERE ts >= %s AND ts <= %s ORDER BY ts
        """, (ws + 60, ws + 200))

        # --- Lag Arb signal ---
        lag_signal = None
        lag_entry_price = None
        for i in range(0, len(ticks), 5):
            t = ticks[i]
            ts = float(t["ts"])
            btc = float(t["price"])
            move_pct = (btc - sp) / sp * 100
            if abs(move_pct) < btc_thr:
                continue
            direction = "UP" if move_pct > 0 else "DOWN"
            odds = fetch_one(conn, """
                SELECT up_best_ask, down_best_ask FROM poly_odds
                WHERE window_start = %s AND ts <= %s ORDER BY ts DESC LIMIT 1
            """, (ws, ts))
            if not odds:
                continue
            up_ask = float(odds[0] or 0.5)
            dn_ask = float(odds[1] or 0.5)
            ep = up_ask if direction == "UP" else dn_ask
            if ep <= max_ask and 0.01 < ep < 0.99:
                lag_signal = direction
                lag_entry_price = ep
                break

        # --- Judge signal ---
        judge_signal = None
        judge_entry_price = None
        judge_conf = 0
        judge_btc_move = 0
        if scl:
            s = scl[0]
            judge_signal = s["direction"]
            judge_conf = float(s.get("avg_confidence") or 0)
            judge_btc_move = float(s.get("btc_move_pct") or 0)
            ua = float(s.get("up_ask") or 0.5)
            da = float(s.get("down_ask") or 0.5)
            ep = ua if judge_signal == "UP" else da
            if 0.01 < ep < 0.99 and ep <= 0.54:
                judge_entry_price = ep

        # --- Strategy selection ---
        direction = None
        entry_price = None

        if strategy == "lag_only":
            direction = lag_signal
            entry_price = lag_entry_price

        elif strategy == "lag+conf":
            if lag_signal and judge_signal == lag_signal and judge_conf >= 0.55:
                direction = lag_signal
                entry_price = lag_entry_price

        elif strategy == "lag+momentum":
            if lag_signal and judge_btc_move != 0:
                btc_agrees = (lag_signal == "UP" and judge_btc_move > 0) or (lag_signal == "DOWN" and judge_btc_move < 0)
                if btc_agrees:
                    direction = lag_signal
                    entry_price = lag_entry_price

        elif strategy == "lag+judge_dir":
            if lag_signal and judge_signal == lag_signal:
                direction = lag_signal
                entry_price = lag_entry_price

        elif strategy == "judge_only":
            if judge_signal and judge_entry_price and judge_conf >= 0.55:
                direction = judge_signal
                entry_price = judge_entry_price

        elif strategy == "lag_OR_judge":
            # Either signal triggers — more trades
            if lag_signal and lag_entry_price:
                direction = lag_signal
                entry_price = lag_entry_price
            elif judge_signal and judge_entry_price and judge_conf >= 0.55:
                direction = judge_signal
                entry_price = judge_entry_price

        elif strategy == "lag03":
            # Lower threshold for more trades
            for i in range(0, len(ticks), 5):
                t = ticks[i]
                ts = float(t["ts"])
                btc = float(t["price"])
                move_pct = (btc - sp) / sp * 100
                if abs(move_pct) < 0.03:
                    continue
                d = "UP" if move_pct > 0 else "DOWN"
                odds = fetch_one(conn, """
                    SELECT up_best_ask, down_best_ask FROM poly_odds
                    WHERE window_start = %s AND ts <= %s ORDER BY ts DESC LIMIT 1
                """, (ws, ts))
                if not odds:
                    continue
                ua2 = float(odds[0] or 0.5)
                da2 = float(odds[1] or 0.5)
                ep2 = ua2 if d == "UP" else da2
                if ep2 <= 0.50 and 0.01 < ep2 < 0.99:
                    direction = d
                    entry_price = ep2
                    break

        if direction and entry_price:
            shares = stake / entry_price
            won = outcome == direction
            if won:
                pnl = shares - stake - stake * 0.03
                wins += 1
            else:
                pnl = -stake
                losses += 1
            total_pnl += pnl

    total = wins + losses
    if total > 0:
        wr = wins / total * 100
        gross_w = total_pnl + losses * stake
        pf = gross_w / (losses * stake) if losses > 0 else 999
        return total, wr, total_pnl, pf
    return 0, 0, 0, 0

strategies = [
    ("lag_only",     "Lag Arb only"),
    ("lag+conf",     "Lag+conf>=0.55"),
    ("lag+momentum", "Lag+momentum"),
    ("lag+judge_dir","Lag+judge dir"),
    ("judge_only",   "Judge only"),
    ("lag_OR_judge", "Lag OR Judge"),
    ("lag03",        "Lag 0.03%"),
]

print(f"{'Strategy':<18s}", end="")
for h in [24, 72, 240, 480]:
    print(f" | {h}h".ljust(28), end="")
print()
print("-" * 130)

for key, label in strategies:
    print(f"{label:<18s}", end="")
    for h in [24, 72, 240, 480]:
        t, wr, pnl, pf = run_test(h, key)
        cell = f"{t}t {wr:.0f}% ${pnl:+.0f} PF{pf:.1f}" if t > 0 else "0t"
        print(f" | {cell:<26s}", end="")
    print()

print("\nDone!")
