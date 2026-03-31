"""Analyze: do CLOB odds predict settlement outcome near expiry?"""
import os, sys
os.environ.setdefault("MARIADB_PORT", "3400")

from config import config  # loads dotenv
from db_config import connect_db, fetch_all_dicts

conn = connect_db()

windows = fetch_all_dicts(conn, """
    SELECT window_start, actual_outcome
    FROM market_windows
    WHERE actual_outcome IN ('UP','DOWN')
    ORDER BY window_start DESC
    LIMIT 500
""")
print(f"Analyzing {len(windows)} windows...\n")

results = {}
for sec in [5, 10, 15, 20, 30]:
    results[sec] = {'correct': 0, 'wrong': 0, 'total': 0, 'strong_correct': 0, 'strong_wrong': 0}

for w in windows:
    ws = int(w["window_start"]); we = ws + 300; outcome = w["actual_outcome"]
    for sec in [5, 10, 15, 20, 30]:
        odds = fetch_all_dicts(conn, """
            SELECT up_best_ask, down_best_ask FROM poly_odds
            WHERE window_start = %s AND ts >= %s AND ts <= %s
            ORDER BY ts DESC LIMIT 5
        """, (ws, we - sec, we - max(1, sec - 8)))
        if not odds: continue
        ua = [float(o["up_best_ask"]) for o in odds if o.get("up_best_ask") and 0.01 < float(o["up_best_ask"]) < 0.99]
        da = [float(o["down_best_ask"]) for o in odds if o.get("down_best_ask") and 0.01 < float(o["down_best_ask"]) < 0.99]
        if not ua or not da: continue
        avg_up = sum(ua)/len(ua); avg_dn = sum(da)/len(da)
        pred = "UP" if avg_up > avg_dn else "DOWN"
        results[sec]['total'] += 1
        if pred == outcome:
            results[sec]['correct'] += 1
            if max(avg_up, avg_dn) >= 0.65:
                results[sec]['strong_correct'] += 1
        else:
            results[sec]['wrong'] += 1
            if max(avg_up, avg_dn) >= 0.65:
                results[sec]['strong_wrong'] += 1

print("=== CLOB Odds -> Settlement Outcome Accuracy ===")
print(f"{'Sec':>4}  {'All':>12}  {'Accuracy':>8}  {'Strong(>=0.65)':>20}")
for sec in sorted(results.keys()):
    r = results[sec]
    if r['total'] > 0:
        acc = r['correct'] / r['total'] * 100
        st = r['strong_correct'] + r['strong_wrong']
        sacc = r['strong_correct'] / st * 100 if st > 0 else 0
        print(f"  {sec:2d}s  {r['correct']:3d}/{r['total']:3d}    {acc:5.1f}%    {r['strong_correct']:3d}/{st:3d} ({sacc:.1f}%)")

# Paper losses at settlement
print("\n=== Paper Settlement Losses: CLOB Warning Check (15-25s before) ===")
losses = fetch_all_dicts(conn, """
    SELECT window_start, direction, stake, pnl, entry_price
    FROM paper_trades
    WHERE archived_at IS NULL AND won = 0 AND close_reason = 'expiry_settlement'
    ORDER BY opened_at DESC LIMIT 50
""")
saved = 0; saveable = 0
for pt in losses:
    ws = int(pt["window_start"]); we = ws + 300; d = pt["direction"]
    stake = float(pt["stake"]); ep = float(pt.get("entry_price") or 0.5)
    odds = fetch_all_dicts(conn, """
        SELECT up_best_ask, down_best_ask, up_best_bid, down_best_bid FROM poly_odds
        WHERE window_start = %s AND ts >= %s AND ts <= %s ORDER BY ts DESC LIMIT 5
    """, (ws, we - 25, we - 10))
    if not odds: continue
    opp_vals = [float(o["down_best_ask"]) for o in odds if o.get("down_best_ask")] if d=="UP" else [float(o["up_best_ask"]) for o in odds if o.get("up_best_ask")]
    bid_vals = [float(o.get("up_best_bid") or 0) for o in odds] if d=="UP" else [float(o.get("down_best_bid") or 0) for o in odds]
    if not opp_vals: continue
    opp = sum(opp_vals)/len(opp_vals)
    bid = sum(v for v in bid_vals if v>0) / max(1, sum(1 for v in bid_vals if v>0)) if any(v>0 for v in bid_vals) else 0.05
    if opp >= 0.55:
        shares = stake / ep; exit_val = shares * max(bid, 0.03)
        improve = exit_val
        saved += improve; saveable += 1
        if saveable <= 10:
            print(f"  {d} lost ${float(pt['pnl']):+6.0f} stk=${stake:5.0f} | opp_ask={opp:.2f} our_bid={bid:.2f} | could exit ${exit_val:.0f}")

print(f"\n  {saveable}/{len(losses)} settlement losses had CLOB warning (opp>=0.55)")
print(f"  Potential save: ${saved:.0f} (exit at bid vs total loss)")
conn.close()
