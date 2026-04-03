"""
Paper trade replay: run EXACT paper_trade_sim logic on historical DB data.

Guarantees backtest = paper by using the SAME code path.
Reads signal_cache_log + btc_ticks + poly_odds from DB.

Usage:
    python paper_replay.py --last-hours 72
    python paper_replay.py --last-hours 24 --equity 100
"""
import argparse
import logging
import os
import sys
import time as _time_mod

# Ensure env is loaded
from config import config
from db_config import (
    connect_db,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
)
from trade_gate import apply_fee_to_pnl
from exit_policy import ExitPolicyConfig, ExitPolicyInput, evaluate_exit_policy
from judges import Jury, MarketContext

# Import paper_trade_sim functions we'll reuse directly
from paper_trade_sim import (
    _paper_exit_policy_config,
    _mark_to_market,
    _safe_prob,
    _recent_price_series,
    _price_at_or_near,
    _compute_bet_size,
)

logger = logging.getLogger("paper_replay")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class ReplayTrade:
    def __init__(self, window_start, direction, entry_price, stake, shares,
                 opened_at, confidence):
        self.window_start = window_start
        self.window_end = window_start + 300
        self.direction = direction
        self.entry_price = entry_price
        self.stake = stake
        self.shares = shares
        self.opened_at = opened_at
        self.confidence = confidence
        self.pnl = 0.0
        self.won = False
        self.close_reason = ""
        self.closed_at = 0.0


class PaperReplay:
    def __init__(self, conn, equity: float = 1000.0):
        self.conn = conn
        self.initial_equity = equity
        self.equity = equity
        self.trades: list[ReplayTrade] = []
        self.peak_roi: dict[int, float] = {}
        self.opposite_hits: dict[int, int] = {}
        self.smart_exit_last: dict[int, float] = {}
        self.exit_cfg = _paper_exit_policy_config()
        self.smart_exit_enabled = os.getenv("SMART_EXIT_ENABLED", "true").lower() == "true"

        # Paper entry config
        self.entry_start_sec = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
        self.entry_end_sec = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
        self.down_entry_end_sec = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "200"))
        self.max_entry_price = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.58"))
        self.down_min_entry_price = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.35"))
        self.min_seconds_remaining = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
        self.max_spread = float(os.getenv("PAPER_MAX_ODDS_SPREAD", "0.12"))
        self.drift_max = float(os.getenv("MAX_ENTRY_PRICE_DRIFT_ABS", "0.080"))
        self.taker_fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.03"))
        self.min_edge_filter = float(os.getenv("REPLAY_MIN_EDGE", "0"))
        self.min_conf_filter = float(os.getenv("REPLAY_MIN_CONF", "0"))
        self.min_btc_move = float(os.getenv("REPLAY_MIN_BTC_MOVE", "0"))
        self.max_btc_move = float(os.getenv("REPLAY_MAX_BTC_MOVE", "0"))
        self.require_momentum_agree = os.getenv("REPLAY_REQUIRE_MOMENTUM_AGREE", "0") == "1"
        self.min_score = int(os.getenv("REPLAY_MIN_SCORE", "0"))

    def _get_scl_entries(self, ws: int) -> list[dict]:
        """Get all signal_cache_log entries with gate_allow=1 for a window."""
        return fetch_all_dicts(self.conn, """
            SELECT ts, direction, avg_confidence, max_edge, up_ask, down_ask,
                   btc_price, start_price, seconds_elapsed, seconds_remaining,
                   btc_move_pct,
                   gate_allow, gate_ev, gate_reason,
                   prev_outcome, odds_velocity, btc_accel_ok
            FROM signal_cache_log
            WHERE window_start = %s AND gate_allow = 1
            ORDER BY ts ASC
        """, (ws,))

    def _get_odds_at(self, ws: int, ts: float) -> dict | None:
        """Get latest poly_odds at or before timestamp."""
        return fetch_one_dict(self.conn, """
            SELECT up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
            FROM poly_odds
            WHERE window_start = %s AND ts <= %s
            ORDER BY ts DESC LIMIT 1
        """, (ws, ts))

    def _simulate_entry(self, ws: int, scl_entries: list[dict]) -> ReplayTrade | None:
        """Try to enter: scan ALL scl entries, find first that passes all filters."""
        if not scl_entries:
            return None

        # Paper reads signal_cache repeatedly. gate_allow=1 at elapsed=2 gets read
        # later when paper's own elapsed >= 80. So we need to find any gate_allow=1
        # entry, then check if odds at elapsed >= entry_start_sec still work.
        #
        # Strategy: for each gate_allow=1 entry, if its elapsed < entry_start_sec,
        # look ahead in poly_odds at elapsed=entry_start_sec to get actual entry price.

        for entry in scl_entries:
            direction = str(entry["direction"])
            scl_elapsed = float(entry.get("seconds_elapsed") or 0)
            gate_ev = float(entry.get("gate_ev") or 0)
            confidence = float(entry.get("avg_confidence") or 0.5)
            max_edge = float(entry.get("max_edge") or 0.1)

            # Edge/confidence quality filters
            if self.min_edge_filter > 0 and max_edge < self.min_edge_filter:
                continue
            if self.min_conf_filter > 0 and confidence < self.min_conf_filter:
                continue
            # BTC move filter: only enter when BTC moved enough in the right direction
            btc_move = float(entry.get("btc_move_pct") or 0)
            if self.min_btc_move > 0:
                if abs(btc_move) < self.min_btc_move:
                    continue
                # Direction must match the move
                if direction == "UP" and btc_move < 0:
                    continue
                if direction == "DOWN" and btc_move > 0:
                    continue
            # Max BTC move filter: skip overextended entries (mean reversion risk)
            if self.max_btc_move > 0 and abs(btc_move) > self.max_btc_move:
                continue

            # Momentum agreement: 30s BTC trend must match bet direction
            if self.require_momentum_agree:
                _check_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
                _prev_tick = fetch_one_dict(self.conn, """
                    SELECT price FROM btc_ticks WHERE ts >= %s AND ts <= %s ORDER BY ts DESC LIMIT 1
                """, (_check_ts - 35, _check_ts - 25))
                if _prev_tick:
                    _btc_now = float(entry.get("btc_price") or 0)
                    _btc_30s = float(_prev_tick.get("price") or 0)
                    if _btc_now > 0 and _btc_30s > 0:
                        _rising = _btc_now > _btc_30s
                        _conflict = (direction == "UP" and not _rising) or (direction == "DOWN" and _rising)
                        if _conflict:
                            continue

            # Score filter: count how many signals are positive
            if self.min_score > 0:
                _prev = str(entry.get("prev_outcome") or "")
                _ov = float(entry.get("odds_velocity") or 0)
                _accel = bool(int(entry.get("btc_accel_ok") or 0)) if entry.get("btc_accel_ok") is not None else False
                _score = 0
                if abs(btc_move) >= 0.02: _score += 1          # 1. BTC moved
                if _prev == direction: _score += 1               # 2. prev won same dir
                # entry_price not yet final here, use ask from scl
                _ep = float(entry.get("up_ask") or 0.5) if direction == "UP" else float(entry.get("down_ask") or 0.5)
                if _ep <= 0.45: _score += 1                      # 3. cheap entry
                if gate_ev >= 0.20: _score += 1                  # 4. high EV
                if confidence >= 0.7: _score += 1                # 5. high conf
                if _ov >= 0.02: _score += 1                      # 6. odds velocity
                if _accel: _score += 1                           # 7. BTC accel
                if _score < self.min_score:
                    continue

            # If signal came early, paper would read it when elapsed >= entry_start_sec
            if scl_elapsed < self.entry_start_sec:
                # Look up odds at entry_start_sec
                check_ts = ws + self.entry_start_sec
                odds_at = fetch_one_dict(self.conn, """
                    SELECT up_best_ask, down_best_ask FROM poly_odds
                    WHERE window_start = %s AND ts >= %s AND ts <= %s
                    ORDER BY ts ASC LIMIT 1
                """, (ws, check_ts - 2, check_ts + 2))
                if not odds_at:
                    continue
                up_ask = float(odds_at.get("up_best_ask") or 0.5)
                down_ask = float(odds_at.get("down_best_ask") or 0.5)
                elapsed = self.entry_start_sec
                remaining = 300 - elapsed
            else:
                up_ask = float(entry.get("up_ask") or 0.5)
                down_ask = float(entry.get("down_ask") or 0.5)
                elapsed = scl_elapsed
                remaining = float(entry.get("seconds_remaining") or (300 - elapsed))

            # Timing filters
            if elapsed > self.entry_end_sec:
                continue
            if remaining < self.min_seconds_remaining:
                continue
            if direction == "DOWN" and elapsed > self.down_entry_end_sec:
                continue

            entry_price = up_ask if direction == "UP" else down_ask

            if entry_price <= 0.01 or entry_price >= 0.99:
                continue
            if entry_price > self.max_entry_price:
                continue
            if direction == "DOWN" and entry_price < self.down_min_entry_price:
                continue

            # Spread filter
            spread = abs(up_ask - down_ask)
            if spread > self.max_spread:
                continue

            # Score filter — skip if pre-computed signals are NULL (old rebuild data)
            # Paper doesn't have this score filter on entry; it's only for sizing.
            # So don't block entry based on score — just use it for mega sizing later.

            # Drift simulation: check ask after entry (FAK execution window)
            # Rust FAK takes ~300ms. Check 0.2-0.5s after signal for realistic fill.
            # If ask rose past max_entry_price -> FAK limit exceeded -> no fill -> skip
            # If ask rose but still valid -> fill at worse (later) price
            entry_ts = float(entry["ts"]) if scl_elapsed >= self.entry_start_sec else (ws + self.entry_start_sec)
            _drift_start = float(os.getenv("REPLAY_DRIFT_START_SEC", "0.2"))
            _drift_end = float(os.getenv("REPLAY_DRIFT_END_SEC", "0.5"))
            later_odds = fetch_all_dicts(self.conn, """
                SELECT up_best_ask, down_best_ask FROM poly_odds
                WHERE window_start = %s AND ts >= %s AND ts <= %s
                ORDER BY ts ASC LIMIT 1
            """, (ws, entry_ts + _drift_start, entry_ts + _drift_end))
            if later_odds:
                later_price = float(later_odds[0].get("up_best_ask") or entry_price) if direction == "UP" \
                    else float(later_odds[0].get("down_best_ask") or entry_price)
                if 0.01 < later_price < 0.99:
                    # Ask rose past max -> FAK limit exceeded, order won't fill
                    if later_price > self.max_entry_price:
                        continue
                    # Ask rose past drift tolerance -> skip
                    if later_price - entry_price > self.drift_max:
                        continue
                    # Use actual fill price (later price = what live would get)
                    entry_price = later_price
            # Re-check price filters after drift adjustment
            if entry_price > self.max_entry_price:
                continue

            # Fixed sizing (matches paper's FIXED mode)
            _fixed_stake = float(os.getenv("PAPER_FIXED_STAKE", "0"))
            if _fixed_stake > 0:
                stake = _fixed_stake
            else:
                stake = round(self.initial_equity * 0.15, 2)

            # conf2x sizing: 2x when jury confidence >= 0.7
            _mega_mult = float(os.getenv("PAPER_MEGA_MULTIPLIER", "2.0"))
            _min_score = int(os.getenv("PAPER_MIN_ENTRY_SCORE", "0"))  # 0 = disabled
            if _min_score > 0:
                # Optional score filter (7 signals)
                _btc_move_abs = abs(float(entry.get("btc_move_pct") or 0))
                if _btc_move_abs == 0 and float(entry.get("start_price") or 0) > 0:
                    _btc_move_abs = abs((float(entry.get("btc_price") or 0) - float(entry["start_price"])) / float(entry["start_price"]) * 100)
                _prev_outcome = str(entry.get("prev_outcome") or "")
                if _prev_outcome not in ("UP", "DOWN"):
                    _prev_outcome = None
                _ov = float(entry.get("odds_velocity") or 0)
                _accel_ok = bool(int(entry.get("btc_accel_ok") or 0)) if entry.get("btc_accel_ok") is not None else False
                _score = 0
                if _btc_move_abs >= 0.02: _score += 1
                if _prev_outcome == direction: _score += 1
                if entry_price <= 0.45: _score += 1
                if gate_ev >= 0.20: _score += 1
                if confidence >= 0.7: _score += 1
                if _ov >= 0.02: _score += 1
                if _accel_ok: _score += 1
                if _score < _min_score:
                    continue

            # conf2x: 2x when confidence >= 0.7
            if confidence >= 0.7 and _mega_mult > 1.0:
                stake = round(stake * _mega_mult, 2)

            shares = stake / entry_price
            opened_at = entry_ts

            return ReplayTrade(
                window_start=ws,
                direction=direction,
                entry_price=entry_price,
                stake=stake,
                shares=shares,
                opened_at=opened_at,
                confidence=confidence,
            )

        # No valid entry found in any scl entry
        return None

    def _simulate_exit(self, trade: ReplayTrade, outcome: str | None) -> None:
        """Run the SAME exit logic as paper_trade_sim.resolve_open_trades()."""
        ws = trade.window_start
        we = trade.window_end
        direction = trade.direction
        stake = trade.stake
        shares = trade.shares
        entry_price = trade.entry_price
        opened_at = trade.opened_at

        # Check every second from entry to window end
        check_interval = 1.0
        t = opened_at + max(self.exit_cfg.min_elapsed_sec, 1.0)

        while t <= we:
            hold_sec = t - opened_at
            seconds_elapsed = t - ws
            remaining_sec = max(0.0, we - t)

            # 1) Settlement check
            if t >= we and outcome in ("UP", "DOWN"):
                won = (outcome == direction)
                if won:
                    raw_pnl = shares - stake
                    trade.pnl = apply_fee_to_pnl(raw_pnl, stake)
                else:
                    trade.pnl = -stake
                trade.won = won
                trade.close_reason = "expiry_settlement"
                trade.closed_at = t
                return

            # 2) Get odds for mark-to-market
            odds_row = self._get_odds_at(ws, t)
            if not odds_row:
                t += check_interval
                continue

            _exit_px, _value, mtm_pnl, mtm_roi_pct = _mark_to_market(
                direction=direction, stake=stake, shares=shares, odds_row=odds_row,
            )
            up_ask = _safe_prob(odds_row.get("up_best_ask")) or _safe_prob(odds_row.get("up_mid"))
            down_ask = _safe_prob(odds_row.get("down_best_ask")) or _safe_prob(odds_row.get("down_mid"))
            opposite_ask = up_ask if direction == "DOWN" else down_ask

            # BTC price
            btc_now = _price_at_or_near(self.conn, t, prefer_before=True)
            btc_entry = _price_at_or_near(self.conn, opened_at, prefer_before=True)
            current_btc = float(btc_now) if btc_now else 0.0
            start_btc = float(fetch_one_dict(self.conn,
                "SELECT btc_start_price FROM market_windows WHERE window_start = %s", (ws,)
            ).get("btc_start_price") or current_btc) if current_btc > 0 else 0.0

            btc_move_entry = None
            btc_adverse_ok = True
            if btc_entry and btc_now and float(btc_entry) > 0:
                btc_move_entry = ((float(btc_now) - float(btc_entry)) / float(btc_entry)) * 100.0
                if self.exit_cfg.stop_loss_require_btc_adverse:
                    thr = abs(float(self.exit_cfg.stop_loss_btc_adverse_pct))
                    if direction == "UP":
                        btc_adverse_ok = float(btc_move_entry) <= -thr
                    else:
                        btc_adverse_ok = float(btc_move_entry) >= thr

            recent_ts, recent_prices = _recent_price_series(self.conn, t, lookback_sec=180.0)
            if recent_prices:
                current_btc = float(recent_prices[-1])

            trade_key = ws
            peak = max(float(self.peak_roi.get(trade_key, -999.0)), float(mtm_roi_pct))
            self.peak_roi[trade_key] = peak

            # 3) Exit policy
            exit_decision = evaluate_exit_policy(
                ExitPolicyInput(
                    direction=direction,
                    hold_sec=float(hold_sec),
                    seconds_elapsed=float(seconds_elapsed),
                    seconds_remaining=float(remaining_sec),
                    signal_confidence=float(trade.confidence),
                    mtm_roi_pct=float(mtm_roi_pct),
                    current_price=float(current_btc),
                    start_price=float(start_btc),
                    peak_roi_pct=float(peak),
                    opposite_ask=float(opposite_ask) if opposite_ask is not None else None,
                    recent_prices=list(recent_prices),
                    recent_timestamps=list(recent_ts),
                    btc_adverse_ok=bool(btc_adverse_ok),
                    btc_move_from_entry_pct=float(btc_move_entry) if btc_move_entry is not None else None,
                    opposite_hits=int(self.opposite_hits.get(trade_key, 0)),
                ),
                self.exit_cfg,
            )
            if exit_decision.opposite_hits > 0:
                self.opposite_hits[trade_key] = int(exit_decision.opposite_hits)
            else:
                self.opposite_hits.pop(trade_key, None)
            early_reason = exit_decision.reason

            # 4) Smart exit
            if (
                early_reason is None
                and self.smart_exit_enabled
                and hold_sec >= float(config.trading.smart_exit_min_hold_sec)
                and float(mtm_roi_pct) >= float(config.trading.smart_exit_min_roi_pct)
            ):
                _se_interval = float(config.trading.smart_exit_interval_sec)
                _se_last = self.smart_exit_last.get(trade_key, 0.0)
                if (t - _se_last) >= _se_interval:
                    self.smart_exit_last[trade_key] = t
                    try:
                        _se_jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))
                        _se_prices, _se_ts = _recent_price_series(self.conn, t, lookback_sec=600.0)
                        if len(_se_prices) >= 20 and start_btc > 0:
                            _se_up = float(up_ask) if up_ask else 0.5
                            _se_dn = float(down_ask) if down_ask else 0.5
                            _se_ctx = MarketContext(
                                current_binance_price=float(current_btc),
                                market_start_price=float(start_btc),
                                recent_prices=list(_se_prices[-600:]),
                                recent_timestamps=list(_se_ts[-600:]),
                                poly_up_price=_se_up,
                                poly_down_price=_se_dn,
                                seconds_elapsed=float(seconds_elapsed),
                                seconds_remaining=float(remaining_sec),
                                poly_up_ask=_se_up,
                                poly_down_ask=_se_dn,
                            )
                            _se_decision = _se_jury.deliberate(_se_ctx)
                            if _se_decision.direction != direction and _se_decision.direction != "NO_TRADE":
                                early_reason = (
                                    f"smart_exit_jury_flip(flip={_se_decision.direction}"
                                    f", hold={hold_sec:.0f}s, roi={mtm_roi_pct:+.1f}%"
                                    f", opp_ask={float(opposite_ask or 0):.3f})"
                                )
                            elif _se_decision.direction == "NO_TRADE" and float(mtm_roi_pct) < -30.0:
                                early_reason = (
                                    f"smart_exit_no_trade(hold={hold_sec:.0f}s"
                                    f", roi={mtm_roi_pct:+.1f}%"
                                    f", opp_ask={float(opposite_ask or 0):.3f})"
                                )
                    except Exception:
                        pass

            if early_reason:
                _exit_px2, _, pnl2, roi2 = _mark_to_market(
                    direction=direction, stake=stake, shares=shares, odds_row=odds_row,
                )
                trade.pnl = pnl2 - (stake * self.taker_fee_rate)  # taker fee on exit
                trade.won = pnl2 > 0
                trade.close_reason = early_reason
                trade.closed_at = t
                self.peak_roi.pop(trade_key, None)
                self.opposite_hits.pop(trade_key, None)
                self.smart_exit_last.pop(trade_key, None)
                return

            t += check_interval

        # If we get here, hold to settlement
        if outcome in ("UP", "DOWN"):
            won = (outcome == direction)
            if won:
                raw_pnl = shares - stake
                trade.pnl = apply_fee_to_pnl(raw_pnl, stake) - (stake * self.taker_fee_rate)
            else:
                trade.pnl = -stake  # no additional fee on total loss
            trade.won = won
            trade.close_reason = "expiry_settlement"
            trade.closed_at = we

    def run(self, start_ts: float, end_ts: float) -> list[ReplayTrade]:
        """Replay paper trades over a time range."""
        # Get all resolved windows in range
        windows = fetch_all_dicts(self.conn, """
            SELECT window_start, actual_outcome
            FROM market_windows
            WHERE window_start >= %s AND window_start <= %s
              AND actual_outcome IN ('UP', 'DOWN')
            ORDER BY window_start ASC
        """, (int(start_ts), int(end_ts)))

        total = len(windows)
        logger.info("Replaying %d windows (%.1f hours)", total, (end_ts - start_ts) / 3600)

        for i, w in enumerate(windows):
            ws = int(w["window_start"])
            outcome = w["actual_outcome"]

            # Get signal_cache_log entries for this window
            scl_entries = self._get_scl_entries(ws)
            if not scl_entries:
                continue  # No gate_allow=1 for this window

            # Try entry
            trade = self._simulate_entry(ws, scl_entries)
            if trade is None:
                continue

            # Check equity
            if trade.stake > self.equity:
                trade.stake = max(5.0, self.equity)
                trade.shares = trade.stake / trade.entry_price

            # Simulate exit
            self._simulate_exit(trade, outcome)

            # Update equity
            self.equity += trade.pnl
            self.trades.append(trade)

            if (i + 1) % 50 == 0:
                logger.info("  [%d/%d] trades=%d PnL=$%+.2f equity=$%.0f",
                            i + 1, total, len(self.trades),
                            sum(t.pnl for t in self.trades), self.equity)

        return self.trades


def main():
    parser = argparse.ArgumentParser(description="Paper trade replay (exact parity)")
    parser.add_argument("--last-hours", type=float, required=True)
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--entry-start", type=float, default=None, help="Override PAPER_ENTRY_START_SEC")
    parser.add_argument("--max-ask", type=float, default=None, help="Override PAPER_MAX_ENTRY_PRICE")
    parser.add_argument("--min-roi", type=float, default=None, help="Override MIN_EXPECTED_ROI (gate level)")
    parser.add_argument("--boundary", type=float, default=None, help="Override PAPER_MIN_BOUNDARY_DIST_PCT")
    parser.add_argument("--max-spread", type=float, default=None, help="Override PAPER_MAX_ODDS_SPREAD")
    parser.add_argument("--mega-mult", type=float, default=None, help="Override PAPER_MEGA_MULTIPLIER")
    parser.add_argument("--stake", type=float, default=None, help="Override PAPER_FIXED_STAKE")
    parser.add_argument("--min-edge", type=float, default=None, help="Min max_edge to enter (filters noisy signals)")
    parser.add_argument("--min-conf", type=float, default=None, help="Min avg_confidence to enter")
    parser.add_argument("--min-btc-move", type=float, default=None, help="Min abs(btc_move_pct) + direction match")
    parser.add_argument("--max-btc-move", type=float, default=None, help="Max abs(btc_move_pct) — skip overextended")
    parser.add_argument("--require-momentum-agree", action="store_true", help="Skip when 30s BTC trend conflicts with bet direction")
    parser.add_argument("--min-score", type=int, default=None, help="Min signal score to enter")
    args = parser.parse_args()

    # CLI overrides (bypass config.py load_dotenv override=True)
    if args.entry_start is not None:
        os.environ["PAPER_ENTRY_START_SEC"] = str(args.entry_start)
    if args.max_ask is not None:
        os.environ["PAPER_MAX_ENTRY_PRICE"] = str(args.max_ask)
    if args.min_roi is not None:
        os.environ["MIN_EXPECTED_ROI"] = str(args.min_roi)
    if args.boundary is not None:
        os.environ["PAPER_MIN_BOUNDARY_DIST_PCT"] = str(args.boundary)
    if args.max_spread is not None:
        os.environ["PAPER_MAX_ODDS_SPREAD"] = str(args.max_spread)
    if args.mega_mult is not None:
        os.environ["PAPER_MEGA_MULTIPLIER"] = str(args.mega_mult)
    if args.stake is not None:
        os.environ["PAPER_FIXED_STAKE"] = str(args.stake)
    if args.min_edge is not None:
        os.environ["REPLAY_MIN_EDGE"] = str(args.min_edge)
    if args.min_conf is not None:
        os.environ["REPLAY_MIN_CONF"] = str(args.min_conf)
    if args.min_btc_move is not None:
        os.environ["REPLAY_MIN_BTC_MOVE"] = str(args.min_btc_move)
    if args.max_btc_move is not None:
        os.environ["REPLAY_MAX_BTC_MOVE"] = str(args.max_btc_move)
    if args.require_momentum_agree:
        os.environ["REPLAY_REQUIRE_MOMENTUM_AGREE"] = "1"
    if args.min_score is not None:
        os.environ["REPLAY_MIN_SCORE"] = str(args.min_score)

    conn = connect_db()
    end_ts = _time_mod.time()
    start_ts = end_ts - args.last_hours * 3600

    replay = PaperReplay(conn, equity=args.equity)
    trades = replay.run(start_ts, end_ts)

    if not trades:
        print("No trades in replay period.")
        conn.close()
        return

    wins = sum(1 for t in trades if t.won)
    losses = len(trades) - wins
    total_pnl = sum(t.pnl for t in trades)
    avg_win = sum(t.pnl for t in trades if t.won) / max(wins, 1)
    avg_loss = sum(t.pnl for t in trades if not t.won) / max(losses, 1)
    gross_win = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    print(f"\n{'='*60}")
    print(f" PAPER REPLAY - {args.last_hours}h ({len(trades)} trades)")
    print(f"{'='*60}")
    print(f"  Trades:        {len(trades)}")
    print(f"  Win rate:      {wins}/{len(trades)} ({wins/len(trades)*100:.1f}%)")
    print(f"  Total PnL:     ${total_pnl:+.2f}")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Avg win:       ${avg_win:+.2f}")
    print(f"  Avg loss:      ${avg_loss:+.2f}")
    print(f"  Final equity:  ${replay.equity:.2f}")
    print(f"  Return:        {(replay.equity - args.equity) / args.equity * 100:+.1f}%")

    # Direction breakdown
    for d in ["UP", "DOWN"]:
        dt = [t for t in trades if t.direction == d]
        if dt:
            dw = sum(1 for t in dt if t.won)
            dp = sum(t.pnl for t in dt)
            print(f"  {d}:   {len(dt)}t {dw}W/{len(dt)-dw}L PnL=${dp:+.2f}")

    # Exit reason breakdown
    print(f"\n  Exit reasons:")
    reasons = {}
    for t in trades:
        r = t.close_reason.split("(")[0].strip()
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t.pnl
        if t.won:
            reasons[r]["wins"] += 1
    for r, d in sorted(reasons.items(), key=lambda x: x[1]["pnl"]):
        print(f"    {r:35s}: {d['count']:2d}t {d['wins']}W PnL=${d['pnl']:+.2f}")

    # Individual trades
    print(f"\n  Individual trades:")
    for t in trades:
        from datetime import datetime
        dt = datetime.fromtimestamp(t.opened_at)
        m = "W" if t.won else "L"
        print(f"    {dt.strftime('%m-%d %H:%M')} {t.direction:4s} {m} ${t.pnl:+8.2f} @{t.entry_price:.3f} stk=${t.stake:.0f} | {t.close_reason[:50]}")

    print(f"{'='*60}")
    conn.close()


if __name__ == "__main__":
    main()
