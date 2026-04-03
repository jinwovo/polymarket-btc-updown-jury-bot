"""
Lag Arbitrage backtest: enter when Binance moved but CLOB hasn't caught up.
BTC moves 0.04%+ → check if CLOB still shows balanced odds → enter.
"""
import os, time
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")

from db_config import connect_db, fetch_all_dicts, fetch_one

conn = connect_db()
end_ts = time.time()
start_ts = end_ts - 480 * 3600

windows = fetch_all_dicts(conn, """
    SELECT window_start, actual_outcome, btc_start_price, btc_end_price
    FROM market_windows
    WHERE actual_outcome IN ('UP','DOWN')
      AND window_start >= %s AND window_start <= %s
      AND btc_start_price > 0
    ORDER BY window_start
""", (int(start_ts), int(end_ts)))

print(f"Testing Lag Arb on {len(windows)} windows (20d)")
print()

configs = [
    # (label, btc_threshold, max_clob_ask, entry_start, entry_end, check_interval)
    # btc_threshold: BTC must move this much from start
    # max_clob_ask: CLOB ask must still be above this (= hasn't repriced)
    ("lag_04_a50", 0.04, 0.50, 60, 200, 5),
    ("lag_04_a48", 0.04, 0.48, 60, 200, 5),
    ("lag_04_a46", 0.04, 0.46, 60, 200, 5),
    ("lag_03_a50", 0.03, 0.50, 60, 200, 5),
    ("lag_03_a48", 0.03, 0.48, 60, 200, 5),
    ("lag_05_a50", 0.05, 0.50, 60, 200, 5),
    ("lag_05_a48", 0.05, 0.48, 60, 200, 5),
    ("lag_04_a50_s90", 0.04, 0.50, 90, 200, 5),
    ("lag_04_a50_s45", 0.04, 0.50, 45, 200, 5),
]

stake = 15.0
print(f"{'Config':<18s} {'Trades':>6s} {'WR':>6s} {'PnL':>8s} {'PF':>6s} {'/day':>5s}")
print("-" * 54)

for label, btc_thr, max_ask, e_start, e_end, interval in configs:
    wins = losses = 0
    total_pnl = 0.0

    for w in windows:
        ws = int(w["window_start"])
        sp = float(w["btc_start_price"])
        outcome = w["actual_outcome"]

        # Load BTC ticks for this window
        ticks = fetch_all_dicts(conn, """
            SELECT ts, price FROM btc_ticks
            WHERE ts >= %s AND ts <= %s ORDER BY ts
        """, (ws + e_start, ws + e_end))

        if not ticks:
            continue

        entered = False
        for i in range(0, len(ticks), interval):
            t = ticks[i]
            ts = float(t["ts"])
            btc = float(t["price"])
            elapsed = ts - ws

            # BTC moved enough?
            move_pct = (btc - sp) / sp * 100
            if abs(move_pct) < btc_thr:
                continue

            direction = "UP" if move_pct > 0 else "DOWN"

            # Check CLOB: has it repriced? Get latest odds
            odds = fetch_one(conn, """
                SELECT up_best_ask, down_best_ask FROM poly_odds
                WHERE window_start = %s AND ts <= %s
                ORDER BY ts DESC LIMIT 1
            """, (ws, ts))

            if not odds:
                continue

            up_ask = float(odds[0] or 0.5)
            dn_ask = float(odds[1] or 0.5)

            # Our side's ask price
            entry_price = up_ask if direction == "UP" else dn_ask

            # LAG CHECK: if CLOB hasn't repriced, our side is still "cheap"
            # (ask hasn't dropped below max_clob_ask = market doesn't know yet)
            if entry_price > max_ask:
                continue  # CLOB already repriced, no lag to exploit

            # Valid entry: BTC moved but CLOB is still balanced
            if entry_price <= 0.01 or entry_price >= 0.99:
                continue

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
        tpd = total / 20
        print(f"{label:<18s} {total:>6d} {wr:>5.1f}% ${total_pnl:>+7.0f} {pf:>5.2f} {tpd:>4.1f}")
    else:
        print(f"{label:<18s}      0     -        -      -   0.0")

conn.close()
print("\nDone!")
