"""
CLOB Fade Strategy: when market OVERREACTS (ask drops too much too fast),
bet the OPPOSITE direction (mean reversion).

Hypothesis: 5-min BTC is mean-reverting. When CLOB says 70% UP,
BTC often comes back down by settlement. Fade the crowd.
"""
import os, time
os.environ.setdefault("MARIADB_PASSWORD", "hana1234")
os.environ.setdefault("MARIADB_PORT", "3400")

from db_config import connect_db, fetch_all_dicts, fetch_one

conn = connect_db()
end_ts = time.time()
start_ts = end_ts - 480 * 3600

windows = fetch_all_dicts(conn, """
    SELECT window_start, actual_outcome FROM market_windows
    WHERE actual_outcome IN ('UP','DOWN')
      AND window_start >= %s AND window_start <= %s
    ORDER BY window_start
""", (int(start_ts), int(end_ts)))

print(f"Testing on {len(windows)} windows")
print()

configs = [
    # Strategy 1: FADE - when CLOB strongly favors one side, bet the other
    # If UP ask < 0.40 (market 60%+ confident UP), bet DOWN (fade)
    ("fade_up40",  90, 200, "fade", 0.40, None),
    ("fade_up38",  90, 200, "fade", 0.38, None),
    ("fade_up35",  90, 200, "fade", 0.35, None),
    ("fade_up42",  90, 200, "fade", 0.42, None),

    # Strategy 2: FOLLOW STRONG - when market is VERY confident AND getting more confident
    ("strong_drop8c", 90, 200, "strong_follow", 0.08, 30),
    ("strong_drop10c", 90, 200, "strong_follow", 0.10, 30),
    ("strong_drop6c", 90, 200, "strong_follow", 0.06, 30),

    # Strategy 3: CHEAP CONTRARIAN - buy the losing side when it's extremely cheap
    # When DOWN ask < 0.30 (market says 70%+ UP), buy DOWN cheap
    ("cheap_contra30", 90, 240, "cheap_contra", 0.30, None),
    ("cheap_contra25", 90, 240, "cheap_contra", 0.25, None),
    ("cheap_contra35", 90, 240, "cheap_contra", 0.35, None),

    # Strategy 4: SPREAD SQUEEZE - enter when spread narrows (consensus forming)
    ("squeeze05", 90, 200, "squeeze", 0.05, None),
    ("squeeze03", 90, 200, "squeeze", 0.03, None),
    ("squeeze08", 90, 200, "squeeze", 0.08, None),
]

stake = 10.0
print(f"{'Config':<18s} {'Trades':>6s} {'WR':>6s} {'PnL':>8s} {'AvgW':>6s} {'AvgL':>6s} {'/h':>5s}")
print("-" * 58)

for label, entry_start, entry_end, strategy, param1, param2 in configs:
    wins = losses = 0
    total_pnl = 0.0
    win_pnl = loss_pnl = 0.0

    for w in windows:
        ws = int(w["window_start"])
        outcome = w["actual_outcome"]

        odds = fetch_all_dicts(conn, """
            SELECT ts, up_best_ask, down_best_ask FROM poly_odds
            WHERE window_start = %s AND ts >= %s AND ts <= %s ORDER BY ts
        """, (ws, ws + entry_start - 60, ws + entry_end))

        if len(odds) < 10:
            continue

        entered = False
        for i, o in enumerate(odds):
            t = float(o["ts"])
            elapsed = t - ws
            if elapsed < entry_start or elapsed > entry_end:
                continue

            up_ask = float(o.get("up_best_ask") or 0.5)
            dn_ask = float(o.get("down_best_ask") or 0.5)
            direction = None
            entry_price = None

            if strategy == "fade":
                # Fade: bet AGAINST the market's strong consensus
                if up_ask <= param1:  # Market says UP strongly
                    direction = "DOWN"  # We bet DOWN (fade)
                    entry_price = dn_ask
                elif dn_ask <= param1:  # Market says DOWN strongly
                    direction = "UP"  # We bet UP (fade)
                    entry_price = up_ask

            elif strategy == "strong_follow":
                # Only follow when drop is HUGE (>= param1 cents in param2 seconds)
                target_ts = t - param2
                prev = None
                for j in range(i-1, -1, -1):
                    if float(odds[j]["ts"]) <= target_ts + 2:
                        prev = odds[j]
                        break
                if prev:
                    prev_up = float(prev.get("up_best_ask") or 0.5)
                    prev_dn = float(prev.get("down_best_ask") or 0.5)
                    up_drop = prev_up - up_ask
                    dn_drop = prev_dn - dn_ask
                    if up_drop >= param1 and up_ask <= 0.54:
                        direction = "UP"
                        entry_price = up_ask
                    elif dn_drop >= param1 and dn_ask <= 0.54:
                        direction = "DOWN"
                        entry_price = dn_ask

            elif strategy == "cheap_contra":
                # Buy the cheap (losing) side — mean reversion bet
                if up_ask <= param1 and up_ask >= 0.10:
                    direction = "UP"
                    entry_price = up_ask
                elif dn_ask <= param1 and dn_ask >= 0.10:
                    direction = "DOWN"
                    entry_price = dn_ask

            elif strategy == "squeeze":
                # Enter when spread is very tight (market uncertain/balanced)
                spread = abs(up_ask - dn_ask)
                if spread <= param1 and up_ask <= 0.54 and dn_ask <= 0.54:
                    # When balanced, use BTC direction from ticks
                    # Simple: buy whichever is cheaper
                    if up_ask < dn_ask:
                        direction = "UP"
                        entry_price = up_ask
                    else:
                        direction = "DOWN"
                        entry_price = dn_ask

            if direction and entry_price and 0.10 < entry_price < 0.90:
                shares = stake / entry_price
                won = outcome == direction
                if won:
                    pnl = shares - stake - stake * 0.03
                    wins += 1
                    win_pnl += pnl
                else:
                    pnl = -stake
                    losses += 1
                    loss_pnl += pnl
                total_pnl += pnl
                entered = True
                break

    total = wins + losses
    if total > 0:
        wr = wins / total * 100
        avg_w = win_pnl / max(wins, 1)
        avg_l = loss_pnl / max(losses, 1)
        tph = total / 480
        print(f"{label:<18s} {total:>6d} {wr:>5.1f}% ${total_pnl:>+7.0f} ${avg_w:>+5.1f} ${avg_l:>+5.1f} {tph:>4.1f}")
    else:
        print(f"{label:<18s}      0     -        -      -      -   0.0")

conn.close()
print("\nDone!")
