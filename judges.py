"""
Probability-first judges for BTC Up/Down 5-minute markets.

Each judge estimates close-time probability p_up = P(S_close >= S_start | info_t),
then compares p_up against executable ask prices (UP/DOWN).
"""
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)
_SIGMA_FLOOR_PER_SQRT_SEC = 1e-4


class Vote(Enum):
    UP = "UP"
    DOWN = "DOWN"
    ABSTAIN = "ABSTAIN"


@dataclass
class JudgeVerdict:
    vote: Vote
    confidence: float  # 0.0 to 1.0
    reason: str
    judge_name: str


@dataclass
class MarketContext:
    """All data a judge needs to make a decision."""
    # Binance data
    current_binance_price: float
    market_start_price: float
    recent_prices: list[float]
    recent_timestamps: list[float]
    # Polymarket data
    poly_up_price: float
    poly_down_price: float
    seconds_elapsed: float
    seconds_remaining: float
    # Optional orderbook fields
    poly_up_bid: Optional[float] = None
    poly_up_ask: Optional[float] = None
    poly_down_bid: Optional[float] = None
    poly_down_ask: Optional[float] = None
    # Historical performance
    recent_results: list[str] = None


def _safe_pct_change(current: float, past: float) -> float:
    if past == 0:
        return 0.0
    return ((current - past) / past) * 100.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def _clamp01(x: float) -> float:
    return _clamp(x, 0.0, 1.0)


def _norm_cdf(z: float) -> float:
    # Standard normal CDF via erf to avoid heavy dependencies.
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _fallback_prob_from_move(ctx: MarketContext) -> float:
    """Fallback p_up when local volatility estimation is unstable."""
    if ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
        return 0.5
    move_pct = _safe_pct_change(ctx.current_binance_price, ctx.market_start_price)
    progress = _clamp01(float(ctx.seconds_elapsed) / 300.0)
    k = 16.0 + 22.0 * progress
    z = _clamp(k * move_pct / 100.0, -20.0, 20.0)
    return _clamp01(1.0 / (1.0 + math.exp(-z)))


def _get_ask_prices(ctx: MarketContext) -> tuple[float, float] | tuple[None, None]:
    up_px = (
        ctx.poly_up_ask
        if (ctx.poly_up_ask is not None and 0.0 < ctx.poly_up_ask < 1.0)
        else ctx.poly_up_price
    )
    down_px = (
        ctx.poly_down_ask
        if (ctx.poly_down_ask is not None and 0.0 < ctx.poly_down_ask < 1.0)
        else ctx.poly_down_price
    )
    if not (0.0 < up_px < 1.0 and 0.0 < down_px < 1.0):
        return (None, None)
    return (float(up_px), float(down_px))


def _estimate_diffusion_params(
    ctx: MarketContext,
    min_samples: int = 24,
) -> tuple[float, float] | tuple[None, None]:
    """Estimate GBM-like (mu, sigma) from recent log returns."""
    n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
    if n < min_samples:
        return (None, None)

    prices = np.asarray(ctx.recent_prices[:n], dtype=float)
    ts = np.asarray(ctx.recent_timestamps[:n], dtype=float)
    if np.any(prices <= 0):
        return (None, None)

    logp = np.log(prices)
    dlog = np.diff(logp)
    dt = np.diff(ts)
    valid = dt > 1e-6
    if not np.any(valid):
        return (None, None)

    dlog = dlog[valid]
    dt = dt[valid]
    if len(dlog) < max(8, min_samples // 2):
        return (None, None)

    total_dt = float(np.sum(dt))
    if total_dt <= 1e-6:
        return (None, None)

    # MLE under dlogS = mu*dt + sigma*dW.
    mu = float(np.sum(dlog) / total_dt)
    resid = dlog - (mu * dt)
    var = float(np.sum((resid ** 2) / np.maximum(dt, 1e-6)) / len(resid))
    sigma = math.sqrt(max(var, 1e-12))
    return (mu, sigma)


def _close_prob_from_diffusion(ctx: MarketContext, mu: float, sigma: float) -> float:
    if (
        ctx.market_start_price <= 0
        or ctx.current_binance_price <= 0
        or sigma is None
        or sigma <= 1e-10
    ):
        return _fallback_prob_from_move(ctx)

    t = max(1.0, float(ctx.seconds_remaining))
    sigma = max(float(sigma), _SIGMA_FLOOR_PER_SQRT_SEC)
    x = math.log(ctx.current_binance_price / ctx.market_start_price)
    # OU-style time decay for drift memory + cap to avoid saturation.
    decay = math.exp(-1.6 * (t / 300.0))
    raw_drift = (mu - 0.5 * sigma * sigma) * t
    drift = _clamp(raw_drift * decay, -0.0018, 0.0018)
    denom = sigma * math.sqrt(t)
    if denom < 1e-8:
        return 1.0 if (x + drift) > 0 else 0.0
    z = _clamp((x + drift) / denom, -8.0, 8.0)
    return _clamp01(_norm_cdf(z))


def _edge_vote(
    p_up: float,
    up_px: float,
    down_px: float,
    min_edge: float,
    tie_margin: float = 0.003,
) -> tuple[Vote, float, float, float]:
    """
    Returns (vote, winning_edge, up_edge, down_edge).
    """
    p_up = _clamp01(p_up)
    p_down = 1.0 - p_up
    up_edge = p_up - up_px
    down_edge = p_down - down_px

    if up_edge > (down_edge + tie_margin) and up_edge > min_edge:
        return (Vote.UP, up_edge, up_edge, down_edge)
    if down_edge > (up_edge + tie_margin) and down_edge > min_edge:
        return (Vote.DOWN, down_edge, up_edge, down_edge)
    return (Vote.ABSTAIN, 0.0, up_edge, down_edge)


def _edge_confidence(
    p_up: float,
    edge: float,
    *,
    scale: float = 5.0,
    certainty_boost: float = 1.8,
    quality: float = 1.0,
) -> float:
    certainty = abs(_clamp01(p_up) - 0.5) * 2.0
    c = min(max(edge, 0.0) * (scale + certainty_boost * certainty), 1.0)
    return _clamp01(c * _clamp(quality, 0.1, 1.0))


# ---------------------------------------------------------------------------
# Judge 1: Technical Analysis -> close probability
# ---------------------------------------------------------------------------
class TechnicalJudge:
    name = "TechnicalJudge"

    def __init__(
        self,
        rsi_period: int = 14,
        bb_period: int = 20,
        bb_std: float = 2.0,
        min_edge: float = 0.016,
    ):
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.min_edge = max(0.0, float(min_edge))

    def _compute_rsi(self, prices: np.ndarray) -> float:
        if len(prices) < self.rsi_period + 1:
            return 50.0
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[-self.rsi_period:])
        avg_loss = np.mean(losses[-self.rsi_period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _compute_momentum(self, prices: np.ndarray, lookback: int = 5) -> float:
        if len(prices) < lookback + 1:
            return 0.0
        return _safe_pct_change(prices[-1], prices[-lookback - 1])

    def _compute_bollinger(self, prices: np.ndarray) -> tuple[float, float, float]:
        if len(prices) < self.bb_period:
            mid = float(np.mean(prices))
            std = float(np.std(prices)) if len(prices) > 1 else 0.0
            return mid - self.bb_std * std, mid, mid + self.bb_std * std
        window = prices[-self.bb_period:]
        mid = float(np.mean(window))
        std = float(np.std(window))
        return mid - self.bb_std * std, mid, mid + self.bb_std * std

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        if len(ctx.recent_prices) < 10:
            return None
        if ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return None

        prices = np.asarray(ctx.recent_prices, dtype=float)
        if len(prices) > 120:
            step = max(1, len(prices) // 60)
            candles = prices[::step]
        else:
            candles = prices

        rsi = self._compute_rsi(candles)
        momentum = self._compute_momentum(candles)
        bb_lower, _bb_mid, bb_upper = self._compute_bollinger(candles)
        current = float(ctx.current_binance_price)
        start_move = _safe_pct_change(current, ctx.market_start_price)

        if bb_upper > bb_lower:
            bb_position = (current - bb_lower) / (bb_upper - bb_lower)
        else:
            bb_position = 0.5

        rsi_term = _clamp((rsi - 50.0) / 30.0, -1.0, 1.0)
        mom_term = _clamp(momentum / 0.10, -1.0, 1.0)
        bb_term = _clamp((bb_position - 0.5) * 2.0, -1.0, 1.0)
        move_term = _clamp(start_move / 0.15, -1.0, 1.0)
        tech_score = _clamp(
            0.34 * rsi_term + 0.30 * mom_term + 0.18 * bb_term + 0.18 * move_term,
            -1.0,
            1.0,
        )

        mu, sigma = _estimate_diffusion_params(ctx, min_samples=24)
        if mu is None or sigma is None:
            return _fallback_prob_from_move(ctx)

        # Convert technical score into a small drift tilt for close probability.
        mu_tilt = tech_score * sigma * 0.22
        return _close_prob_from_diffusion(ctx, mu + mu_tilt, sigma)

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        if len(ctx.recent_prices) < 10:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient price data", self.name)

        up_px, down_px = _get_ask_prices(ctx)
        if up_px is None or down_px is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        p_up = self.estimate_prob(ctx)
        if p_up is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Unable to estimate close probability", self.name)

        prices = np.asarray(ctx.recent_prices, dtype=float)
        step = max(1, len(prices) // 60) if len(prices) > 120 else 1
        candles = prices[::step]
        rsi = self._compute_rsi(candles)
        momentum = self._compute_momentum(candles)
        bb_lower, _bb_mid, bb_upper = self._compute_bollinger(candles)
        current = float(ctx.current_binance_price)
        bb_position = (current - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5
        tech_score = _clamp(
            0.34 * _clamp((rsi - 50.0) / 30.0, -1.0, 1.0)
            + 0.30 * _clamp(momentum / 0.10, -1.0, 1.0)
            + 0.18 * _clamp((bb_position - 0.5) * 2.0, -1.0, 1.0)
            + 0.18 * _clamp(_safe_pct_change(current, ctx.market_start_price) / 0.15, -1.0, 1.0),
            -1.0,
            1.0,
        )

        vote, edge, up_edge, down_edge = _edge_vote(
            p_up,
            up_px,
            down_px,
            min_edge=self.min_edge,
            tie_margin=0.003,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={p_up:.3f}, no edge: up={up_edge:+.3f} down={down_edge:+.3f}",
                self.name,
            )

        confidence = _edge_confidence(p_up, edge, scale=4.8, certainty_boost=1.6)
        reason = (
            f"p_up={p_up:.3f}, edge=({up_edge:+.3f}/{down_edge:+.3f}), "
            f"tech={tech_score:+.2f}, RSI={rsi:.1f}, mom={momentum:+.4f}%, bb={bb_position:.2f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 2: Price Divergence / Arbitrage -> close probability
# ---------------------------------------------------------------------------
class ArbitrageJudge:
    name = "ArbitrageJudge"

    def __init__(self, min_edge: float = 0.02, min_samples: int = 24):
        self.min_edge = max(0.0, float(min_edge))
        self.min_samples = max(10, int(min_samples))

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        if ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return None
        mu, sigma = _estimate_diffusion_params(ctx, min_samples=self.min_samples)
        if mu is None or sigma is None:
            return _fallback_prob_from_move(ctx)
        return _close_prob_from_diffusion(ctx, mu, sigma)

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        if ctx.market_start_price <= 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "No market start price", self.name)

        up_px, down_px = _get_ask_prices(ctx)
        if up_px is None or down_px is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        fair_prob_up = self.estimate_prob(ctx)
        if fair_prob_up is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Unable to estimate close probability", self.name)

        mu, sigma = _estimate_diffusion_params(ctx, min_samples=self.min_samples)

        vote, edge, up_edge, down_edge = _edge_vote(
            fair_prob_up,
            up_px,
            down_px,
            min_edge=self.min_edge,
            tie_margin=0.003,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={fair_prob_up:.3f}, no significant edge: up={up_edge:+.3f}, down={down_edge:+.3f}",
                self.name,
            )

        confidence = _edge_confidence(fair_prob_up, edge, scale=5.2, certainty_boost=1.8)
        market_prob_up = up_px / max(1e-9, (up_px + down_px))
        mu_str = f"{mu:+.2e}" if mu is not None else "n/a"
        sigma_str = f"{sigma:.2e}" if sigma is not None else "n/a"
        reason = (
            f"p_up={fair_prob_up:.3f}, mkt_p_up={market_prob_up:.3f}, edge={edge:+.3f}, "
            f"asks=({up_px:.3f}/{down_px:.3f}), mu={mu_str}, sigma={sigma_str}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 3: Statistical / Pattern -> close probability
# ---------------------------------------------------------------------------
class StatisticalJudge:
    name = "StatisticalJudge"

    def __init__(self, base_min_edge: float = 0.018):
        self.base_min_edge = max(0.0, float(base_min_edge))

    def _log_returns(
        self, prices: list[float], timestamps: list[float]
    ) -> tuple[np.ndarray, np.ndarray]:
        n = min(len(prices), len(timestamps))
        if n < 3:
            return np.array([], dtype=float), np.array([], dtype=float)
        p = np.asarray(prices[:n], dtype=float)
        ts = np.asarray(timestamps[:n], dtype=float)
        if np.any(p <= 0):
            return np.array([], dtype=float), np.array([], dtype=float)
        logp = np.log(p)
        dlog = np.diff(logp)
        dt = np.diff(ts)
        valid = dt > 1e-6
        if not np.any(valid):
            return np.array([], dtype=float), np.array([], dtype=float)
        return dlog[valid], dt[valid]

    def _realized_variation_parts(
        self, prices: list[float], timestamps: list[float]
    ) -> tuple[float, float, float]:
        """
        Returns (rv, bv, jump_var):
        - rv: realized variance
        - bv: bipower variation (jump-robust continuous variance proxy)
        - jump_var: non-negative jump variation estimate (rv - bv)+
        """
        dlog, _dt = self._log_returns(prices, timestamps)
        if len(dlog) < 2:
            return 0.0, 0.0, 0.0

        rv = float(np.sum(dlog ** 2))
        abs_r = np.abs(dlog)
        bv = float((math.pi / 2.0) * np.sum(abs_r[1:] * abs_r[:-1]))
        # Numerical guard: bipower can exceed rv in finite samples.
        bv = max(0.0, min(bv, rv))
        jump_var = max(0.0, rv - bv)
        return rv, bv, jump_var

    def _compute_trend_strength(self, prices: list[float]) -> float:
        if len(prices) < 5:
            return 0.0
        y = np.asarray(prices, dtype=float)
        x = np.arange(len(y), dtype=float)
        x_mean = np.mean(x)
        y_mean = np.mean(y)
        denom = max(np.sum((x - x_mean) ** 2), 1e-10)
        slope = np.sum((x - x_mean) * (y - y_mean)) / denom
        return (slope / max(y_mean, 1e-12)) * 100.0

    def _recent_streak(self, results: list[str]) -> tuple[str, int]:
        if not results:
            return ("NONE", 0)
        current = results[-1]
        count = 0
        for r in reversed(results):
            if r == current:
                count += 1
            else:
                break
        return (current, count)

    def _estimate_prob_components(self, ctx: MarketContext) -> tuple[Optional[float], dict]:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20 or ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return (None, {})

        look_prices = ctx.recent_prices[-300:] if len(ctx.recent_prices) > 300 else ctx.recent_prices
        look_ts = ctx.recent_timestamps[-300:] if len(ctx.recent_timestamps) > 300 else ctx.recent_timestamps
        rv, bv, jump_var = self._realized_variation_parts(look_prices, look_ts)
        jump_ratio = jump_var / max(rv, 1e-12)

        elapsed = max(1.0, float(ctx.seconds_elapsed))
        sigma = math.sqrt(max(bv, 1e-12) / elapsed)
        if sigma <= 1e-9:
            _mu_alt, sigma_alt = _estimate_diffusion_params(ctx, min_samples=20)
            sigma = sigma_alt if sigma_alt is not None else 1e-6
        sigma = max(float(sigma), _SIGMA_FLOOR_PER_SQRT_SEC)

        rem = max(1.0, float(ctx.seconds_remaining))
        x = math.log(ctx.current_binance_price / ctx.market_start_price)
        denom = sigma * math.sqrt(rem)
        z = (x / denom) if denom > 1e-10 else (8.0 if x > 0 else -8.0 if x < 0 else 0.0)
        p_up = _clamp01(_norm_cdf(z))

        trend = self._compute_trend_strength(
            look_prices[-40:] if len(look_prices) > 40 else look_prices
        )
        streak_dir, streak_len = self._recent_streak(ctx.recent_results or [])

        trend_bias = _clamp(trend / 0.010, -1.0, 1.0) * 0.035
        p_up = _clamp01(p_up + trend_bias)

        jumpy = jump_ratio > 0.35
        if jumpy:
            shrink = 0.22 if rem > 120 else 0.10
            p_up = 0.5 + (p_up - 0.5) * (1.0 - shrink)
            if rem < 90 and abs(z) > 1.5:
                p_up = _clamp01(p_up + (0.03 if z > 0 else -0.03))

        if streak_len >= 5 and streak_dir in ("UP", "DOWN"):
            p_up = _clamp01(p_up - 0.02 if streak_dir == "UP" else p_up + 0.02)

        return (
            p_up,
            {
                "rv": rv,
                "bv": bv,
                "jump_ratio": jump_ratio,
                "z": z,
                "trend": trend,
                "jumpy": jumpy,
            },
        )

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        p_up, _meta = self._estimate_prob_components(ctx)
        return p_up

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20 or ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient statistical features", self.name)

        up_px, down_px = _get_ask_prices(ctx)
        if up_px is None or down_px is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        p_up, meta = self._estimate_prob_components(ctx)
        if p_up is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Unable to estimate close probability", self.name)
        rv = float(meta.get("rv", 0.0))
        bv = float(meta.get("bv", 0.0))
        jump_ratio = float(meta.get("jump_ratio", 0.0))
        z = float(meta.get("z", 0.0))
        trend = float(meta.get("trend", 0.0))
        jumpy = bool(meta.get("jumpy", False))

        min_edge = self.base_min_edge + (0.008 if jumpy else 0.0)
        vote, edge, up_edge, down_edge = _edge_vote(
            p_up,
            up_px,
            down_px,
            min_edge=min_edge,
            tie_margin=0.004,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={p_up:.3f}, no edge: up={up_edge:+.3f} down={down_edge:+.3f}",
                self.name,
            )

        quality = 0.80 if jumpy else 1.0
        confidence = _edge_confidence(
            p_up,
            edge,
            scale=5.0,
            certainty_boost=2.0,
            quality=quality,
        )
        reason = (
            f"p_up={p_up:.3f}, z={z:+.2f}, edge=({up_edge:+.3f}/{down_edge:+.3f}), "
            f"rv={rv:.2e}, bv={bv:.2e}, jump={jump_ratio:.2f}, trend={trend:+.4f}%"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 4: Multi-Horizon Trend Persistence -> close probability
# ---------------------------------------------------------------------------
class TrendPersistenceJudge:
    name = "TrendPersistenceJudge"

    def __init__(self, min_edge: float = 0.017):
        self.min_edge = max(0.0, float(min_edge))

    def _price_n_seconds_ago(self, ctx: MarketContext, seconds: float) -> Optional[float]:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n == 0:
            return None
        latest_ts = ctx.recent_timestamps[n - 1]
        target_ts = latest_ts - seconds
        for i in range(n - 1, -1, -1):
            if ctx.recent_timestamps[i] <= target_ts:
                return ctx.recent_prices[i]
        return ctx.recent_prices[0]

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20 or ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return None

        current = ctx.recent_prices[n - 1]
        p15 = self._price_n_seconds_ago(ctx, 15.0)
        p45 = self._price_n_seconds_ago(ctx, 45.0)
        p120 = self._price_n_seconds_ago(ctx, 120.0)
        if p15 is None or p45 is None or p120 is None:
            return None

        r15 = _safe_pct_change(current, p15)
        r45 = _safe_pct_change(current, p45)
        r120 = _safe_pct_change(current, p120)
        start_move = _safe_pct_change(ctx.current_binance_price, ctx.market_start_price)

        c15 = _clamp(r15 / 0.025, -1.0, 1.0)
        c45 = _clamp(r45 / 0.040, -1.0, 1.0)
        c120 = _clamp(r120 / 0.060, -1.0, 1.0)
        trend_score = 0.30 * c15 + 0.40 * c45 + 0.30 * c120

        if (r15 > 0 and r45 > 0) or (r15 < 0 and r45 < 0):
            trend_score += 0.10 if r15 > 0 else -0.10

        if ctx.seconds_remaining < 90 and abs(start_move) > 0.02:
            trend_score += 0.12 if start_move > 0 else -0.12

        trend_score = _clamp(trend_score, -1.0, 1.0)
        mu, sigma = _estimate_diffusion_params(ctx, min_samples=20)
        if mu is None or sigma is None:
            return _fallback_prob_from_move(ctx)

        mu_tilt = trend_score * sigma * 0.25
        return _close_prob_from_diffusion(ctx, mu + mu_tilt, sigma)

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20 or ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient trend lookback", self.name)

        up_px, down_px = _get_ask_prices(ctx)
        if up_px is None or down_px is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        current = ctx.recent_prices[n - 1]
        p15 = self._price_n_seconds_ago(ctx, 15.0)
        p45 = self._price_n_seconds_ago(ctx, 45.0)
        p120 = self._price_n_seconds_ago(ctx, 120.0)
        if p15 is None or p45 is None or p120 is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Missing timestamp alignment", self.name)

        r15 = _safe_pct_change(current, p15)
        r45 = _safe_pct_change(current, p45)
        r120 = _safe_pct_change(current, p120)
        start_move = _safe_pct_change(ctx.current_binance_price, ctx.market_start_price)

        c15 = _clamp(r15 / 0.025, -1.0, 1.0)
        c45 = _clamp(r45 / 0.040, -1.0, 1.0)
        c120 = _clamp(r120 / 0.060, -1.0, 1.0)
        trend_score = 0.30 * c15 + 0.40 * c45 + 0.30 * c120

        if (r15 > 0 and r45 > 0) or (r15 < 0 and r45 < 0):
            trend_score += 0.10 if r15 > 0 else -0.10

        if ctx.seconds_remaining < 90 and abs(start_move) > 0.02:
            trend_score += 0.12 if start_move > 0 else -0.12

        trend_score = _clamp(trend_score, -1.0, 1.0)
        p_up = self.estimate_prob(ctx)
        if p_up is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Unable to estimate close probability", self.name)

        vote, edge, up_edge, down_edge = _edge_vote(
            p_up,
            up_px,
            down_px,
            min_edge=self.min_edge,
            tie_margin=0.003,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={p_up:.3f}, no edge: up={up_edge:+.3f} down={down_edge:+.3f}",
                self.name,
            )

        confidence = _edge_confidence(p_up, edge, scale=4.8, certainty_boost=1.7)
        reason = (
            f"p_up={p_up:.3f}, edge=({up_edge:+.3f}/{down_edge:+.3f}), "
            f"r15={r15:+.4f}% r45={r45:+.4f}% r120={r120:+.4f}% trend={trend_score:+.2f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 5: Orderbook Value / Quality -> close probability
# ---------------------------------------------------------------------------
class OrderbookValueJudge:
    name = "OrderbookValueJudge"

    def __init__(
        self,
        min_entry_edge: float = 0.02,
        max_spread: float = 0.08,
        max_overround: float = 0.12,
    ):
        self.min_entry_edge = max(0.0, float(min_entry_edge))
        self.max_spread = max(0.0, float(max_spread))
        self.max_overround = max(0.0, float(max_overround))

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        up_bid = ctx.poly_up_bid if ctx.poly_up_bid is not None else max(ctx.poly_up_price - 0.01, 0.0)
        up_ask = ctx.poly_up_ask if ctx.poly_up_ask is not None else min(ctx.poly_up_price + 0.01, 1.0)
        down_bid = ctx.poly_down_bid if ctx.poly_down_bid is not None else max(ctx.poly_down_price - 0.01, 0.0)
        down_ask = ctx.poly_down_ask if ctx.poly_down_ask is not None else min(ctx.poly_down_price + 0.01, 1.0)
        if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):
            return None

        up_spread = max(0.0, up_ask - up_bid)
        down_spread = max(0.0, down_ask - down_bid)
        overround = (up_ask + down_ask) - 1.0
        if up_spread > self.max_spread or down_spread > self.max_spread:
            return None
        if overround > self.max_overround:
            return None

        mu, sigma = _estimate_diffusion_params(ctx, min_samples=20)
        if mu is None or sigma is None:
            p_model = _fallback_prob_from_move(ctx)
        else:
            p_model = _close_prob_from_diffusion(ctx, mu, sigma)

        p_ask = up_ask / max(1e-9, (up_ask + down_ask))
        p_bid = up_bid / max(1e-9, (up_bid + down_bid)) if (up_bid + down_bid) > 1e-9 else p_ask
        progress = _clamp01(ctx.seconds_elapsed / 300.0)
        w_model = 0.60 + 0.20 * progress
        w_mkt = 1.0 - w_model
        p_up = _clamp01(w_model * p_model + w_mkt * (0.65 * p_ask + 0.35 * p_bid))

        spread_asym = (down_spread - up_spread) / max(1e-6, up_spread + down_spread)
        p_up = _clamp01(p_up + 0.03 * _clamp(spread_asym, -1.0, 1.0))

        quality = 1.0
        quality -= min(0.6, (up_spread + down_spread) * 4.0)
        quality -= max(0.0, overround) * 1.5
        quality = _clamp(quality, 0.25, 1.0)
        p_up = 0.5 + (p_up - 0.5) * quality
        return _clamp01(p_up)

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        up_bid = ctx.poly_up_bid if ctx.poly_up_bid is not None else max(ctx.poly_up_price - 0.01, 0.0)
        up_ask = ctx.poly_up_ask if ctx.poly_up_ask is not None else min(ctx.poly_up_price + 0.01, 1.0)
        down_bid = ctx.poly_down_bid if ctx.poly_down_bid is not None else max(ctx.poly_down_price - 0.01, 0.0)
        down_ask = ctx.poly_down_ask if ctx.poly_down_ask is not None else min(ctx.poly_down_price + 0.01, 1.0)

        if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid ask prices", self.name)

        up_spread = max(0.0, up_ask - up_bid)
        down_spread = max(0.0, down_ask - down_bid)
        overround = (up_ask + down_ask) - 1.0

        if up_spread > self.max_spread or down_spread > self.max_spread:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"Wide spread: up={up_spread:.3f} down={down_spread:.3f}",
                self.name,
            )
        if overround > self.max_overround:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"Expensive book overround={overround:.3f}",
                self.name,
            )

        p_up = self.estimate_prob(ctx)
        if p_up is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Unable to estimate close probability", self.name)

        quality = 1.0
        quality -= min(0.6, (up_spread + down_spread) * 4.0)
        quality -= max(0.0, overround) * 1.5
        quality = _clamp(quality, 0.25, 1.0)

        min_edge = self.min_entry_edge * (1.0 + (1.0 - quality) * 0.8)
        vote, edge, up_edge, down_edge = _edge_vote(
            p_up,
            up_ask,
            down_ask,
            min_edge=min_edge,
            tie_margin=0.010,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={p_up:.3f}, no ask edge: up={up_edge:+.3f} down={down_edge:+.3f}",
                self.name,
            )

        confidence = _edge_confidence(
            p_up,
            edge,
            scale=5.6,
            certainty_boost=1.6,
            quality=quality,
        )
        reason = (
            f"p_up={p_up:.3f}, edge={edge:+.3f}, ask=({up_ask:.3f}/{down_ask:.3f}), "
            f"spread=({up_spread:.3f}/{down_spread:.3f}), overround={overround:.3f}, q={quality:.2f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


def estimate_ensemble_close_probability(
    ctx: MarketContext,
) -> tuple[float, dict[str, float], float]:
    """
    Returns:
    - p_up_ensemble
    - per-judge p_up map
    - dispersion (std dev across judge probabilities)
    """
    estimators = [
        TechnicalJudge(),
        ArbitrageJudge(),
        StatisticalJudge(),
        TrendPersistenceJudge(),
        OrderbookValueJudge(),
    ]
    probs: dict[str, float] = {}
    vals: list[float] = []
    for est in estimators:
        p = est.estimate_prob(ctx)
        if p is None:
            continue
        p = _clamp01(float(p))
        probs[est.name] = p
        vals.append(p)

    if not vals:
        p_fallback = _fallback_prob_from_move(ctx)
        return (_clamp01(p_fallback), {"Fallback": _clamp01(p_fallback)}, 0.5)

    arr = np.asarray(vals, dtype=float)
    arr_sorted = np.sort(arr)
    if len(arr_sorted) >= 4:
        core = float(np.mean(arr_sorted[1:-1]))  # trimmed mean
    else:
        core = float(np.mean(arr_sorted))

    dispersion = float(np.std(arr))
    # Robustness-inspired calibration:
    # higher model disagreement -> shrink toward 0.5.
    reliability = _clamp(1.0 - 1.5 * dispersion, 0.35, 1.0)
    p_up = _clamp01(0.5 + (core - 0.5) * reliability)
    return (p_up, probs, dispersion)


# ---------------------------------------------------------------------------
# Jury System
# ---------------------------------------------------------------------------
@dataclass
class JuryDecision:
    final_vote: Vote
    direction: str  # "UP", "DOWN", or "NO_TRADE"
    avg_confidence: float
    max_edge: float
    verdicts: list[JudgeVerdict]
    unanimous: bool


class Jury:
    """
    Aggregates verdicts from all judges.
    Requires `threshold` same-direction votes and a confidence-score margin.
    """

    def __init__(self, threshold: Optional[int] = None, min_score_margin: float = 0.08):
        self.judges = [
            TechnicalJudge(),
            ArbitrageJudge(),
            StatisticalJudge(),
            TrendPersistenceJudge(),
            OrderbookValueJudge(),
        ]
        jury_size = len(self.judges)
        if threshold is None:
            threshold = max(2, (jury_size // 2) + 1)
        self.threshold = max(1, min(int(threshold), jury_size))
        self.min_score_margin = max(0.0, float(min_score_margin))

    def _price_n_seconds_ago(self, ctx: MarketContext, seconds: float) -> Optional[float]:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n == 0:
            return None
        latest_ts = float(ctx.recent_timestamps[n - 1])
        target_ts = latest_ts - float(max(1.0, seconds))
        for i in range(n - 1, -1, -1):
            if float(ctx.recent_timestamps[i]) <= target_ts:
                return float(ctx.recent_prices[i])
        return float(ctx.recent_prices[0])

    def _recent_move(self, ctx: MarketContext, seconds: float) -> float:
        prev = self._price_n_seconds_ago(ctx, seconds)
        if prev is None or prev <= 0.0:
            return 0.0
        return _safe_pct_change(float(ctx.current_binance_price), float(prev))

    def _judge_base_weights(self, ctx: MarketContext) -> dict[str, float]:
        weights = {j.name: 1.0 for j in self.judges}
        progress = _clamp01(float(ctx.seconds_elapsed) / 300.0)
        up_ask = ctx.poly_up_ask if ctx.poly_up_ask is not None else ctx.poly_up_price
        down_ask = ctx.poly_down_ask if ctx.poly_down_ask is not None else ctx.poly_down_price
        up_bid = ctx.poly_up_bid if ctx.poly_up_bid is not None else max(ctx.poly_up_price - 0.01, 0.0)
        down_bid = ctx.poly_down_bid if ctx.poly_down_bid is not None else max(ctx.poly_down_price - 0.01, 0.0)

        spread_wide = False
        if 0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0:
            up_spread = max(0.0, float(up_ask) - float(up_bid))
            down_spread = max(0.0, float(down_ask) - float(down_bid))
            overround = (float(up_ask) + float(down_ask)) - 1.0
            spread_wide = (up_spread > 0.055) or (down_spread > 0.055) or (overround > 0.10)

        move15 = abs(self._recent_move(ctx, 15.0))
        move45 = abs(self._recent_move(ctx, 45.0))
        whipsaw = (move15 > 0.02 and move45 < 0.01) or (move45 > 0.04 and move15 < 0.008)

        # Regime-aware weighting:
        # - Early window: reduce trend persistence influence (too noisy).
        # - Late window: boost trend/statistical persistence.
        # - Whipsaw: dampen pure trend/technical sensitivity.
        # - Wide spreads/overround: downweight orderbook-derived confidence.
        if progress < 0.25:
            weights["TrendPersistenceJudge"] *= 0.88
            weights["TechnicalJudge"] *= 0.94
        elif progress > 0.65:
            weights["TrendPersistenceJudge"] *= 1.10
            weights["StatisticalJudge"] *= 1.08

        if whipsaw:
            weights["TrendPersistenceJudge"] *= 0.84
            weights["TechnicalJudge"] *= 0.88
            weights["StatisticalJudge"] *= 1.06

        if spread_wide:
            weights["OrderbookValueJudge"] *= 0.82

        for k, v in list(weights.items()):
            weights[k] = _clamp(v, 0.70, 1.20)
        return weights

    def _directional_alignment_multiplier(
        self,
        vote: Vote,
        move15: float,
        move45: float,
    ) -> float:
        if vote == Vote.ABSTAIN:
            return 1.0

        up_align = (move15 >= 0.0) + (move45 >= 0.0)
        down_align = (move15 <= 0.0) + (move45 <= 0.0)
        aligns = up_align if vote == Vote.UP else down_align

        if aligns == 2:
            return 1.08
        if aligns == 1:
            return 0.96
        return 0.80

    @property
    def size(self) -> int:
        return len(self.judges)

    @property
    def judge_names(self) -> list[str]:
        return [j.name for j in self.judges]

    def deliberate(self, ctx: MarketContext) -> JuryDecision:
        verdicts = [j.judge(ctx) for j in self.judges]
        up_votes = [v for v in verdicts if v.vote == Vote.UP]
        down_votes = [v for v in verdicts if v.vote == Vote.DOWN]

        n_up = len(up_votes)
        n_down = len(down_votes)
        base_weights = self._judge_base_weights(ctx)
        move15 = self._recent_move(ctx, 15.0)
        move45 = self._recent_move(ctx, 45.0)

        weighted_conf: dict[str, float] = {}
        for v in verdicts:
            w_base = base_weights.get(v.judge_name, 1.0)
            w_align = self._directional_alignment_multiplier(v.vote, move15, move45)
            weighted_conf[v.judge_name] = _clamp01(v.confidence * w_base * w_align)

        up_score = sum(weighted_conf.get(v.judge_name, v.confidence) for v in up_votes)
        down_score = sum(weighted_conf.get(v.judge_name, v.confidence) for v in down_votes)

        if n_up >= self.threshold and up_score >= down_score + self.min_score_margin:
            direction = "UP"
            final_vote = Vote.UP
            winning = up_votes
        elif n_down >= self.threshold and down_score >= up_score + self.min_score_margin:
            direction = "DOWN"
            final_vote = Vote.DOWN
            winning = down_votes
        else:
            direction = "NO_TRADE"
            final_vote = Vote.ABSTAIN
            winning = []

        avg_confidence = (
            sum(weighted_conf.get(v.judge_name, v.confidence) for v in winning) / len(winning)
            if winning
            else 0.0
        )
        max_confidence = max((weighted_conf.get(v.judge_name, v.confidence) for v in winning), default=0.0)
        unanimous = (n_up == self.size and self.size > 0) or (n_down == self.size and self.size > 0)

        for v in verdicts:
            w = base_weights.get(v.judge_name, 1.0)
            wc = weighted_conf.get(v.judge_name, v.confidence)
            logger.info(
                "  [%s] %s (conf=%.3f -> w=%.2f => %.3f): %s",
                v.judge_name,
                v.vote.value,
                v.confidence,
                w,
                wc,
                v.reason,
            )
        logger.info(
            "  JURY: %s | votes UP=%d DOWN=%d ABSTAIN=%d | score UP=%.3f DOWN=%.3f | "
            "avg_conf=%.3f | move15=%+.4f%% move45=%+.4f%%",
            direction,
            n_up,
            n_down,
            self.size - n_up - n_down,
            up_score,
            down_score,
            avg_confidence,
            move15,
            move45,
        )

        return JuryDecision(
            final_vote=final_vote,
            direction=direction,
            avg_confidence=avg_confidence,
            max_edge=max_confidence,
            verdicts=verdicts,
            unanimous=unanimous,
        )
