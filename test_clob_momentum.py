"""
CLOB Momentum Strategy backtest.
Instead of judges, use CLOB price movement as signal.
When UP ask drops fast → market expects UP → follow.
"""
import os, re, time
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")

from db_config import connect_db, fetch_all_dicts, fetch_one

conn = connect_db()
end_ts = time.time()
start_ts = end_ts - 480 * 3600

# Get all resolved windows
windows = fetch_all_dicts(conn, """
    SELECT window_start, actual_outcome, btc_start_price, btc_end_price
    FROM market_windows
    WHERE actual_outcome IN ('UP','DOWN')
      AND window_start >= %s AND window_start <= %s
    ORDER BY window_start
""", (int(start_ts), int(end_ts)))

print(f"Testing CLOB Momentum on {len(windows)} windows (20d)")
print()

# Parameters to sweep
configs = [
    # (label, entry_start, entry_end, ask_drop_threshold, max_ask, lookback_sec)
    ("drop3c_s90",  90, 200, 0.03, 0.54, 30),
    ("drop4c_s90",  90, 200, 0.04, 0.54, 30),
    ("drop5c_s90",  90, 200, 0.05, 0.54, 30),
    ("drop3c_s60",  60, 200, 0.03, 0.54, 30),
    ("drop4c_s60",  60, 200, 0.04, 0.54, 30),
    ("drop3c_s120", 120, 200, 0.03, 0.54, 30),
    ("drop4c_s120", 120, 200, 0.04, 0.54, 30),
    ("drop3c_lb20", 90, 200, 0.03, 0.54, 20),
    ("drop3c_lb45", 90, 200, 0.03, 0.54, 45),
    ("drop3c_a50",  90, 200, 0.03, 0.50, 30),
    ("drop5c_a58",  90, 200, 0.05, 0.58, 30),
]

print(f"{'Config':<16s} {'Trades':>6s} {'WR':>6s} {'PnL':>8s} {'PF':>6s} {'/h':>5s}")
print("-" * 52)

for label, entry_start, entry_end, threshold, max_ask, lookback in configs:
    wins = losses = 0
    total_pnl = 0
    stake = 10.0

    for w in windows:
        ws = int(w["window_start"])
        outcome = w["actual_outcome"]

        # Get poly_odds for this window, check at each second from entry_start to entry_end
        odds = fetch_all_dicts(conn, """
            SELECT ts, up_best_ask, down_best_ask FROM poly_odds
            WHERE window_start = %s AND ts >= %s AND ts <= %s
            ORDER BY ts
        """, (ws, ws + entry_start - lookback, ws + entry_end))

        if len(odds) < 10:
            continue

        # Find first entry signal
        entered = False
        for i, o in enumerate(odds):
            t = float(o["ts"])
            elapsed = t - ws
            if elapsed < entry_start or elapsed > entry_end:
                continue

            up_ask = float(o.get("up_best_ask") or 0.5)
            dn_ask = float(o.get("down_best_ask") or 0.5)

            # Find ask from lookback_sec ago
            target_ts = t - lookback
            prev_up = prev_dn = None
            for j in range(i-1, -1, -1):
                pt = float(odds[j]["ts"])
                if pt <= target_ts + 2 and pt >= target_ts - 2:
                    prev_up = float(odds[j].get("up_best_ask") or 0.5)
                    prev_dn = float(odds[j].get("down_best_ask") or 0.5)
                    break

            if prev_up is None or prev_dn is None:
                continue

            # CLOB momentum: ask dropped = market moving toward that direction
            up_drop = prev_up - up_ask    # positive = UP getting cheaper = market expects UP
            dn_drop = prev_dn - dn_ask    # positive = DOWN getting cheaper = market expects DOWN

            direction = None
            entry_price = None

            if up_drop >= threshold and up_ask <= max_ask:
                direction = "UP"
                entry_price = up_ask
            elif dn_drop >= threshold and dn_ask <= max_ask:
                direction = "DOWN"
                entry_price = dn_ask

            if direction and entry_price and 0.01 < entry_price < 0.99:
                shares = stake / entry_price
                won = outcome == direction
                if won:
                    pnl = shares - stake - stake * 0.03  # taker fee
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
        gross_w = sum(1 for _ in range(wins))  # placeholder
        gross_win = total_pnl + losses * stake  # approximate
        gross_loss = losses * stake
        pf = gross_win / gross_loss if gross_loss > 0 else 999
        tph = total / 480
        print(f"{label:<16s} {total:>6d} {wr:>5.1f}% ${total_pnl:>+7.0f} {pf:>5.2f} {tph:>4.1f}")
    else:
        print(f"{label:<16s}      0     -        -      -   0.0")

conn.close()
print("\nDone!")
