"""
Lag Arb full test: all periods + drift simulation (300ms, 1s)
"""
import os, time, sys
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")
from db_config import connect_db, fetch_all_dicts, fetch_one

conn = connect_db()
end_ts = time.time()

def run_lag_arb(hours, btc_thr=0.04, max_ask=0.50, e_start=60, e_end=200, drift_sec=0.0):
    start_ts = end_ts - hours * 3600
    windows = fetch_all_dicts(conn, """
        SELECT window_start, actual_outcome, btc_start_price
        FROM market_windows
        WHERE actual_outcome IN ('UP','DOWN') AND btc_start_price > 0
          AND window_start >= %s AND window_start <= %s
        ORDER BY window_start
    """, (int(start_ts), int(end_ts)))

    wins = losses = skipped_drift = 0
    total_pnl = 0.0
    stake = 15.0

    for w in windows:
        ws = int(w["window_start"])
        sp = float(w["btc_start_price"])
        outcome = w["actual_outcome"]

        ticks = fetch_all_dicts(conn, """
            SELECT ts, price FROM btc_ticks
            WHERE ts >= %s AND ts <= %s ORDER BY ts
        """, (ws + e_start, ws + e_end))
        if not ticks:
            continue

        entered = False
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
            entry_price = up_ask if direction == "UP" else dn_ask

            if entry_price > max_ask or entry_price <= 0.01 or entry_price >= 0.99:
                continue

            # Drift simulation: check if ask changed after drift_sec
            if drift_sec > 0:
                later = fetch_one(conn, """
                    SELECT up_best_ask, down_best_ask FROM poly_odds
                    WHERE window_start = %s AND ts >= %s AND ts <= %s
                    ORDER BY ts ASC LIMIT 1
                """, (ws, ts + drift_sec * 0.5, ts + drift_sec * 1.5))
                if later:
                    later_price = float(later[0] or 0.5) if direction == "UP" else float(later[1] or 0.5)
                    if later_price > max_ask:
                        skipped_drift += 1
                        continue
                    if 0.01 < later_price < 0.99:
                        entry_price = later_price

            shares = stake / entry_price
            won = outcome == direction
            if won:
                pnl = shares - stake - stake * 0.03
                wins += 1
            else:
                pnl = -stake
                losses += 1
            total_pnl += pnl
            entered = True
            break

    total = wins + losses
    if total > 0:
        wr = wins / total * 100
        gross_w = total_pnl + losses * stake
        pf = gross_w / (losses * stake) if losses > 0 else 999
        return total, wr, total_pnl, pf, skipped_drift
    return 0, 0, 0, 0, 0

# Full period test
print("=== Lag Arb (btc>=0.04%, ask<=0.50) -- Period Check ===")
print(f"{'Period':>6s} {'No Drift':>30s} | {'300ms Drift':>30s} | {'1s Drift':>30s}")
print("-" * 105)

for hours in [12, 24, 48, 72, 120, 240, 360, 480]:
    results = []
    for drift in [0, 0.3, 1.0]:
        t, wr, pnl, pf, skip = run_lag_arb(hours, drift_sec=drift)
        results.append(f"{t}t {wr:.0f}% ${pnl:+.0f} PF{pf:.2f}" if t > 0 else "0t")
    print(f"{hours:>4d}h  {results[0]:>30s} | {results[1]:>30s} | {results[2]:>30s}")

print()
print("=== lag_05_a50 (stricter) ===")
print(f"{'Period':>6s} {'No Drift':>30s} | {'300ms Drift':>30s}")
print("-" * 70)
for hours in [24, 72, 240, 480]:
    results = []
    for drift in [0, 0.3]:
        t, wr, pnl, pf, skip = run_lag_arb(hours, btc_thr=0.05, drift_sec=drift)
        results.append(f"{t}t {wr:.0f}% ${pnl:+.0f} PF{pf:.2f}" if t > 0 else "0t")
    print(f"{hours:>4d}h  {results[0]:>30s} | {results[1]:>30s}")

conn.close()
print("\nDone!")
