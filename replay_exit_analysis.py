"""
Replay existing paper trade entries against the current exit policy.

This does not open new trades. It reuses recorded paper entry timestamps and
simulates when the current exit policy would have closed them.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
from dataclasses import replace
import itertools

import config  # noqa: F401 - load env files
from db_config import connect_db, db_label, fetch_all_dicts, fetch_one_dict
from exit_policy import ExitPolicyInput, evaluate_exit_policy
from paper_trade_sim import _paper_exit_policy_config
from trade_gate import apply_fee_to_pnl


def _safe_prob(value):
    try:
        if value is None:
            return None
        x = float(value)
        if 0.0 < x < 1.0:
            return x
    except Exception:
        pass
    return None


def _mtm(direction: str, stake: float, shares: float, odds_row: dict) -> tuple[float, float, float]:
    if str(direction).upper() == "UP":
        px = _safe_prob(odds_row.get("up_best_bid")) or _safe_prob(odds_row.get("up_mid")) or 0.5
    else:
        px = _safe_prob(odds_row.get("down_best_bid")) or _safe_prob(odds_row.get("down_mid")) or 0.5
    current_value = float(shares * px)
    pnl = float(apply_fee_to_pnl(current_value - stake, stake))
    roi = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0
    return float(px), pnl, roi


def _parse_args():
    p = argparse.ArgumentParser(description="Replay paper entries with current exit policy")
    p.add_argument("--limit", type=int, default=24, help="Number of recent closed trades to replay")
    p.add_argument("--top", type=int, default=8, help="Rows to print in top/bottom sections")
    p.add_argument("--trailing-drop", type=float, default=None)
    p.add_argument("--trailing-peak", type=float, default=None)
    p.add_argument("--trailing-hold", type=float, default=None)
    p.add_argument("--trailing-late-remain", type=float, default=None)
    p.add_argument("--trailing-force-peak", type=float, default=None)
    p.add_argument("--profit-take", type=float, default=None)
    p.add_argument("--profit-take-hold", type=float, default=None)
    p.add_argument("--profit-late-remain", type=float, default=None)
    p.add_argument("--profit-force-roi", type=float, default=None)
    p.add_argument("--opposite-ask", type=float, default=None)
    p.add_argument("--opposite-loss-roi", type=float, default=None)
    p.add_argument("--opposite-late-remain", type=float, default=None)
    p.add_argument("--opposite-sigma-mult", type=float, default=None)
    p.add_argument("--opposite-min-move", type=float, default=None)
    p.add_argument("--stop-loss", type=float, default=None)
    p.add_argument("--break-even-peak", type=float, default=None)
    p.add_argument("--break-even-floor", type=float, default=None)
    p.add_argument("--break-even-late-remain", type=float, default=None)
    p.add_argument("--break-even-force-peak", type=float, default=None)
    p.add_argument("--sweep", action="store_true", help="Run a small parameter sweep")
    p.add_argument("--top-sweep", type=int, default=10, help="Top sweep rows to print")
    p.add_argument("--grid-trailing-drop", type=str, default="24,26,30")
    p.add_argument("--grid-trailing-late-remain", type=str, default="100,120,140")
    p.add_argument("--grid-profit-take", type=str, default="60,70,80")
    p.add_argument("--grid-profit-force", type=str, default="70,80,90")
    p.add_argument("--grid-opposite-late-remain", type=str, default="100,120,130")
    return p.parse_args()


def _parse_grid(raw: str) -> list[float]:
    vals: list[float] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        vals.append(float(part))
    return vals


def main():
    args = _parse_args()
    exit_cfg = _paper_exit_policy_config()
    override_map = {
        "trailing_stop_drop_pct": args.trailing_drop,
        "trailing_stop_min_peak_pct": args.trailing_peak,
        "trailing_stop_min_hold_sec": args.trailing_hold,
        "trailing_late_only_remaining_sec": args.trailing_late_remain,
        "trailing_force_peak_pct": args.trailing_force_peak,
        "profit_take_roi_pct": args.profit_take,
        "profit_take_min_hold_sec": args.profit_take_hold,
        "profit_take_late_only_remaining_sec": args.profit_late_remain,
        "profit_take_force_roi_pct": args.profit_force_roi,
        "opposite_ask": args.opposite_ask,
        "opposite_min_loss_roi_pct": args.opposite_loss_roi,
        "opposite_late_only_remaining_sec": args.opposite_late_remain,
        "opposite_severe_adverse_sigma_mult": args.opposite_sigma_mult,
        "opposite_severe_adverse_min_move_pct": args.opposite_min_move,
        "stop_loss_roi_pct": args.stop_loss,
        "break_even_peak_pct": args.break_even_peak,
        "break_even_floor_roi_pct": args.break_even_floor,
        "break_even_late_only_remaining_sec": args.break_even_late_remain,
        "break_even_force_peak_pct": args.break_even_force_peak,
    }
    override_map = {k: v for k, v in override_map.items() if v is not None}
    if override_map:
        exit_cfg = replace(exit_cfg, **override_map)

    conn = connect_db()
    trades = fetch_all_dicts(
        conn,
        """SELECT id, window_start, direction, stake, shares, entry_price, opened_at,
                  signal_confidence, pnl AS recorded_pnl, close_reason
           FROM paper_trades
           WHERE status='CLOSED'
           ORDER BY opened_at DESC
           LIMIT ?""",
        (int(args.limit),),
    )

    def run_replay(cfg):
        recorded_total = 0.0
        replay_total = 0.0
        expiry_total = 0.0
        reason_counts: Counter[str] = Counter()
        rows = []
        for tr in trades:
            ws = int(tr["window_start"])
            mw = fetch_one_dict(
                conn,
                """SELECT window_end, actual_outcome, btc_start_price
                   FROM market_windows
                   WHERE window_start = ?""",
                (ws,),
            )
            if not mw:
                continue
            outcome = str(mw.get("actual_outcome") or "").upper()
            if outcome not in ("UP", "DOWN"):
                continue
            window_end = float(mw["window_end"])
            start_btc = float(mw.get("btc_start_price") or 0.0)
            if start_btc <= 0.0:
                continue
            opened_at = float(tr["opened_at"])
            direction = str(tr["direction"]).upper()
            stake = float(tr["stake"])
            shares = float(tr["shares"])
            signal_conf = float(tr.get("signal_confidence") or 0.5)
            recorded_pnl = float(tr.get("recorded_pnl") or 0.0)
            odds_rows = fetch_all_dicts(
                conn,
                """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
                   FROM poly_odds
                   WHERE window_start = ? AND ts >= ? AND ts <= ?
                   ORDER BY ts ASC""",
                (ws, opened_at, window_end),
            )
            ticks = fetch_all_dicts(
                conn,
                """SELECT ts, price
                   FROM btc_ticks
                   WHERE ts >= ? AND ts <= ?
                   ORDER BY ts ASC""",
                (max(0.0, opened_at - 180.0), window_end),
            )
            tick_ts = [float(x["ts"]) for x in ticks]
            tick_px = [float(x["price"]) for x in ticks]
            if not odds_rows or not tick_ts:
                continue
            idx_entry = bisect.bisect_right(tick_ts, opened_at) - 1
            btc_entry = tick_px[idx_entry] if idx_entry >= 0 else tick_px[0]
            peak_roi = -999.0
            opposite_hits = 0
            replay_reason = "expiry_settlement"
            replay_pnl = 0.0
            for od in odds_rows:
                now_ts = float(od["ts"])
                _exit_px, mtm_pnl, mtm_roi = _mtm(direction, stake, shares, od)
                peak_roi = max(peak_roi, mtm_roi)
                opp_ask = (
                    (_safe_prob(od.get("up_best_ask")) or _safe_prob(od.get("up_mid")))
                    if direction == "DOWN"
                    else (_safe_prob(od.get("down_best_ask")) or _safe_prob(od.get("down_mid")))
                )
                idx_now = bisect.bisect_right(tick_ts, now_ts) - 1
                if idx_now < 0:
                    continue
                cur_btc = tick_px[idx_now]
                look_lo = max(0.0, now_ts - 180.0)
                j0 = bisect.bisect_left(tick_ts, look_lo)
                recent_ts = tick_ts[j0 : idx_now + 1]
                recent_px = tick_px[j0 : idx_now + 1]
                btc_move = ((cur_btc - btc_entry) / btc_entry) * 100.0 if btc_entry > 0.0 else None
                adverse_thr = abs(float(cfg.stop_loss_btc_adverse_pct))
                btc_adverse_ok = bool(
                    btc_move is not None
                    and (
                        btc_move <= -adverse_thr
                        if direction == "UP"
                        else btc_move >= adverse_thr
                    )
                )
                dec = evaluate_exit_policy(
                    ExitPolicyInput(
                        direction=direction,
                        hold_sec=now_ts - opened_at,
                        seconds_elapsed=max(1.0, now_ts - ws),
                        seconds_remaining=max(0.0, window_end - now_ts),
                        signal_confidence=signal_conf,
                        mtm_roi_pct=mtm_roi,
                        current_price=cur_btc,
                        start_price=start_btc,
                        peak_roi_pct=peak_roi,
                        opposite_ask=opp_ask,
                        recent_prices=list(recent_px),
                        recent_timestamps=list(recent_ts),
                        btc_adverse_ok=btc_adverse_ok,
                        btc_move_from_entry_pct=btc_move,
                        opposite_hits=opposite_hits,
                    ),
                    cfg,
                )
                opposite_hits = int(dec.opposite_hits)
                if dec.reason:
                    replay_reason = str(dec.reason).split("(")[0]
                    replay_pnl = float(mtm_pnl)
                    break
            else:
                won = outcome == direction
                settlement = shares if won else 0.0
                replay_pnl = float(apply_fee_to_pnl(settlement - stake, stake))
            won = outcome == direction
            settlement = shares if won else 0.0
            expiry_pnl = float(apply_fee_to_pnl(settlement - stake, stake))
            recorded_total += recorded_pnl
            replay_total += replay_pnl
            expiry_total += expiry_pnl
            reason_counts[replay_reason] += 1
            rows.append(
                {
                    "id": int(tr["id"]),
                    "ws": ws,
                    "dir": direction,
                    "recorded_reason": str(tr.get("close_reason") or "").split("(")[0],
                    "replay_reason": replay_reason,
                    "recorded": round(recorded_pnl, 2),
                    "replay": round(replay_pnl, 2),
                    "expiry": round(expiry_pnl, 2),
                    "improve": round(replay_pnl - recorded_pnl, 2),
                    "outcome": outcome,
                }
            )
        return recorded_total, replay_total, expiry_total, reason_counts, rows

    if args.sweep:
        combos = []
        for trailing_drop, trailing_late, profit_take, profit_force, opposite_late in itertools.product(
            _parse_grid(args.grid_trailing_drop),
            _parse_grid(args.grid_trailing_late_remain),
            _parse_grid(args.grid_profit_take),
            _parse_grid(args.grid_profit_force),
            _parse_grid(args.grid_opposite_late_remain),
        ):
            cfg = replace(
                exit_cfg,
                trailing_stop_drop_pct=float(trailing_drop),
                trailing_late_only_remaining_sec=float(trailing_late),
                profit_take_roi_pct=float(profit_take),
                profit_take_force_roi_pct=float(profit_force),
                opposite_late_only_remaining_sec=float(opposite_late),
            )
            recorded_total, replay_total, expiry_total, _reason_counts, _rows = run_replay(cfg)
            combos.append(
                {
                    "replay_total": round(replay_total, 2),
                    "delta_vs_recorded": round(replay_total - recorded_total, 2),
                    "delta_vs_expiry": round(replay_total - expiry_total, 2),
                    "trailing_drop": trailing_drop,
                    "trailing_late_remain": trailing_late,
                    "profit_take": profit_take,
                    "profit_force": profit_force,
                    "opposite_late_remain": opposite_late,
                }
            )
        print("db=", db_label())
        print("sweep_top=")
        for row in sorted(combos, key=lambda x: x["replay_total"], reverse=True)[: int(args.top_sweep)]:
            print(row)
        conn.close()
        return

    recorded_total, replay_total, expiry_total, reason_counts, rows = run_replay(exit_cfg)
    print("db=", db_label())
    print("exit_cfg=", exit_cfg)
    print("recorded_total=", round(recorded_total, 2))
    print("replay_total=", round(replay_total, 2))
    print("expiry_total=", round(expiry_total, 2))
    print("delta_replay_vs_recorded=", round(replay_total - recorded_total, 2))
    print("delta_replay_vs_expiry=", round(replay_total - expiry_total, 2))
    print("reason_counts=", dict(reason_counts))
    print("top_improvements:")
    for row in sorted(rows, key=lambda x: x["improve"], reverse=True)[: int(args.top)]:
        print(row)
    print("top_worse:")
    for row in sorted(rows, key=lambda x: x["improve"])[: int(args.top)]:
        print(row)
    conn.close()


if __name__ == "__main__":
    main()
