"""
Trade entry gate utilities.

Purpose:
- Block entries when expected net return is too low even if jury agrees.
- Keep decision consistent across dashboard/main/backtest/paper sim.
"""
from dataclasses import dataclass
import math

from config import config


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _fair_prob_up(current_price: float, start_price: float, seconds_elapsed: float) -> float:
    if start_price <= 0:
        return 0.5
    move_pct = ((current_price - start_price) / start_price) * 100.0
    progress = _clamp(seconds_elapsed / 300.0, 0.0, 1.0)
    effective_move = move_pct * (0.9 + progress * 1.8)
    k = 20.0 + progress * 18.0
    z = _clamp(k * effective_move / 100.0, -20.0, 20.0)
    return _clamp(1.0 / (1.0 + math.exp(-z)), 0.001, 0.999)


@dataclass
class EntryGateResult:
    allow: bool
    expected_roi: float
    implied_prob: float
    model_prob: float
    break_even_prob: float
    reason: str


def evaluate_entry_gate(
    direction: str,
    entry_price: float,
    current_price: float,
    start_price: float,
    seconds_elapsed: float,
    jury_confidence: float,
    support_ratio: float,
) -> EntryGateResult:
    if direction not in ("UP", "DOWN"):
        return EntryGateResult(False, -1.0, 0.0, 0.0, 1.0, "direction not tradable")
    if not (0.0 < entry_price < 1.0):
        return EntryGateResult(False, -1.0, 0.0, 0.0, 1.0, "invalid entry price")

    fair_up = _fair_prob_up(current_price, start_price, seconds_elapsed)
    base_prob = fair_up if direction == "UP" else (1.0 - fair_up)

    # Conservative boost from jury confidence/support on top of market-fair estimate.
    # Kept deliberately small to avoid overfitting jury accuracy.
    conf = _clamp(jury_confidence, 0.0, 1.0)
    support = _clamp(support_ratio, 0.0, 1.0)
    boost = conf * (0.03 + 0.07 * support)
    model_prob = _clamp(base_prob + boost, 0.001, 0.999)

    fee_rate = max(0.0, float(config.trading.fee_rate))
    min_roi = float(config.trading.min_expected_roi)
    expected_roi = (model_prob / entry_price) - 1.0 - fee_rate
    break_even_prob = entry_price * (1.0 + fee_rate + min_roi)
    allow = expected_roi >= min_roi

    if allow:
        reason = (
            f"net_ev={expected_roi:+.3%} >= target={min_roi:.3%} "
            f"(p={model_prob:.3f}, ask={entry_price:.3f}, fee={fee_rate:.2%})"
        )
    else:
        reason = (
            f"skip low net_ev={expected_roi:+.3%} < target={min_roi:.3%} "
            f"(need p>={break_even_prob:.3f}, p={model_prob:.3f}, ask={entry_price:.3f}, fee={fee_rate:.2%})"
        )

    return EntryGateResult(
        allow=allow,
        expected_roi=expected_roi,
        implied_prob=entry_price,
        model_prob=model_prob,
        break_even_prob=break_even_prob,
        reason=reason,
    )


def apply_fee_to_pnl(raw_pnl: float, stake: float) -> float:
    fee_rate = max(0.0, float(config.trading.fee_rate))
    if stake <= 0.0 or fee_rate <= 0.0:
        return raw_pnl
    return raw_pnl - (stake * fee_rate)
