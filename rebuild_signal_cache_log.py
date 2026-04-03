"""
Rebuild signal_cache_log from raw btc_ticks + poly_odds using CURRENT jury/guards/gate logic.
Produces signal_cache_log entries identical to what data_collector would have generated.

Usage:
    python rebuild_signal_cache_log.py --last-hours 120
    python rebuild_signal_cache_log.py --last-hours 72 --clear
"""
import argparse
import json
import logging
import os
import time as _time

os.environ.setdefault("MARIADB_PORT", "3400")

from config import config
from db_config import connect_db, execute_write, fetch_all_dicts, fetch_one
from judges import Jury, MarketContext
from entry_guards import evaluate_market_guards
from trade_gate import evaluate_entry_gate

logger = logging.getLogger("rebuild_scl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def rebuild(last_hours: float, clear: bool = False):
    conn = connect_db()
    end_ts = _time.time()
    start_ts = end_ts - last_hours * 3600

    if clear:
        execute_write(conn, "DELETE FROM signal_cache_log WHERE ts >= %s AND ts <= %s", (start_ts, end_ts))
        conn.commit()
        logger.info("Cleared signal_cache_log for %.0fh", last_hours)

    # Get windows
    windows = fetch_all_dicts(conn, """
        SELECT window_start, window_start + 300 as window_end, actual_outcome
        FROM market_windows
        WHERE actual_outcome IN ('UP','DOWN')
          AND window_start >= %s AND window_start <= %s
        ORDER BY window_start
    """, (int(start_ts), int(end_ts)))
    logger.info("Processing %d windows (%.0fh)", len(windows), last_hours)

    jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))
    entry_start = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
    entry_end = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
    check_interval = 1.0  # check every 1s
    inserted = 0
    windows_with_gate = 0

    for wi, w in enumerate(windows):
        ws = int(w["window_start"])
        we = int(w["window_end"])

        # Load ticks for this window + lookback
        ticks = fetch_all_dicts(conn, """
            SELECT ts, price FROM btc_ticks
            WHERE ts >= %s AND ts <= %s ORDER BY ts
        """, (ws - 600, we))
        if len(ticks) < 20:
            continue

        tick_ts = [float(t["ts"]) for t in ticks]
        tick_px = [float(t["price"]) for t in ticks]

        # Load odds for this window
        odds_rows = fetch_all_dicts(conn, """
            SELECT ts, up_best_ask, down_best_ask, up_best_bid, down_best_bid, up_mid, down_mid
            FROM poly_odds WHERE window_start = %s ORDER BY ts
        """, (ws,))
        if not odds_rows:
            continue

        # Get start price
        start_price = tick_px[0]
        for t in ticks:
            if float(t["ts"]) >= ws:
                start_price = float(t["price"])
                break

        # Check at each second from entry_start to entry_end
        t = ws + entry_start
        gate_found = False

        while t <= ws + entry_end and t < we:
            elapsed = t - ws
            remaining = we - t

            # Get BTC price at this time
            btc_price = None
            for i in range(len(tick_ts) - 1, -1, -1):
                if tick_ts[i] <= t:
                    btc_price = tick_px[i]
                    break
            if btc_price is None:
                t += check_interval
                continue

            # Get odds at this time
            odds_at = None
            for o in reversed(odds_rows):
                if float(o["ts"]) <= t:
                    odds_at = o
                    break
            if odds_at is None:
                t += check_interval
                continue

            up_ask = float(odds_at.get("up_best_ask") or odds_at.get("up_mid") or 0.5)
            dn_ask = float(odds_at.get("down_best_ask") or odds_at.get("down_mid") or 0.5)

            # Recent prices for context
            recent_px = [tick_px[i] for i in range(len(tick_ts)) if tick_ts[i] >= t - 600 and tick_ts[i] <= t]
            recent_ts_arr = [tick_ts[i] for i in range(len(tick_ts)) if tick_ts[i] >= t - 600 and tick_ts[i] <= t]
            if len(recent_px) < 10:
                t += check_interval
                continue

            # Build MarketContext
            ctx = MarketContext(
                current_binance_price=btc_price,
                market_start_price=start_price,
                recent_prices=recent_px[-600:],
                recent_timestamps=recent_ts_arr[-600:],
                poly_up_price=up_ask,
                poly_down_price=dn_ask,
                seconds_elapsed=elapsed,
                seconds_remaining=remaining,
                poly_up_ask=up_ask,
                poly_down_ask=dn_ask,
            )

            # Jury
            decision = jury.deliberate(ctx)
            if decision.direction not in ("UP", "DOWN"):
                t += check_interval
                continue

            btc_move_pct = ((btc_price - start_price) / start_price) * 100.0 if start_price > 0 else 0

            # Guards
            guard = evaluate_market_guards(
                direction=decision.direction,
                btc_price=btc_price,
                start_price=start_price,
                up_ask=up_ask,
                down_ask=dn_ask,
                elapsed=elapsed,
                prices=recent_px,
                timestamps=recent_ts_arr,
                now_ts=t,
            )
            guards_passed = 1 if guard.passed else 0

            # Gate
            gate_allow = 0
            gate_ev = None
            gate_reason = None
            if guards_passed:
                entry_price = up_ask if decision.direction == "UP" else dn_ask
                if 0.01 < entry_price < 0.99:
                    support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
                    support_ratio = support_votes / len(decision.verdicts) if decision.verdicts else 0

                    gate = evaluate_entry_gate(
                        direction=decision.direction,
                        entry_price=entry_price,
                        current_price=btc_price,
                        start_price=start_price,
                        seconds_elapsed=elapsed,
                        jury_confidence=float(decision.avg_confidence),
                        support_ratio=float(support_ratio),
                        seconds_remaining=remaining,
                        recent_prices=recent_px[-600:],
                        recent_timestamps=recent_ts_arr[-600:],
                        poly_up_ask=up_ask,
                        poly_down_ask=dn_ask,
                    )
                    gate_allow = 1 if gate.allow else 0
                    gate_ev = float(gate.expected_roi) if gate.expected_roi else None
                    gate_reason = str(gate.reason or "")[:200]

            # Build judges_json
            judges_json = json.dumps([
                {"judge": v.judge_name, "vote": v.vote.value,
                 "confidence": v.confidence, "reason": v.reason}
                for v in decision.verdicts
            ])

            # Compute score signals (prev_outcome, odds_velocity, btc_accel_ok)
            _prev_outcome = None
            _odds_velocity = None
            _btc_accel_ok = None
            # 1) prev_outcome: previous window's result
            try:
                _pw = ws - 300
                _pr = fetch_one(conn, "SELECT actual_outcome FROM market_windows WHERE window_start = %s", (_pw,))
                if _pr and _pr[0]:
                    _prev_outcome = str(_pr[0])
            except Exception:
                pass
            # 2) odds_velocity: current ask vs 30s ago ask
            try:
                _dir_ask = up_ask if decision.direction == "UP" else dn_ask
                _eo = fetch_one(conn,
                    "SELECT up_best_ask, down_best_ask FROM poly_odds WHERE window_start = %s AND ts >= %s AND ts <= %s ORDER BY ts ASC LIMIT 1",
                    (ws, t - 35, t - 25))
                if _eo:
                    _old_ask = float(_eo[0] or 0.5) if decision.direction == "UP" else float(_eo[1] or 0.5)
                    _odds_velocity = round(float(_dir_ask) - _old_ask, 6)
            except Exception:
                pass
            # 3) btc_accel_ok: BTC acceleration matches direction
            try:
                if len(tick_px) >= 15:
                    # Find prices at current, 1/3 ago, 2/3 ago
                    _idx = len(tick_px) - 1
                    for i in range(len(tick_ts) - 1, -1, -1):
                        if tick_ts[i] <= t:
                            _idx = i
                            break
                    _n = min(_idx + 1, 60)
                    if _n >= 15:
                        _p1 = tick_px[_idx]
                        _p2 = tick_px[_idx - max(_n // 3, 5)]
                        _p3 = tick_px[_idx - max(2 * _n // 3, 10)]
                        if _p3 > 0:
                            _v1 = (_p1 - _p2) / _p3 * 100
                            _v2 = (_p2 - _p3) / _p3 * 100
                            _a = _v1 - _v2
                            _btc_accel_ok = 1 if ((decision.direction == "UP" and _a > 0) or (decision.direction == "DOWN" and _a < 0)) else 0
            except Exception:
                pass

            # Insert to signal_cache_log
            execute_write(conn, """
                INSERT INTO signal_cache_log
                (ts, window_start, direction, avg_confidence, max_edge,
                 unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                 seconds_elapsed, seconds_remaining,
                 btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                 buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                 prev_outcome, odds_velocity, btc_accel_ok)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                t, ws, decision.direction,
                float(decision.avg_confidence), float(decision.max_edge),
                1 if decision.unanimous else 0, judges_json,
                up_ask, dn_ask, btc_price, start_price,
                elapsed, remaining,
                btc_move_pct, None, None, guards_passed,
                None, gate_allow, gate_ev, gate_reason, None,
                _prev_outcome, _odds_velocity, _btc_accel_ok,
            ))
            inserted += 1

            if gate_allow:
                gate_found = True
                break  # First gate_allow=1 per window — matches paper_replay_orig

            t += check_interval

        if gate_found:
            windows_with_gate += 1

        if (wi + 1) % 100 == 0:
            conn.commit()
            logger.info("  [%d/%d] inserted=%d gate_windows=%d", wi + 1, len(windows), inserted, windows_with_gate)

    conn.commit()
    logger.info("Done: %d windows, %d entries inserted, %d with gate_allow=1", len(windows), inserted, windows_with_gate)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--last-hours", type=float, required=True)
    parser.add_argument("--clear", action="store_true", help="Clear existing entries in range first")
    args = parser.parse_args()
    rebuild(args.last_hours, args.clear)
