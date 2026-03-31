"""
Shared time-weighted exit policy for paper and live trading.

The policy is designed for 5-minute binary markets where the same absolute BTC
move has different informational value depending on time remaining.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Optional


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def _safe_pct_change(current: float, past: float) -> float:
    if past == 0.0:
        return 0.0
    return ((current - past) / past) * 100.0


def _estimate_sigma_per_sqrt_sec(prices: list[float], ts: list[float]) -> float | None:
    n = min(len(prices), len(ts))
    if n < 8:
        return None

    pairs: list[tuple[float, float]] = []
    total_dt = 0.0
    total_dlog = 0.0
    for i in range(1, n):
        p0 = float(prices[i - 1])
        p1 = float(prices[i])
        dt = float(ts[i] - ts[i - 1])
        if p0 <= 0.0 or p1 <= 0.0 or dt <= 1e-6:
            continue
        dlog = math.log(p1 / p0)
        pairs.append((dlog, dt))
        total_dt += dt
        total_dlog += dlog

    if len(pairs) < 6 or total_dt <= 1e-6:
        return None

    mu = total_dlog / total_dt
    var_acc = 0.0
    for dlog, dt in pairs:
        resid = dlog - (mu * dt)
        var_acc += (resid * resid) / max(dt, 1e-6)

    var = var_acc / float(len(pairs))
    if var <= 0.0:
        return None
    return math.sqrt(var)


@dataclass(frozen=True)
class ExitPolicyConfig:
    enabled: bool = True
    min_elapsed_sec: float = 25.0
    opposite_ask: float = 0.78
    opposite_min_loss_roi_pct: float = -20.0
    opposite_confirm_polls: int = 3
    stop_loss_roi_pct: float = -40.0
    stop_loss_min_hold_sec: float = 35.0
    stop_loss_high_conf_cutoff: float = 0.75
    stop_loss_high_conf_min_hold_sec: float = 20.0
    stop_loss_low_conf_cutoff: float = 0.60
    stop_loss_low_conf_relax_pct: float = 15.0
    stop_loss_require_btc_adverse: bool = True
    stop_loss_btc_adverse_pct: float = 0.090
    max_hold_sec: float = 220.0
    timestop_max_remain_sec: float = 20.0
    timestop_max_roi_pct: float = -8.0
    trailing_stop_drop_pct: float = 30.0
    trailing_stop_min_peak_pct: float = 15.0
    trailing_stop_min_hold_sec: float = 35.0
    break_even_peak_pct: float = 30.0
    break_even_floor_roi_pct: float = -10.0
    profit_take_roi_pct: float = 60.0
    profit_take_min_hold_sec: float = 35.0
    opposite_late_only_remaining_sec: float = 130.0
    opposite_severe_adverse_sigma_mult: float = 1.35
    opposite_severe_adverse_min_move_pct: float = 0.060
    trailing_late_only_remaining_sec: float = 140.0
    trailing_force_peak_pct: float = 95.0
    break_even_late_only_remaining_sec: float = 90.0
    break_even_force_peak_pct: float = 90.0
    profit_take_late_only_remaining_sec: float = 95.0
    profit_take_force_roi_pct: float = 70.0
    time_weight_enabled: bool = True
    early_opposite_ask_extra: float = 0.10
    early_opposite_loss_extra_pct: float = 18.0
    early_stop_loss_extra_pct: float = 12.0
    early_trailing_drop_extra_pct: float = 14.0
    early_trailing_peak_extra_pct: float = 18.0
    early_profit_take_extra_pct: float = 25.0
    strong_favor_sigma_mult: float = 0.90
    strong_favor_min_move_pct: float = 0.020
    favor_hold_min_remaining_sec: float = 60.0
    favor_hold_break_even_floor_roi_pct: float = -8.0
    # Near-certain win: exit immediately when opposite token is this cheap
    # (our side ~ 1 - opposite_ask, e.g. opposite <= 0.05 -> side >= 0.95).
    # No point risking a late reversal when we've essentially won.
    near_certain_win_opposite_ask: float = 0.05
    near_certain_win_min_hold_sec: float = 10.0


@dataclass(frozen=True)
class ExitPolicyInput:
    direction: str
    hold_sec: float
    seconds_elapsed: float
    seconds_remaining: float
    signal_confidence: float
    mtm_roi_pct: float
    current_price: float
    start_price: float
    peak_roi_pct: float
    opposite_ask: Optional[float]
    recent_prices: list[float]
    recent_timestamps: list[float]
    btc_adverse_ok: bool = True
    btc_move_from_entry_pct: Optional[float] = None
    opposite_hits: int = 0


@dataclass(frozen=True)
class ExitPolicyDecision:
    reason: Optional[str]
    opposite_hits: int
    dynamic_opposite_ask: float
    dynamic_opposite_min_loss_roi_pct: float
    dynamic_stop_loss_roi_pct: float
    dynamic_trailing_drop_pct: float
    dynamic_trailing_peak_pct: float
    dynamic_profit_take_roi_pct: float
    directional_move_pct: float
    boundary_sigma_pct: float
    strong_favor: bool
    severe_adverse: bool


def evaluate_exit_policy(inp: ExitPolicyInput, cfg: ExitPolicyConfig) -> ExitPolicyDecision:
    if not bool(cfg.enabled):
        return ExitPolicyDecision(
            reason=None,
            opposite_hits=0,
            dynamic_opposite_ask=float(cfg.opposite_ask),
            dynamic_opposite_min_loss_roi_pct=float(cfg.opposite_min_loss_roi_pct),
            dynamic_stop_loss_roi_pct=float(cfg.stop_loss_roi_pct),
            dynamic_trailing_drop_pct=float(cfg.trailing_stop_drop_pct),
            dynamic_trailing_peak_pct=float(cfg.trailing_stop_min_peak_pct),
            dynamic_profit_take_roi_pct=float(cfg.profit_take_roi_pct),
            directional_move_pct=0.0,
            boundary_sigma_pct=0.0,
            strong_favor=False,
            severe_adverse=False,
        )

    hold_sec = max(0.0, float(inp.hold_sec))
    elapsed = max(1.0, float(inp.seconds_elapsed))
    remaining = max(0.0, float(inp.seconds_remaining))
    remain_ratio = _clamp(remaining / 300.0, 0.0, 1.0)

    dynamic_opposite_ask = float(cfg.opposite_ask)
    dynamic_opposite_min_loss_roi_pct = float(cfg.opposite_min_loss_roi_pct)
    dynamic_stop_loss_roi_pct = float(cfg.stop_loss_roi_pct)
    dynamic_trailing_drop_pct = float(cfg.trailing_stop_drop_pct)
    dynamic_trailing_peak_pct = float(cfg.trailing_stop_min_peak_pct)
    dynamic_profit_take_roi_pct = float(cfg.profit_take_roi_pct)

    if bool(cfg.time_weight_enabled):
        dynamic_opposite_ask = _clamp(
            float(cfg.opposite_ask) + (remain_ratio * float(cfg.early_opposite_ask_extra)),
            float(cfg.opposite_ask),
            0.98,
        )
        dynamic_opposite_min_loss_roi_pct = float(cfg.opposite_min_loss_roi_pct) - (
            remain_ratio * float(cfg.early_opposite_loss_extra_pct)
        )
        dynamic_stop_loss_roi_pct = float(cfg.stop_loss_roi_pct) - (
            remain_ratio * float(cfg.early_stop_loss_extra_pct)
        )
        dynamic_trailing_drop_pct = float(cfg.trailing_stop_drop_pct) + (
            remain_ratio * float(cfg.early_trailing_drop_extra_pct)
        )
        dynamic_trailing_peak_pct = float(cfg.trailing_stop_min_peak_pct) + (
            remain_ratio * float(cfg.early_trailing_peak_extra_pct)
        )
        dynamic_profit_take_roi_pct = float(cfg.profit_take_roi_pct) + (
            remain_ratio * float(cfg.early_profit_take_extra_pct)
        )

    dynamic_stop_loss_min_hold = max(float(cfg.min_elapsed_sec), float(cfg.stop_loss_min_hold_sec))
    signal_conf = float(_clamp(inp.signal_confidence, 0.0, 1.0))
    if signal_conf >= float(cfg.stop_loss_high_conf_cutoff):
        dynamic_stop_loss_min_hold = min(
            dynamic_stop_loss_min_hold,
            max(float(cfg.min_elapsed_sec), float(cfg.stop_loss_high_conf_min_hold_sec)),
        )
    elif signal_conf <= float(cfg.stop_loss_low_conf_cutoff):
        dynamic_stop_loss_roi_pct -= abs(float(cfg.stop_loss_low_conf_relax_pct))

    direction = str(inp.direction).upper()
    move_from_start_pct = _safe_pct_change(float(inp.current_price), float(inp.start_price))
    directional_move_pct = float(move_from_start_pct if direction == "UP" else -move_from_start_pct)

    boundary_sigma_pct = 0.0
    sigma = _estimate_sigma_per_sqrt_sec(list(inp.recent_prices or []), list(inp.recent_timestamps or []))
    if sigma is not None and sigma > 0.0:
        boundary_sigma_pct = float(sigma * math.sqrt(max(1.0, remaining)) * 100.0)

    favor_band = max(
        float(cfg.strong_favor_min_move_pct),
        float(boundary_sigma_pct) * float(cfg.strong_favor_sigma_mult),
    )
    strong_favor = bool(
        directional_move_pct > 0.0
        and directional_move_pct >= favor_band
        and remaining >= float(cfg.favor_hold_min_remaining_sec)
    )
    adverse_band = max(
        float(cfg.opposite_severe_adverse_min_move_pct),
        float(boundary_sigma_pct) * float(cfg.opposite_severe_adverse_sigma_mult),
    )
    severe_adverse = bool(directional_move_pct < 0.0 and abs(directional_move_pct) >= adverse_band)
    trailing_late_window = bool(remaining <= float(cfg.trailing_late_only_remaining_sec))
    break_even_late_window = bool(remaining <= float(cfg.break_even_late_only_remaining_sec))
    profit_take_late_window = bool(remaining <= float(cfg.profit_take_late_only_remaining_sec))
    opposite_late_window = bool(remaining <= float(cfg.opposite_late_only_remaining_sec))

    opposite_hits = int(inp.opposite_hits or 0)
    reason: Optional[str] = None
    mtm_roi_pct = float(inp.mtm_roi_pct)

    if hold_sec < float(cfg.min_elapsed_sec):
        return ExitPolicyDecision(
            reason=None,
            opposite_hits=0,
            dynamic_opposite_ask=dynamic_opposite_ask,
            dynamic_opposite_min_loss_roi_pct=dynamic_opposite_min_loss_roi_pct,
            dynamic_stop_loss_roi_pct=dynamic_stop_loss_roi_pct,
            dynamic_trailing_drop_pct=dynamic_trailing_drop_pct,
            dynamic_trailing_peak_pct=dynamic_trailing_peak_pct,
            dynamic_profit_take_roi_pct=dynamic_profit_take_roi_pct,
            directional_move_pct=directional_move_pct,
            boundary_sigma_pct=boundary_sigma_pct,
            strong_favor=strong_favor,
            severe_adverse=severe_adverse,
        )

    # Dynamic profit-take: exit when our side bid reaches entry_price + offset
    _pt_enabled = os.getenv("LIVE_PROFIT_TAKE_ENABLED", "true").lower() == "true"
    _pt_offset = float(os.getenv("LIVE_PROFIT_TAKE_OFFSET", "0.10"))
    if (
        reason is None
        and _pt_enabled
        and inp.entry_price is not None
        and inp.side_ask is not None
        and hold_sec >= 10.0
    ):
        _side_bid = 1.0 - float(inp.opposite_ask) if inp.opposite_ask is not None else None
        _target = float(inp.entry_price) + _pt_offset
        if _side_bid is not None and _side_bid >= _target:
            reason = (
                f"profit_take(side_bid={_side_bid:.3f}"
                f" >= target={_target:.3f},"
                f" entry={float(inp.entry_price):.3f}+{_pt_offset:.2f},"
                f" roi={mtm_roi_pct:+.2f}%, hold={hold_sec:.1f}s)"
            )

    # Manipulation defense: if near expiry, coinflip, and opposite strongly disagrees
    # Someone may dump BTC in last seconds to flip the result
    _manip_enabled = os.getenv("LIVE_MANIP_DEFENSE_ENABLED", "true").lower() == "true"
    if (
        reason is None
        and _manip_enabled
        and inp.opposite_ask is not None
        and inp.seconds_remaining is not None
        and float(inp.seconds_remaining) <= 20.0
        and float(inp.opposite_ask) >= 0.70
    ):
        # Check if BTC is close to start (< 0.03% = manipulation zone)
        _btc_diff_pct = abs((float(inp.current_price) - float(inp.start_price)) / float(inp.start_price) * 100) if inp.start_price and inp.start_price > 0 else 999
        if _btc_diff_pct < 0.03:
            reason = (
                f"manip_defense(opp_ask={float(inp.opposite_ask):.3f} >= 0.70,"
                f" btc_diff={_btc_diff_pct:+.4f}% < 0.03%,"
                f" remain={float(inp.seconds_remaining):.0f}s)"
            )

    # CLOB conviction exit: CLOB strongly disagrees with our position.
    # Data: opp_ask >= 0.65 at 20s predicts outcome 93% accurately.
    # Graduated: more time remaining = higher opp_ask required.
    _clob_exit_enabled = os.getenv("CLOB_CONVICTION_EXIT_ENABLED", "true").lower() == "true"
    if (
        reason is None
        and _clob_exit_enabled
        and inp.opposite_ask is not None
        and inp.seconds_remaining is not None
    ):
        _remain = float(inp.seconds_remaining)
        _opp = float(inp.opposite_ask)
        _clob_trigger = False
        if _remain <= 30 and _opp >= 0.65:
            _clob_trigger = True
        elif _remain <= 90 and _opp >= 0.80:
            _clob_trigger = True
        elif _remain <= 150 and _opp >= 0.90:
            _clob_trigger = True
        if _clob_trigger:
            reason = (
                f"clob_conviction_exit(opp_ask={_opp:.3f},"
                f" remain={_remain:.0f}s)"
            )

    # Near-certain win: our side token ~ 1 - opposite_ask.
    # When opposite is nearly worthless, we've essentially won -- sell now
    # rather than risk a late reversal for a tiny extra payout.
    ncw_threshold = float(cfg.near_certain_win_opposite_ask)
    if (
        reason is None
        and ncw_threshold > 0
        and inp.opposite_ask is not None
        and float(inp.opposite_ask) <= ncw_threshold
        and hold_sec >= float(cfg.near_certain_win_min_hold_sec)
        and mtm_roi_pct > 0
    ):
        side_approx = 1.0 - float(inp.opposite_ask)
        reason = (
            f"near_certain_win(opposite_ask={float(inp.opposite_ask):.3f}"
            f" <= {ncw_threshold:.3f},"
            f" side~{side_approx:.3f},"
            f" roi={mtm_roi_pct:+.2f}%, hold={hold_sec:.1f}s)"
        )

    if (
        inp.opposite_ask is not None
        and float(inp.opposite_ask) >= dynamic_opposite_ask
        and mtm_roi_pct <= dynamic_opposite_min_loss_roi_pct
        and not strong_favor
        and (opposite_late_window or severe_adverse)
    ):
        opposite_hits += 1
        if opposite_hits >= max(1, int(cfg.opposite_confirm_polls)):
            reason = (
                f"opposite_prob_surge(opposite_ask={float(inp.opposite_ask):.3f}"
                f" >= {dynamic_opposite_ask:.3f},"
                f" roi={mtm_roi_pct:+.2f}% <= {dynamic_opposite_min_loss_roi_pct:+.2f}%,"
                f" hits={opposite_hits}, rem={remaining:.1f}s, favor={strong_favor}, adverse={severe_adverse})"
            )
    else:
        opposite_hits = 0

    # Hard adverse flush: BINARY MARKET -- only salvage when position is
    # nearly certainly lost.  Require opposite >= 0.93 (market 93%+ against
    # us) AND remaining <= 90s (late enough that reversal is unlikely).
    hard_adverse_ask = max(float(cfg.opposite_ask), 0.93)
    hard_adverse_loss_roi_pct = max(float(cfg.stop_loss_roi_pct), -70.0)
    hard_adverse_time_ok = remaining <= 90.0
    if (
        reason is None
        and inp.opposite_ask is not None
        and float(inp.opposite_ask) >= hard_adverse_ask
        and mtm_roi_pct <= hard_adverse_loss_roi_pct
        and hold_sec >= float(cfg.min_elapsed_sec)
        and not strong_favor
        and hard_adverse_time_ok
    ):
        reason = (
            f"hard_adverse_flush(opposite_ask={float(inp.opposite_ask):.3f}"
            f" >= {hard_adverse_ask:.3f},"
            f" roi={mtm_roi_pct:+.2f}% <= {hard_adverse_loss_roi_pct:+.2f}%"
            f", rem={remaining:.1f}s, adverse={severe_adverse})"
        )

    if (
        reason is None
        and hold_sec >= dynamic_stop_loss_min_hold
        and mtm_roi_pct <= dynamic_stop_loss_roi_pct
        and bool(inp.btc_adverse_ok)
    ):
        btc_move_note = (
            f", btc_entry_move={float(inp.btc_move_from_entry_pct):+.4f}%"
            if inp.btc_move_from_entry_pct is not None
            else ""
        )
        reason = (
            f"stop_loss(roi={mtm_roi_pct:+.2f}%"
            f" <= {dynamic_stop_loss_roi_pct:+.2f}%"
            f", hold={hold_sec:.1f}s >= {dynamic_stop_loss_min_hold:.1f}s"
            f", conf={signal_conf:.3f}, rem={remaining:.1f}s"
            f"{btc_move_note})"
        )

    # Time stop: only fire when ROI is truly negative (losing) AND not in
    # strong_favor.  Previous logic cut profitable positions that hadn't
    # reached profit_take threshold yet -- e.g. paper id=6 was UP-correct
    # but time_stop closed it at a loss because of mark-to-market lag.
    effective_timestop_roi = min(float(cfg.timestop_max_roi_pct), -2.0)  # never positive
    if (
        reason is None
        and hold_sec >= float(cfg.max_hold_sec)
        and mtm_roi_pct <= effective_timestop_roi
        and remaining <= float(cfg.timestop_max_remain_sec)
        and not strong_favor
    ):
        reason = (
            f"time_stop(hold={hold_sec:.1f}s, rem={remaining:.1f}s,"
            f" roi={mtm_roi_pct:+.2f}% <= {effective_timestop_roi:+.2f}%)"
        )

    peak = float(inp.peak_roi_pct)
    # Trailing stop: suppress when BTC is moving in our favour with enough
    # time left -- mark-to-market dips are normal noise in 5m binary markets.
    # Also suppress if ROI is still above break-even (> -5%) and remaining > 60s.
    # EXCEPTION: never suppress when peak was very high -- protecting large
    # profits takes priority over waiting for settlement.  The force_peak
    # threshold marks the level where profit protection overrides holding.
    high_peak_protect = peak >= float(cfg.trailing_force_peak_pct)
    trailing_suppress = (
        not high_peak_protect
        and (
            (strong_favor and mtm_roi_pct > float(cfg.favor_hold_break_even_floor_roi_pct))
            or (remaining >= 60.0 and mtm_roi_pct > -5.0 and not severe_adverse)
        )
    )
    if (
        reason is None
        and peak >= dynamic_trailing_peak_pct
        and (peak - mtm_roi_pct) >= dynamic_trailing_drop_pct
        and hold_sec >= float(cfg.trailing_stop_min_hold_sec)
        and not trailing_suppress
        and (trailing_late_window or peak >= float(cfg.trailing_force_peak_pct))
    ):
        reason = (
            f"trailing_stop(peak={peak:+.2f}%"
            f" -> current={mtm_roi_pct:+.2f}%"
            f" drop={peak - mtm_roi_pct:.2f}%"
            f" >= {dynamic_trailing_drop_pct:.1f}%"
            f", rem={remaining:.1f}s, favor={strong_favor}, late={trailing_late_window})"
        )

    if (
        reason is None
        and hold_sec >= float(cfg.trailing_stop_min_hold_sec)
        and peak >= float(cfg.break_even_peak_pct)
        and mtm_roi_pct <= float(cfg.break_even_floor_roi_pct)
        and not (
            strong_favor
            and mtm_roi_pct > float(cfg.favor_hold_break_even_floor_roi_pct)
        )
        and (break_even_late_window or peak >= float(cfg.break_even_force_peak_pct))
    ):
        reason = (
            f"break_even_protect(peak={peak:+.2f}%"
            f" -> current={mtm_roi_pct:+.2f}%"
            f", rem={remaining:.1f}s, favor={strong_favor}, late={break_even_late_window})"
        )

    # Profit-take: when ROI exceeds force threshold, ignore strong_favor --
    # locking in guaranteed large wins trumps directional momentum.
    # Binance-Chainlink divergence means even "winning" positions can lose
    # at settlement, so taking profit at extreme ROI is strictly better.
    profit_force = mtm_roi_pct >= float(cfg.profit_take_force_roi_pct)
    if (
        reason is None
        and mtm_roi_pct >= dynamic_profit_take_roi_pct
        and hold_sec >= float(cfg.profit_take_min_hold_sec)
        and (not strong_favor or profit_force)
        and (profit_take_late_window or profit_force)
    ):
        reason = (
            f"profit_take(roi={mtm_roi_pct:+.2f}% >= {dynamic_profit_take_roi_pct:+.2f}%"
            f", hold={hold_sec:.1f}s, rem={remaining:.1f}s, favor={strong_favor}, late={profit_take_late_window}"
            f", force={profit_force})"
        )

    return ExitPolicyDecision(
        reason=reason,
        opposite_hits=opposite_hits,
        dynamic_opposite_ask=dynamic_opposite_ask,
        dynamic_opposite_min_loss_roi_pct=dynamic_opposite_min_loss_roi_pct,
        dynamic_stop_loss_roi_pct=dynamic_stop_loss_roi_pct,
        dynamic_trailing_drop_pct=dynamic_trailing_drop_pct,
        dynamic_trailing_peak_pct=dynamic_trailing_peak_pct,
        dynamic_profit_take_roi_pct=dynamic_profit_take_roi_pct,
        directional_move_pct=directional_move_pct,
        boundary_sigma_pct=boundary_sigma_pct,
        strong_favor=strong_favor,
        severe_adverse=severe_adverse,
    )
