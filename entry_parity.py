from __future__ import annotations

from dataclasses import dataclass


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


@dataclass(frozen=True)
class ParityAdaptiveConfig:
    min_expected_roi: float
    min_support_ratio: float
    min_confidence: float
    max_entry_price: float
    adaptive_max_ask_floor: float
    require_unanimous: bool
    strictness_unanimous_at: float
    base_trade_gap_sec: float
    target_trade_gap_sec: float
    stale_relax_max: float
    profit_mode: str = "aggressive"
    aggressive_entry_relax: float = 0.20
    aggressive_gap_mult: float = 0.65


@dataclass(frozen=True)
class ParityAdaptiveState:
    loss_streak: int
    recent_loss_rate: float
    drawdown_pct: float
    perf_count: int
    perf_pnl: float
    stale_relax: float


@dataclass(frozen=True)
class ParityAdaptiveThresholds:
    strictness: float
    strictness_eff: float
    adaptive_min_ev: float
    adaptive_min_support: float
    adaptive_min_conf: float
    adaptive_max_ask: float
    dynamic_gap: float
    require_unanimous: bool


def compute_parity_thresholds(
    cfg: ParityAdaptiveConfig,
    state: ParityAdaptiveState,
) -> ParityAdaptiveThresholds:
    strictness = 0.0
    strictness += min(0.30, float(state.loss_streak) * 0.10)
    strictness += max(0.0, float(state.recent_loss_rate) - 0.55) * 0.50
    strictness += min(0.25, float(state.drawdown_pct) * 0.60)
    if int(state.perf_count) >= 4 and float(state.perf_pnl) < 0.0:
        strictness += 0.08
    strictness = _clamp(strictness, 0.0, 1.0)

    stale_relax_gain = _clamp(float(cfg.stale_relax_max), 0.0, 1.0) * float(state.stale_relax)
    strictness_eff = _clamp(strictness * (1.0 - stale_relax_gain), 0.0, 1.0)

    adaptive_min_ev = (
        float(cfg.min_expected_roi) * (1.0 + 0.9 * strictness_eff)
        + min(int(state.loss_streak), 3) * 0.005
    )
    adaptive_min_support = _clamp(
        float(cfg.min_support_ratio) + 0.20 * strictness_eff,
        float(cfg.min_support_ratio),
        1.0,
    )
    adaptive_min_conf = _clamp(
        float(cfg.min_confidence) + 0.18 * strictness_eff,
        float(cfg.min_confidence),
        0.80,
    )
    ask_floor = _clamp(float(cfg.adaptive_max_ask_floor), 0.40, float(cfg.max_entry_price))
    adaptive_max_ask = _clamp(
        float(cfg.max_entry_price) - 0.08 * strictness_eff,
        ask_floor,
        float(cfg.max_entry_price),
    )

    if str(cfg.profit_mode or "aggressive").strip().lower() == "aggressive":
        relax = _clamp(float(cfg.aggressive_entry_relax), 0.0, 0.60)
        adaptive_min_ev = max(0.0, adaptive_min_ev * (1.0 - relax))
        adaptive_min_support = _clamp(adaptive_min_support - (0.10 * relax), 0.35, 1.0)
        adaptive_min_conf = _clamp(adaptive_min_conf - (0.08 * relax), 0.15, 0.80)
        adaptive_max_ask = _clamp(adaptive_max_ask + (0.03 * relax), ask_floor, 0.54)

    dynamic_gap = float(cfg.base_trade_gap_sec) + strictness_eff * (
        max(float(cfg.base_trade_gap_sec), float(cfg.target_trade_gap_sec))
        - float(cfg.base_trade_gap_sec)
    )
    if str(cfg.profit_mode or "aggressive").strip().lower() == "aggressive":
        dynamic_gap *= _clamp(float(cfg.aggressive_gap_mult), 0.35, 1.0)
    dynamic_gap = max(0.0, dynamic_gap)

    require_unanimous = bool(cfg.require_unanimous) or strictness_eff >= float(cfg.strictness_unanimous_at)

    return ParityAdaptiveThresholds(
        strictness=float(strictness),
        strictness_eff=float(strictness_eff),
        adaptive_min_ev=float(adaptive_min_ev),
        adaptive_min_support=float(adaptive_min_support),
        adaptive_min_conf=float(adaptive_min_conf),
        adaptive_max_ask=float(adaptive_max_ask),
        dynamic_gap=float(dynamic_gap),
        require_unanimous=bool(require_unanimous),
    )
