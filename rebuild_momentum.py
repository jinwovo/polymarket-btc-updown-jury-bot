"""
Rebuild signal_cache_log with PURE MOMENTUM strategy (no judges).

Simple rules:
  1. BTC moved >= threshold% from start_price
  2. Direction maintained for stability_sec continuously
  3. Entry between entry_start and entry_end seconds
  4. Only stores gate_allow=1 entries (like original rebuild)

Usage:
    python rebuild_momentum.py --last-hours 480
    python rebuild_momentum.py --last-hours 480 --threshold 0.05 --stability 20
"""
import argparse
import json
import logging
import os
import time as _time

os.environ.setdefault("MARIADB_PORT", "3400")

from db_config import connect_db, execute_write, fetch_all_dicts, fetch_one

logger = logging.getLogger("rebuild_momentum")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def rebuild(last_hours: float, threshold_pct: float = 0.05, stability_sec: float = 20.0,
            entry_start: float = 60.0, entry_end: float = 240.0, clear: bool = False):
    conn = connect_db()
    end_ts = _time.time()
    start_ts = end_ts - last_hours * 3600

    if clear:
        execute_write(conn, "DELETE FROM signal_cache_log WHERE ts >= %s AND ts <= %s",
                      (start_ts, end_ts))
        conn.commit()
        logger.info("Cleared signal_cache_log for %.0fh", last_hours)

    # Get resolved windows
    windows = fetch_all_dicts(conn, """
        SELECT window_start, actual_outcome
        FROM market_windows
        WHERE actual_outcome IN ('UP','DOWN')
          AND window_start >= %s AND window_start <= %s
        ORDER BY window_start
    """, (int(start_ts), int(end_ts)))
    logger.info("Processing %d windows (%.0fh), threshold=%.3f%%, stability=%.0fs, entry=%s-%ss",
                len(windows), last_hours, threshold_pct, stability_sec, entry_start, entry_end)

    inserted = 0
    windows_with_signal = 0

    for wi, w in enumerate(windows):
        ws = int(w["window_start"])
        we = ws + 300

        # Load ticks for this window + lookback
        ticks = fetch_all_dicts(conn, """
            SELECT ts, price FROM btc_ticks
            WHERE ts >= %s AND ts <= %s ORDER BY ts
        """, (ws - 60, we))
        if len(ticks) < 20:
            continue

        tick_ts = [float(t["ts"]) for t in ticks]
        tick_px = [float(t["price"]) for t in ticks]

        # Get start price (first tick at or after window start)
        start_price = None
        for i, t in enumerate(tick_ts):
            if t >= ws:
                start_price = tick_px[i]
                break
        if start_price is None or start_price <= 0:
            continue

        # Load odds for this window
        odds_rows = fetch_all_dicts(conn, """
            SELECT ts, up_best_ask, down_best_ask, up_best_bid, down_best_bid
            FROM poly_odds WHERE window_start = %s ORDER BY ts
        """, (ws,))

        # Scan from entry_start to entry_end
        check_interval = 2.0  # check every 2 seconds (faster than 1s)
        t = ws + entry_start
        signal_found = False

        while t <= ws + entry_end and t < we:
            elapsed = t - ws
            remaining = we - t

            # Get current BTC price
            btc_price = None
            for i in range(len(tick_ts) - 1, -1, -1):
                if tick_ts[i] <= t:
                    btc_price = tick_px[i]
                    break
            if btc_price is None:
                t += check_interval
                continue

            # Check 1: BTC moved enough from start
            move_pct = ((btc_price - start_price) / start_price) * 100.0
            if abs(move_pct) < threshold_pct:
                t += check_interval
                continue

            direction = "UP" if move_pct > 0 else "DOWN"

            # Check 2: Direction stability — BTC consistently on same side for stability_sec
            stable = True
            check_from = t - stability_sec
            for i in range(len(tick_ts)):
                if tick_ts[i] < check_from:
                    continue
                if tick_ts[i] > t:
                    break
                if direction == "UP" and tick_px[i] < start_price:
                    stable = False
                    break
                if direction == "DOWN" and tick_px[i] > start_price:
                    stable = False
                    break
            if not stable:
                t += check_interval
                continue

            # Check 3: Get CLOB odds
            odds_at = None
            for o in reversed(odds_rows):
                if float(o["ts"]) <= t:
                    odds_at = o
                    break
            if odds_at is None:
                t += check_interval
                continue

            up_ask = float(odds_at.get("up_best_ask") or 0.5)
            down_ask = float(odds_at.get("down_best_ask") or 0.5)
            entry_price = up_ask if direction == "UP" else down_ask

            # Confidence = how strong the move is (scaled 0.5 to 1.0)
            confidence = min(1.0, 0.5 + abs(move_pct) / (threshold_pct * 4))
            edge = abs(move_pct) / 100.0  # use move as "edge"

            # Compute simple EV
            if 0.01 < entry_price < 0.99:
                ev = (1.0 / entry_price - 1.0) * (0.5 + abs(move_pct) * 5) - 0.03  # rough model
            else:
                ev = 0

            # Insert signal_cache_log entry
            execute_write(conn, """
                INSERT INTO signal_cache_log
                (ts, window_start, direction, avg_confidence, max_edge,
                 unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                 seconds_elapsed, seconds_remaining,
                 btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                 buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                t, ws, direction,
                confidence, edge,
                1, json.dumps([{"judge": "momentum", "vote": direction,
                                "confidence": round(confidence, 3),
                                "reason": f"move={move_pct:+.3f}% stable={stability_sec}s"}]),
                up_ask, down_ask, btc_price, start_price,
                elapsed, remaining,
                move_pct, None, None, 1,
                None, 1, ev, f"momentum_{threshold_pct}pct_{stability_sec}s", None,
            ))
            inserted += 1
            signal_found = True
            break  # One signal per window

            t += check_interval

        if signal_found:
            windows_with_signal += 1

        if (wi + 1) % 100 == 0:
            conn.commit()
            logger.info("  [%d/%d] inserted=%d signal_windows=%d",
                        wi + 1, len(windows), inserted, windows_with_signal)

    conn.commit()
    logger.info("Done: %d windows, %d signals inserted, %.1f%% hit rate",
                len(windows), inserted, 100 * windows_with_signal / max(len(windows), 1))
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--last-hours", type=float, required=True)
    parser.add_argument("--threshold", type=float, default=0.05, help="Min BTC move %% from start")
    parser.add_argument("--stability", type=float, default=20.0, help="Direction stability seconds")
    parser.add_argument("--entry-start", type=float, default=60.0)
    parser.add_argument("--entry-end", type=float, default=240.0)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    rebuild(args.last_hours, args.threshold, args.stability,
            args.entry_start, args.entry_end, args.clear)
