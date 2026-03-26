"""
Trade entry gate utilities.

Purpose:
- Block entries when expected net return is too low even if jury agrees.
- Keep decision consistent across dashboard/main/backtest/paper sim.
"""
from dataclasses import dataclass
import math
from typing import Optional

from config import config
from judges import MarketContext, estimate_ensemble_close_probability


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def _safe_pct_change(current: float, past: float) -> float:
    if past == 0:
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


def _price_n_seconds_ago(prices: list[float], ts: list[float], seconds: float) -> float | None:
    n = min(len(prices), len(ts))
    if n <= 0:
        return None
    latest_ts = float(ts[n - 1])
    target = latest_ts - max(1.0, float(seconds))
    for i in range(n - 1, -1, -1):
        if float(ts[i]) <= target:
            return float(prices[i])
    return float(prices[0])


def _jump_ratio_from_series(prices: list[float], ts: list[float]) -> float:
    n = min(len(prices), len(ts))
    if n < 10:
        return 0.0
    dlogs: list[float] = []
    for i in range(1, n):
        p0 = float(prices[i - 1])
        p1 = float(prices[i])
        dt = float(ts[i] - ts[i - 1])
        if p0 <= 0.0 or p1 <= 0.0 or dt <= 1e-6:
            continue
        dlogs.append(math.log(p1 / p0))
    if len(dlogs) < 6:
        return 0.0

    rv = sum(x * x for x in dlogs)
    if rv <= 1e-16:
        return 0.0

    abs_r = [abs(x) for x in dlogs]
    bv_acc = 0.0
    for i in range(1, len(abs_r)):
        bv_acc += abs_r[i] * abs_r[i - 1]
    bv = (math.pi / 2.0) * bv_acc
    bv = max(0.0, min(bv, rv))
    return _clamp((rv - bv) / max(rv, 1e-16), 0.0, 1.0)


def _up_regime_features(
    *,
    current_price: float,
    start_price: float,
    prices: list[float],
    ts: list[float],
) -> tuple[float, bool, float, float, float, float, float]:
    move_start = _safe_pct_change(current_price, start_price)
    p10 = _price_n_seconds_ago(prices, ts, 10.0)
    p30 = _price_n_seconds_ago(prices, ts, 30.0)
    p60 = _price_n_seconds_ago(prices, ts, 60.0)
    m10 = _safe_pct_change(current_price, p10) if p10 is not None else 0.0
    m30 = _safe_pct_change(current_price, p30) if p30 is not None else 0.0
    m60 = _safe_pct_change(current_price, p60) if p60 is not None else 0.0

    positive = 0
    total = 0
    for m in (m10, m30, m60):
        total += 1
        if m >= 0.0:
            positive += 1
    consistency = (positive / float(total)) if total > 0 else 0.5

    jump_ratio = _jump_ratio_from_series(prices, ts)
    whipsaw_thr = max(0.001, float(config.trading.up_regime_whipsaw_pct))
    whipsaw = (m10 * m30 < 0.0) and (abs(m10) >= whipsaw_thr) and (abs(m30) >= whipsaw_thr)
    whipsaw_penalty = 1.0 if whipsaw else 0.0

    score = 0.50
    score += 0.22 * _clamp(move_start / max(0.001, float(config.trading.up_regime_move_scale_pct)), -1.0, 1.0)
    score += 0.18 * _clamp(m10 / max(0.001, float(config.trading.up_regime_mom10_scale_pct)), -1.0, 1.0)
    score += 0.16 * _clamp(m30 / max(0.001, float(config.trading.up_regime_mom30_scale_pct)), -1.0, 1.0)
    score += 0.08 * _clamp(m60 / max(0.001, float(config.trading.up_regime_mom60_scale_pct)), -1.0, 1.0)
    score += 0.12 * ((consistency - 0.5) * 2.0)

    max_jump = _clamp(float(config.trading.up_regime_max_jump_ratio), 0.0, 1.0)
    jump_penalty = _clamp((jump_ratio - max_jump) / max(1e-9, 1.0 - max_jump), 0.0, 1.0)
    score -= 0.12 * jump_penalty
    score -= 0.10 * whipsaw_penalty
    score = _clamp(score, 0.0, 1.0)

    min_score = _clamp(float(config.trading.up_regime_min_score), 0.0, 1.0)
    min_mom30 = float(config.trading.up_regime_min_mom30_pct)
    min_move_start = float(config.trading.up_regime_min_move_from_start_pct)
    regime_pass = bool(score >= min_score and m30 >= min_mom30 and move_start >= min_move_start)

    return (
        float(score),
        regime_pass,
        float(move_start),
        float(m10),
        float(m30),
        float(m60),
        float(jump_ratio),
    )


@dataclass
class EntryGateResult:
    allow: bool
    expected_roi: float
    implied_prob: float
    model_prob: float
    break_even_prob: float
    profit_break_even_prob: float
    win_prob_floor: float
    win_prob_pass: bool
    fair_prob_up: float
    dispersion: float
    aligned_move_pct: float
    boundary_dist_pct: float
    boundary_sigma_pct: float
    alignment_penalty: float
    ambiguity_penalty: float
    up_regime_score: float
    up_regime_pass: bool
    up_regime_reason: str
    per_judge_probs: dict[str, float]
    reason: str


def evaluate_entry_gate(
    direction: str,
    entry_price: float,
    current_price: float,
    start_price: float,
    seconds_elapsed: float,
    jury_confidence: float,
    support_ratio: float,
    seconds_remaining: Optional[float] = None,
    recent_prices: Optional[list[float]] = None,
    recent_timestamps: Optional[list[float]] = None,
    poly_up_ask: Optional[float] = None,
    poly_down_ask: Optional[float] = None,
    recent_results: Optional[list[str]] = None,
) -> EntryGateResult:
    if direction not in ("UP", "DOWN"):
        return EntryGateResult(
            False,
            -1.0,
            0.0,
            0.0,
            1.0,
            1.0,
            1.0,
            False,
            0.5,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            True,
            "n/a",
            {},
            "direction not tradable",
        )
    if not (0.0 < entry_price < 1.0):
        return EntryGateResult(
            False,
            -1.0,
            0.0,
            0.0,
            1.0,
            1.0,
            1.0,
            False,
            0.5,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            True,
            "n/a",
            {},
            "invalid entry price",
        )

    elapsed = max(1.0, float(seconds_elapsed))
    remaining = max(1.0, float(seconds_remaining)) if seconds_remaining is not None else max(1.0, 300.0 - elapsed)

    prices = list(recent_prices or [])
    ts = list(recent_timestamps or [])
    if len(prices) < 10 or len(ts) < 10:
        prices = [float(start_price), float(current_price)]
        ts = [0.0, elapsed]
    else:
        n = min(len(prices), len(ts))
        prices = [float(x) for x in prices[:n]]
        ts = [float(x) for x in ts[:n]]

    if poly_up_ask is None and poly_down_ask is None:
        if direction == "UP":
            up_ask = float(entry_price)
            down_ask = _clamp(1.0 - float(entry_price), 0.001, 0.999)
        else:
            down_ask = float(entry_price)
            up_ask = _clamp(1.0 - float(entry_price), 0.001, 0.999)
    else:
        up_ask = float(poly_up_ask) if poly_up_ask is not None else _clamp(1.0 - float(poly_down_ask), 0.001, 0.999)
        down_ask = float(poly_down_ask) if poly_down_ask is not None else _clamp(1.0 - float(poly_up_ask), 0.001, 0.999)

    gate_ctx = MarketContext(
        current_binance_price=float(current_price),
        market_start_price=float(start_price),
        recent_prices=prices,
        recent_timestamps=ts,
        poly_up_price=float(up_ask),
        poly_down_price=float(down_ask),
        seconds_elapsed=float(elapsed),
        seconds_remaining=float(remaining),
        poly_up_ask=float(up_ask),
        poly_down_ask=float(down_ask),
        recent_results=list(recent_results or []),
    )
    fair_up, per_judge_probs, dispersion = estimate_ensemble_close_probability(gate_ctx)
    base_prob = fair_up if direction == "UP" else (1.0 - fair_up)

    # Conservative boost from jury confidence/support, damped by model disagreement.
    conf = float(_clamp(jury_confidence, 0.0, 1.0))
    support = float(_clamp(support_ratio, 0.0, 1.0))
    disagreement_penalty = _clamp(dispersion, 0.0, 0.5) / 0.5  # 0..1
    boost = conf * (0.010 + 0.030 * support) * (1.0 - 0.50 * disagreement_penalty)

    move_from_start_pct = _safe_pct_change(float(current_price), float(start_price))
    aligned_move_pct = move_from_start_pct if direction == "UP" else -move_from_start_pct
    boundary_dist_pct = abs(move_from_start_pct)

    min_aligned_move_pct = max(0.0, float(config.trading.close_prob_min_aligned_move_pct))
    alignment_penalty_max = _clamp(float(config.trading.close_prob_alignment_penalty_max), 0.0, 0.35)
    alignment_penalty = 0.0
    if min_aligned_move_pct > 1e-9:
        shortfall = _clamp((min_aligned_move_pct - aligned_move_pct) / min_aligned_move_pct, 0.0, 1.0)
        alignment_penalty = alignment_penalty_max * shortfall

    # Boundary uncertainty shrink:
    # if BTC is still too close to the start-price boundary relative to expected
    # remaining noise, penalize directional probability to avoid coin-flip entries.
    sigma = _estimate_sigma_per_sqrt_sec(prices, ts)
    boundary_sigma_pct = 0.0
    ambiguity_penalty = 0.0
    if sigma is not None and sigma > 0.0:
        band_mult = max(0.0, float(config.trading.close_prob_boundary_sigma_mult))
        penalty_max = _clamp(float(config.trading.close_prob_uncertainty_penalty_max), 0.0, 0.35)
        boundary_sigma_pct = float(sigma * math.sqrt(max(1.0, remaining)) * 100.0)
        boundary_band_pct = band_mult * boundary_sigma_pct
        if boundary_band_pct > 1e-9:
            ambiguity_ratio = _clamp((boundary_band_pct - boundary_dist_pct) / boundary_band_pct, 0.0, 1.0)
            disagreement_factor = 0.55 + 0.45 * disagreement_penalty
            ambiguity_penalty = penalty_max * ambiguity_ratio * disagreement_factor

    prob_penalty = alignment_penalty + ambiguity_penalty
    model_prob = float(_clamp(base_prob + boost - prob_penalty, 0.001, 0.999))

    up_regime_score = 0.0
    up_regime_pass = True
    up_regime_reason = "n/a"
    up_move_start = 0.0
    up_m10 = 0.0
    up_m30 = 0.0
    up_m60 = 0.0
    up_jump_ratio = 0.0
    if direction == "UP" and bool(config.trading.up_regime_filter_enabled):
        (
            up_regime_score,
            up_regime_pass,
            up_move_start,
            up_m10,
            up_m30,
            up_m60,
            up_jump_ratio,
        ) = _up_regime_features(
            current_price=float(current_price),
            start_price=float(start_price),
            prices=prices,
            ts=ts,
        )
        up_regime_reason = (
            f"score={up_regime_score:.3f} pass={bool(up_regime_pass)} "
            f"(vs_start={up_move_start:+.4f}%, m10={up_m10:+.4f}%, m30={up_m30:+.4f}%, "
            f"m60={up_m60:+.4f}%, jump={up_jump_ratio:.3f})"
        )

    fee_rate = max(0.0, float(config.trading.fee_rate))
    min_roi = float(config.trading.min_expected_roi)

    # -- Spread cost awareness (from RL article insight) --
    # The overround (up_ask + down_ask - 1.0) represents the book's spread tax.
    # If we need to early-exit, we lose roughly half the overround.  Factor this
    # into the effective fee so that wide-spread windows need bigger edge.
    overround = max(0.0, up_ask + down_ask - 1.0)
    spread_cost = overround * 0.5  # half the overround as execution friction
    effective_fee = fee_rate + spread_cost

    expected_roi = float((model_prob / entry_price) - 1.0 - effective_fee)
    break_even_prob = float(entry_price * (1.0 + effective_fee + min_roi))
    profit_break_even_prob = float(entry_price * (1.0 + effective_fee))
    win_prob_floor = float(
        max(
            _clamp(float(config.trading.min_win_probability), 0.0, 0.999),
            profit_break_even_prob + max(0.0, float(config.trading.win_prob_margin)),
        )
    )
    win_prob_pass = bool(model_prob >= win_prob_floor)
    # -- Coinflip weak-edge guard --
    # When entry_price is near 0.50 (the coin-flip zone), require a stronger
    # probability edge before allowing entry.  Most recent losses came from
    # entering at 0.47-0.53 with thin margins that flipped.
    coinflip_lo = float(getattr(config.trading, "coinflip_guard_lo", 0.44))
    coinflip_hi = float(getattr(config.trading, "coinflip_guard_hi", 0.56))
    coinflip_min_prob_margin = float(getattr(config.trading, "coinflip_min_prob_margin", 0.06))
    coinflip_min_confidence = float(getattr(config.trading, "coinflip_min_confidence", 0.45))
    coinflip_blocked = False
    coinflip_reason = ""
    if coinflip_lo <= entry_price <= coinflip_hi:
        prob_margin = model_prob - entry_price  # how much model exceeds implied
        if prob_margin < coinflip_min_prob_margin:
            coinflip_blocked = True
            coinflip_reason = (
                f"coinflip_guard(ask={entry_price:.3f} in [{coinflip_lo:.2f},{coinflip_hi:.2f}], "
                f"prob_margin={prob_margin:.4f} < {coinflip_min_prob_margin:.3f})"
            )
        elif jury_confidence < coinflip_min_confidence:
            coinflip_blocked = True
            coinflip_reason = (
                f"coinflip_guard(ask={entry_price:.3f}, conf={jury_confidence:.3f}"
                f" < {coinflip_min_confidence:.3f})"
            )

    mode = str(getattr(config.trading, "entry_decision_mode", "HYBRID")).strip().upper()
    if mode == "EV_FIRST":
        allow = bool(expected_roi >= min_roi and up_regime_pass and not coinflip_blocked)
    elif mode == "PROBABILITY_FIRST":
        allow = bool(win_prob_pass and up_regime_pass and not coinflip_blocked)
    else:
        # HYBRID (or unknown): require both EV and win-probability gates.
        allow = bool(expected_roi >= min_roi and win_prob_pass and up_regime_pass and not coinflip_blocked)

    fee_info = f"fee={fee_rate:.2%}+spread={spread_cost:.3f}"
    if coinflip_blocked:
        reason = (
            f"skip {coinflip_reason} | "
            f"net_ev={expected_roi:+.3%} (p={model_prob:.3f}, fair_up={fair_up:.3f}, "
            f"disp={dispersion:.3f}, ask={entry_price:.3f}, conf={jury_confidence:.3f}, {fee_info})"
        )
    elif (direction == "UP") and bool(config.trading.up_regime_filter_enabled) and (not up_regime_pass):
        reason = (
            f"skip up_regime_filter ({up_regime_reason}) | "
            f"net_ev={expected_roi:+.3%} target={min_roi:.3%} "
            f"(p={model_prob:.3f}, fair_up={fair_up:.3f}, disp={dispersion:.3f}, "
            f"align={aligned_move_pct:+.4f}%, pen={prob_penalty:.3f}, ask={entry_price:.3f}, {fee_info})"
        )
    elif not win_prob_pass:
        reason = (
            f"skip low win_prob={model_prob:.3f} < floor={win_prob_floor:.3f} "
            f"(mode={mode}, p_profit>={profit_break_even_prob:.3f}, "
            f"fair_up={fair_up:.3f}, disp={dispersion:.3f}, ask={entry_price:.3f}, {fee_info})"
        )
    elif allow:
        reason = (
            f"net_ev={expected_roi:+.3%} >= target={min_roi:.3%} "
            f"(mode={mode}, p={model_prob:.3f}, p_floor={win_prob_floor:.3f}, "
            f"fair_up={fair_up:.3f}, disp={dispersion:.3f}, align={aligned_move_pct:+.4f}%, "
            f"pen={prob_penalty:.3f}, ask={entry_price:.3f}, {fee_info})"
        )
    else:
        reason = (
            f"skip low net_ev={expected_roi:+.3%} < target={min_roi:.3%} "
            f"(mode={mode}, need p>={break_even_prob:.3f}, p={model_prob:.3f}, p_floor={win_prob_floor:.3f}, "
            f"fair_up={fair_up:.3f}, disp={dispersion:.3f}, align={aligned_move_pct:+.4f}%, "
            f"pen={prob_penalty:.3f}, ask={entry_price:.3f}, {fee_info})"
        )

    return EntryGateResult(
        allow=bool(allow),
        expected_roi=float(expected_roi),
        implied_prob=float(entry_price),
        model_prob=float(model_prob),
        break_even_prob=float(break_even_prob),
        profit_break_even_prob=float(profit_break_even_prob),
        win_prob_floor=float(win_prob_floor),
        win_prob_pass=bool(win_prob_pass),
        fair_prob_up=float(fair_up),
        dispersion=float(dispersion),
        aligned_move_pct=float(aligned_move_pct),
        boundary_dist_pct=float(boundary_dist_pct),
        boundary_sigma_pct=float(boundary_sigma_pct),
        alignment_penalty=float(alignment_penalty),
        ambiguity_penalty=float(ambiguity_penalty),
        up_regime_score=float(up_regime_score),
        up_regime_pass=bool(up_regime_pass),
        up_regime_reason=str(up_regime_reason),
        per_judge_probs={str(k): float(v) for k, v in (per_judge_probs or {}).items()},
        reason=reason,
    )


def apply_fee_to_pnl(raw_pnl: float, stake: float) -> float:
    fee_rate = max(0.0, float(config.trading.fee_rate))
    if stake <= 0.0 or fee_rate <= 0.0:
        return raw_pnl
    return raw_pnl - (stake * fee_rate)
