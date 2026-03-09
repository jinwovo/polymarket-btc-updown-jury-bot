"""
Trade entry gate utilities.

Purpose:
- Block entries when expected net return is too low even if jury agrees.
- Keep decision consistent across dashboard/main/backtest/paper sim.
"""
from dataclasses import dataclass
from typing import Optional

from config import config
from judges import MarketContext, estimate_ensemble_close_probability


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


@dataclass
class EntryGateResult:
    allow: bool
    expected_roi: float
    implied_prob: float
    model_prob: float
    break_even_prob: float
    fair_prob_up: float
    dispersion: float
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
        return EntryGateResult(False, -1.0, 0.0, 0.0, 1.0, 0.5, 1.0, {}, "direction not tradable")
    if not (0.0 < entry_price < 1.0):
        return EntryGateResult(False, -1.0, 0.0, 0.0, 1.0, 0.5, 1.0, {}, "invalid entry price")

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
    model_prob = float(_clamp(base_prob + boost, 0.001, 0.999))

    fee_rate = max(0.0, float(config.trading.fee_rate))
    min_roi = float(config.trading.min_expected_roi)
    expected_roi = float((model_prob / entry_price) - 1.0 - fee_rate)
    break_even_prob = float(entry_price * (1.0 + fee_rate + min_roi))
    allow = bool(expected_roi >= min_roi)

    if allow:
        reason = (
            f"net_ev={expected_roi:+.3%} >= target={min_roi:.3%} "
            f"(p={model_prob:.3f}, fair_up={fair_up:.3f}, disp={dispersion:.3f}, ask={entry_price:.3f}, fee={fee_rate:.2%})"
        )
    else:
        reason = (
            f"skip low net_ev={expected_roi:+.3%} < target={min_roi:.3%} "
            f"(need p>={break_even_prob:.3f}, p={model_prob:.3f}, fair_up={fair_up:.3f}, disp={dispersion:.3f}, ask={entry_price:.3f}, fee={fee_rate:.2%})"
        )

    return EntryGateResult(
        allow=bool(allow),
        expected_roi=float(expected_roi),
        implied_prob=float(entry_price),
        model_prob=float(model_prob),
        break_even_prob=float(break_even_prob),
        fair_prob_up=float(fair_up),
        dispersion=float(dispersion),
        per_judge_probs={str(k): float(v) for k, v in (per_judge_probs or {}).items()},
        reason=reason,
    )


def apply_fee_to_pnl(raw_pnl: float, stake: float) -> float:
    fee_rate = max(0.0, float(config.trading.fee_rate))
    if stake <= 0.0 or fee_rate <= 0.0:
        return raw_pnl
    return raw_pnl - (stake * fee_rate)
