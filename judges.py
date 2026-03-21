"""
Probability-first judges for BTC Up/Down 5-minute markets.

Each judge estimates close-time probability p_up = P(S_close >= S_start | info_t),
then compares p_up against executable ask prices (UP/DOWN).

Three genuinely independent judges:
  1. StatisticalJudge  - physics-based diffusion + jump detection + trend
  2. ArbitrageJudge    - Binance-Polymarket lag freshness detector
  3. OrderbookValueJudge - execution quality + spread-adjusted edge
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
    # Buy/sell volume ratio (last 60s): >1 = buy pressure, <1 = sell pressure
    buy_sell_ratio: Optional[float] = None


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
    up_px = float(up_px)
    down_px = float(down_px)
    # Thin CLOB can show extreme asks (e.g. UP=0.93, DOWN=0.08) where
    # up+down deviates wildly from 1.0.  This creates phantom "edge" that
    # judges chase.  When overround is extreme, normalize to sum=1.0 so
    # edge calculations compare model prob against fair implied prob.
    total = up_px + down_px
    if total > 0 and abs(total - 1.0) > 0.25:
        up_px = up_px / total
        down_px = down_px / total
    return (up_px, down_px)


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
    raw_p = _clamp01(_norm_cdf(z))
    # Cautious calibration: shrink toward 0.5 (more at start, less near expiry)
    progress = _clamp01(1.0 - t / 300.0)
    shrinkage = 0.75 + 0.17 * progress
    p = _clamp01(0.5 + shrinkage * (raw_p - 0.5))
    # Long-term trend handled by MomentumJudge (separate independent signal)
    return p


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


def _price_at_seconds_ago(
    prices: list[float],
    ts: list[float],
    seconds: float,
) -> float | None:
    """Get price at approximately `seconds` ago from the latest timestamp."""
    n = min(len(prices), len(ts))
    if n == 0:
        return None
    latest_ts = float(ts[n - 1])
    target = latest_ts - max(1.0, float(seconds))
    for i in range(n - 1, -1, -1):
        if float(ts[i]) <= target:
            return float(prices[i])
    return float(prices[0])


# ---------------------------------------------------------------------------
# Judge 1: Statistical / Physics-based (diffusion + jump detection + trend)
# ---------------------------------------------------------------------------
class StatisticalJudge:
    """
    Physics-based probability from bipower variation + jump detection.

    Genuinely independent info: uses realized/bipower variance decomposition
    to detect jump regimes, and trend strength to adjust drift.
    """
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
        dlog, _dt = self._log_returns(prices, timestamps)
        if len(dlog) < 2:
            return 0.0, 0.0, 0.0

        rv = float(np.sum(dlog ** 2))
        abs_r = np.abs(dlog)
        bv = float((math.pi / 2.0) * np.sum(abs_r[1:] * abs_r[:-1]))
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

        # Short-term trend (40 ticks ~ 40s) — within-window micro-trend
        trend_short = self._compute_trend_strength(
            look_prices[-40:] if len(look_prices) > 40 else look_prices
        )
        # Long-term trend (300 ticks ~ 5min) — cross-window macro-trend
        trend_long = self._compute_trend_strength(
            look_prices[-300:] if len(look_prices) > 300 else look_prices
        )
        streak_dir, streak_len = self._recent_streak(ctx.recent_results or [])

        # Short-term trend bias only (long-term handled by MomentumJudge)
        trend_bias = _clamp(trend_short / 0.010, -1.0, 1.0) * 0.035
        p_up = _clamp01(p_up + trend_bias)

        jumpy = jump_ratio > 0.35
        if jumpy:
            shrink = 0.22 if rem > 120 else 0.10
            p_up = 0.5 + (p_up - 0.5) * (1.0 - shrink)
            if rem < 90 and abs(z) > 1.5:
                p_up = _clamp01(p_up + (0.03 if z > 0 else -0.03))

        if streak_len >= 5 and streak_dir in ("UP", "DOWN"):
            p_up = _clamp01(p_up - 0.02 if streak_dir == "UP" else p_up + 0.02)

        # Buy/sell volume ratio: >1 = buyers aggressive = UP bias
        # Disabled for now — needs more data to validate. Backtest showed
        # negative impact (-0.45 PF) with ±0.03 bias. Keep collecting data.
        # if ctx.buy_sell_ratio is not None and ctx.buy_sell_ratio > 0:
        #     _vol_signal = _clamp(math.log(ctx.buy_sell_ratio) / 0.7, -1.0, 1.0)
        #     p_up = _clamp01(p_up + _vol_signal * 0.03)

        return (
            p_up,
            {
                "rv": rv,
                "bv": bv,
                "jump_ratio": jump_ratio,
                "z": z,
                "trend": trend_short,
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
# Judge 2: Arbitrage / Lag Freshness Detector
# ---------------------------------------------------------------------------
class ArbitrageJudge:
    """
    Detects FRESH Binance-Polymarket price lag --- the ONLY real edge source.

    Key difference from StatisticalJudge: measures HOW FRESH the lag is.
    A BTC move in the last 5-10 seconds that Polymarket hasn't priced in yet
    is a strong, exploitable signal.  A move from 30+ seconds ago is likely
    already reflected in the odds.

    Inspired by the RL article insight: the agent learned that *timing*
    matters more than signal magnitude.  We encode this directly.
    """
    name = "ArbitrageJudge"

    def __init__(self, min_edge: float = 0.020, min_samples: int = 24):
        self.min_edge = max(0.0, float(min_edge))
        self.min_samples = max(10, int(min_samples))

    def _lag_freshness(self, ctx: MarketContext) -> tuple[float, float, float]:
        """
        Measure how fresh the current Binance-Poly lag is.

        Returns:
          freshness: 0.0 (stale/no move) to 1.0 (very recent move)
          move_5s:   absolute price change in last 5 seconds (% of start)
          move_30s:  absolute price change in last 30 seconds (% of start)
        """
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 15:
            return (0.5, 0.0, 0.0)

        prices = ctx.recent_prices[:n]
        ts = ctx.recent_timestamps[:n]

        current = float(ctx.current_binance_price)
        start = float(ctx.market_start_price)
        if start <= 0 or current <= 0:
            return (0.5, 0.0, 0.0)

        p5 = _price_at_seconds_ago(prices, ts, 5.0)
        p10 = _price_at_seconds_ago(prices, ts, 10.0)
        p30 = _price_at_seconds_ago(prices, ts, 30.0)

        move_5s = abs(current - p5) / start if p5 and p5 > 0 else 0.0
        move_10s = abs(current - p10) / start if p10 and p10 > 0 else 0.0
        move_30s = abs(current - p30) / start if p30 and p30 > 0 else 0.0

        if move_30s < 1e-8:
            return (0.5, move_5s, move_30s)  # no significant move

        # What fraction of the 30s move happened in the last 5-10s?
        # High ratio = move is fresh = Polymarket likely hasn't caught up
        recency_ratio = max(move_5s, move_10s * 0.8) / max(move_30s, 1e-8)
        freshness = _clamp(recency_ratio * 1.4, 0.0, 1.0)

        return (freshness, move_5s, move_30s)

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        """Pure diffusion probability - no market blending."""
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

        freshness, move_5s, move_30s = self._lag_freshness(ctx)

        # HARD GATE: if the lag is stale, there is NO arbitrage opportunity.
        # The entire strategy premise is exploiting fresh Binance→Poly lag.
        # A stale lag means Polymarket already priced it in → no edge.
        # Raised from 0.20 → 0.35: only trade on genuinely fresh lag.
        # ArbitrageJudge is most accurate at 68.2% — tighter freshness
        # filters stale signals where Polymarket already caught up.
        min_freshness = 0.35
        min_30s_move = 0.00008  # need at least 0.008% move in 30s
        if freshness < min_freshness or move_30s < min_30s_move:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"stale lag: fresh={freshness:.2f}<{min_freshness}, "
                f"move_30s={move_30s:.5f} (min={min_30s_move:.5f})",
                self.name,
            )

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
                f"p_up={fair_prob_up:.3f}, no edge: up={up_edge:+.3f}, down={down_edge:+.3f}, "
                f"fresh={freshness:.2f}",
                self.name,
            )

        # Fresh lag -> higher confidence, stale lag -> lower confidence
        # freshness=1.0 -> mult=1.30 (boost), freshness=0.0 -> mult=0.70 (dampen)
        freshness_mult = 0.70 + 0.60 * freshness
        base_confidence = _edge_confidence(fair_prob_up, edge, scale=5.2, certainty_boost=1.8)
        confidence = _clamp01(base_confidence * freshness_mult)

        market_prob_up = up_px / max(1e-9, (up_px + down_px))
        reason = (
            f"p_up={fair_prob_up:.3f}, mkt_p_up={market_prob_up:.3f}, edge={edge:+.3f}, "
            f"fresh={freshness:.2f}, move_5s={move_5s:.5f}, move_30s={move_30s:.5f}, "
            f"asks=({up_px:.3f}/{down_px:.3f})"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 3: Orderbook Execution Quality + Spread-Adjusted Edge
# ---------------------------------------------------------------------------
class OrderbookValueJudge:
    """
    Evaluates whether a trade is executable at a profit given current spreads.

    Key difference from other judges: factors in the COST of trading.
    Even if the diffusion model shows edge, wide spreads eat profits.
    From the RL article: spread cost penalty (-0.05 * c_spread) was one of
    the most important reward components.

    Does NOT blend Polymarket price into probability estimate (that was
    reducing our detected edge by pulling toward the price we're trying to beat).
    """
    name = "OrderbookValueJudge"

    def __init__(
        self,
        min_entry_edge: float = 0.020,
        max_spread: float = 0.08,
        max_overround: float = 0.12,
    ):
        self.min_entry_edge = max(0.0, float(min_entry_edge))
        self.max_spread = max(0.0, float(max_spread))
        self.max_overround = max(0.0, float(max_overround))

    def estimate_prob(self, ctx: MarketContext) -> Optional[float]:
        """Pure diffusion model - no market price blending."""
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

        # Pure diffusion probability - the market price is what we're BETTING
        # AGAINST, not what we should blend into our estimate.
        mu, sigma = _estimate_diffusion_params(ctx, min_samples=20)
        if mu is None or sigma is None:
            p_model = _fallback_prob_from_move(ctx)
        else:
            p_model = _close_prob_from_diffusion(ctx, mu, sigma)

        # Spread asymmetry: if DOWN spread is wider than UP spread,
        # that hints at informed selling pressure on DOWN side (bullish signal).
        # This is genuine orderbook microstructure info, independent of price.
        spread_asym = (down_spread - up_spread) / max(1e-6, up_spread + down_spread)
        p_model = _clamp01(p_model + 0.02 * _clamp(spread_asym, -1.0, 1.0))

        # Execution quality: wide spreads = higher uncertainty = shrink toward 0.5
        quality = 1.0
        quality -= min(0.6, (up_spread + down_spread) * 4.0)
        quality -= max(0.0, overround) * 1.5
        quality = _clamp(quality, 0.25, 1.0)
        p_up = 0.5 + (p_model - 0.5) * quality

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

        # Execution quality score
        quality = 1.0
        quality -= min(0.6, (up_spread + down_spread) * 4.0)
        quality -= max(0.0, overround) * 1.5
        quality = _clamp(quality, 0.25, 1.0)

        # SPREAD-AWARE EDGE: need bigger edge when spreads are wide
        # This is the key insight from the RL article - spread cost eats edge.
        # If we need to early-exit, we pay the spread again, so factor it in.
        effective_spread = (up_spread + down_spread) / 2.0
        spread_adjusted_min_edge = self.min_entry_edge + effective_spread * 0.5
        # Also raise bar when quality is poor
        spread_adjusted_min_edge *= (1.0 + (1.0 - quality) * 0.8)

        vote, edge, up_edge, down_edge = _edge_vote(
            p_up,
            up_ask,
            down_ask,
            min_edge=spread_adjusted_min_edge,
            tie_margin=0.010,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"p_up={p_up:.3f}, no ask edge: up={up_edge:+.3f} down={down_edge:+.3f} "
                f"(need>={spread_adjusted_min_edge:.3f})",
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
            f"spread=({up_spread:.3f}/{down_spread:.3f}), overround={overround:.3f}, "
            f"q={quality:.2f}, min_e={spread_adjusted_min_edge:.3f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 4: Momentum / Trend Persistence
# ---------------------------------------------------------------------------
class MomentumJudge:
    """
    Predicts direction based on multi-timeframe price momentum.

    Key insight: BTC has short-term momentum persistence. If price has been
    rising for the last 60s, it's more likely to continue than reverse.
    This is the missing signal — other judges look at position (where price IS)
    but not direction (where price is GOING).

    Features:
    - Multi-timeframe momentum (15s, 60s, 180s)
    - EMA crossover (fast EMA vs slow EMA)
    - Momentum acceleration (is trend strengthening or weakening?)
    - Position-adjusted: momentum toward start price = stronger signal
    """
    name = "MomentumJudge"

    def __init__(self, min_edge: float = 0.020):
        self.min_edge = max(0.0, float(min_edge))

    def _compute_momentum(
        self, prices: list[float], timestamps: list[float], lookback_sec: float,
    ) -> float | None:
        """Price change % over last lookback_sec seconds."""
        n = min(len(prices), len(timestamps))
        if n < 5:
            return None
        latest_ts = float(timestamps[n - 1])
        target_ts = latest_ts - lookback_sec
        p_old = None
        for i in range(n - 1, -1, -1):
            if float(timestamps[i]) <= target_ts:
                p_old = float(prices[i])
                break
        if p_old is None:
            p_old = float(prices[0])
        if p_old <= 0:
            return None
        p_now = float(prices[n - 1])
        return ((p_now - p_old) / p_old) * 100.0

    def _compute_ema(self, prices: list[float], span: int) -> float | None:
        """Exponential moving average of last `span` prices."""
        if len(prices) < span:
            return None
        alpha = 2.0 / (span + 1)
        ema = float(prices[-span])
        for p in prices[-span + 1:]:
            ema = alpha * float(p) + (1 - alpha) * ema
        return ema

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 60:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient momentum data", self.name)

        prices = ctx.recent_prices[:n]
        timestamps = ctx.recent_timestamps[:n]

        # Multi-timeframe momentum
        mom_15 = self._compute_momentum(prices, timestamps, 15.0)
        mom_60 = self._compute_momentum(prices, timestamps, 60.0)
        mom_180 = self._compute_momentum(prices, timestamps, 180.0)

        if mom_15 is None or mom_60 is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Cannot compute momentum", self.name)

        # EMA crossover: fast(10) vs slow(50)
        ema_fast = self._compute_ema(prices, 10)
        ema_slow = self._compute_ema(prices, 50)
        ema_signal = 0.0
        if ema_fast is not None and ema_slow is not None and ema_slow > 0:
            ema_signal = (ema_fast - ema_slow) / ema_slow * 100.0  # % difference

        # Momentum acceleration: is 15s momentum aligned with 60s?
        # Same direction = trend strengthening
        acceleration = 1.0
        if mom_60 != 0 and mom_15 != 0:
            if (mom_15 > 0) == (mom_60 > 0):
                acceleration = 1.3  # aligned = stronger
            else:
                acceleration = 0.5  # divergent = weakening

        # Key insight from data analysis:
        # - Pure momentum (follow trend) = 38% accuracy in 5min BTC
        # - Pure position (where is price vs start) = already done by StatisticalJudge
        # - Unique value of MomentumJudge: VELOCITY + ACCELERATION
        #   Speed of recent move predicts continuation vs reversal.
        #   Fast moves tend to continue. Slow drift tends to reverse.
        mom_180_safe = mom_180 if mom_180 is not None else 0.0

        # Velocity: how fast is price moving RIGHT NOW (15s) vs recently (60s)?
        # If 15s momentum > 60s momentum → accelerating → likely continues
        # If 15s momentum < 60s momentum → decelerating → may reverse
        if abs(mom_60) > 0.001:
            velocity_ratio = mom_15 / mom_60  # >1 = accelerating, <1 = decelerating
        else:
            velocity_ratio = 0.0

        # Direction: use 60s momentum as primary direction indicator
        # (15s too noisy, 180s too slow for 5-min window)
        direction_signal = _clamp(mom_60 / 0.04, -1.0, 1.0)

        # Acceleration boost: accelerating trend = higher confidence
        accel_factor = 1.0
        if velocity_ratio > 1.2:
            accel_factor = 1.4  # accelerating strongly
        elif velocity_ratio > 0.8:
            accel_factor = 1.0  # steady
        elif velocity_ratio > 0.0:
            accel_factor = 0.6  # decelerating
        else:
            accel_factor = 0.3  # reversing

        # EMA confirmation: when EMA agrees with direction, boost
        ema_confirms = (ema_signal > 0 and direction_signal > 0) or (ema_signal < 0 and direction_signal < 0)
        ema_boost = 1.2 if ema_confirms else 0.8

        # Time factor: later in window = momentum more reliable
        # (more data, trend more established)
        time_factor = 0.7 + 0.6 * _clamp01(ctx.seconds_elapsed / 240.0)

        composite = direction_signal * accel_factor * ema_boost * time_factor

        p_up = _clamp01(0.5 + composite * 0.15)

        up_px, down_px = _get_ask_prices(ctx)
        if up_px is None or down_px is None:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        vote, edge, up_edge, down_edge = _edge_vote(
            p_up, up_px, down_px, min_edge=self.min_edge, tie_margin=0.003,
        )
        if vote == Vote.ABSTAIN:
            return JudgeVerdict(
                Vote.ABSTAIN, 0.0,
                f"p_up={p_up:.3f}, no edge: up={up_edge:+.3f} down={down_edge:+.3f}, "
                f"mom15={mom_15:+.4f}% mom60={mom_60:+.4f}% ema={ema_signal:+.4f}%",
                self.name,
            )

        confidence = _edge_confidence(p_up, edge, scale=4.5, certainty_boost=1.5)
        # Boost confidence when acceleration is aligned
        confidence = _clamp01(confidence * (0.8 + 0.4 * min(acceleration, 1.5)))

        reason = (
            f"p_up={p_up:.3f}, edge={edge:+.3f}, "
            f"mom15={mom_15:+.4f}% mom60={mom_60:+.4f}% mom180={mom_180_safe:+.4f}% "
            f"ema={ema_signal:+.4f}% accel={acceleration:.1f} composite={composite:+.3f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Ensemble Close Probability (physics-first, used by trade_gate.py)
# ---------------------------------------------------------------------------
def estimate_ensemble_close_probability(
    ctx: MarketContext,
) -> tuple[float, dict[str, float], float]:
    """
    Physics-first probability estimation for 5-minute binary markets.

    The ONLY real edge in this market is the lag between Binance price and
    Polymarket odds.  This function computes the *true* probability using
    the diffusion model with proper time weighting:

        P(UP) = N( x / (sigma * sqrt(remaining)) )

    where x = ln(current_price / start_price).

    Key insight: the SAME BTC price gives VERY different probabilities
    depending on time remaining:
      - 240s left, +0.14% move  =>  P(UP) ~ 0.58  (still uncertain)
      - 60s  left, +0.14% move  =>  P(UP) ~ 0.78  (nearly locked in)

    Secondary signals (momentum quality, jump detection) provide small
    adjustments.  Polymarket implied probability is NOT blended into our
    estimate --- it IS the thing we're trying to beat.

    Returns:
    - p_up_ensemble
    - per-source p_up map (for logging/diagnostics)
    - dispersion (|our_estimate - poly_implied|, higher = more edge OR more risk)
    """
    if ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
        return (0.5, {"Fallback": 0.5}, 0.5)

    probs: dict[str, float] = {}

    # -- 1. Core: diffusion probability with time weighting --
    mu, sigma = _estimate_diffusion_params(ctx, min_samples=16)
    if sigma is None or sigma <= 1e-10:
        sigma = _SIGMA_FLOOR_PER_SQRT_SEC
        mu = 0.0

    sigma = max(float(sigma), _SIGMA_FLOOR_PER_SQRT_SEC)
    remaining = max(1.0, float(ctx.seconds_remaining))
    x = math.log(ctx.current_binance_price / ctx.market_start_price)

    # Pure GBM close probability: N(x / (sigma * sqrt(T)))
    drift_decay = math.exp(-1.2 * (remaining / 300.0))
    raw_drift = (mu - 0.5 * sigma * sigma) * remaining
    drift = _clamp(raw_drift * drift_decay, -0.0015, 0.0015)

    denom = sigma * math.sqrt(remaining)
    if denom < 1e-8:
        p_diffusion = 1.0 if (x + drift) > 0 else 0.0
    else:
        z = _clamp((x + drift) / denom, -8.0, 8.0)
        p_diffusion = _clamp01(_norm_cdf(z))

    probs["Diffusion"] = p_diffusion

    # -- 2. Momentum quality: is the move accelerating or fading? --
    momentum_adj = 0.0
    n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
    if n >= 20:
        prices = ctx.recent_prices[:n]
        ts = ctx.recent_timestamps[:n]
        latest_ts = float(ts[-1])

        recent_10s_idx = None
        for i in range(n - 1, -1, -1):
            if float(ts[i]) <= latest_ts - 10.0:
                recent_10s_idx = i
                break
        if recent_10s_idx is not None and float(prices[recent_10s_idx]) > 0:
            recent_move = (float(prices[-1]) - float(prices[recent_10s_idx])) / float(prices[recent_10s_idx])
            overall_move = x
            if overall_move != 0.0:
                alignment = _clamp(recent_move / max(abs(overall_move), 1e-9), -2.0, 2.0)
                momentum_adj = _clamp(alignment * 0.015, -0.02, 0.02)

    probs["MomentumAdj"] = momentum_adj

    # -- 3. Jump detection: large sudden moves are less reliable --
    jump_shrink = 0.0
    if n >= 20:
        prices_arr = np.asarray(ctx.recent_prices[:n], dtype=float)
        log_ret = np.diff(np.log(np.maximum(prices_arr, 1e-10)))
        if len(log_ret) >= 10:
            rv = float(np.sum(log_ret ** 2))
            abs_ret = np.abs(log_ret)
            bv = (math.pi / 2.0) * float(np.sum(abs_ret[1:] * abs_ret[:-1]))
            bv = max(0.0, min(bv, rv))
            if rv > 1e-14:
                jump_ratio = _clamp((rv - bv) / rv, 0.0, 1.0)
                if jump_ratio > 0.35:
                    jump_shrink = _clamp((jump_ratio - 0.35) * 0.15, 0.0, 0.06)

    # -- 4. Final probability with Cautious Calibration --
    # (arXiv:2408.05120) Shrink toward 0.5 to prevent overconfident entries.
    # calibrated = 0.5 + shrinkage × (raw - 0.5)
    # shrinkage < 1.0 means we're intentionally underconfident.
    # Adaptive: early in window (more uncertain) → more shrink;
    #           late in window (price more locked in) → less shrink.
    progress = _clamp01(1.0 - remaining / 300.0)  # 0.0=start, 1.0=expiry
    # At start: shrinkage=0.75 (aggressive shrink), at expiry: shrinkage=0.92
    cautious_shrinkage = 0.75 + 0.17 * progress

    p_up = p_diffusion + momentum_adj
    if jump_shrink > 0.0:
        p_up = 0.5 + (p_up - 0.5) * (1.0 - jump_shrink)
    # Apply cautious calibration
    p_up = 0.5 + cautious_shrinkage * (p_up - 0.5)
    p_up = _clamp01(p_up)
    probs["CautiousShrinkage"] = cautious_shrinkage
    probs["Final"] = p_up

    # -- 5. Dispersion = gap between our estimate and Polymarket implied --
    poly_implied_up = float(ctx.poly_up_price) if ctx.poly_up_price else 0.5
    probs["PolyImplied"] = poly_implied_up
    dispersion = abs(p_up - poly_implied_up)

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

    def __init__(self, threshold: Optional[int] = None, min_score_margin: float = 0.04):
        # Three judges (MomentumJudge tested but removed — 62.3% accuracy
        # was not independent enough from other judges, slightly hurt ensemble).
        #   1. StatisticalJudge:    physics (bipower variance, jumps, trend)
        #   2. ArbitrageJudge:      lag freshness (is the mispricing exploitable?)
        #   3. OrderbookValueJudge: execution quality (can we profitably execute?)
        self.judges = [
            StatisticalJudge(),
            ArbitrageJudge(),
            OrderbookValueJudge(),
        ]
        jury_size = len(self.judges)
        if threshold is None:
            threshold = max(2, (jury_size // 2) + 1)  # default 2/3
        self.threshold = max(1, min(int(threshold), jury_size))
        self.min_score_margin = max(0.0, float(min_score_margin))

    def _price_n_seconds_ago(self, ctx: MarketContext, seconds: float) -> Optional[float]:
        return _price_at_seconds_ago(ctx.recent_prices, ctx.recent_timestamps, seconds)

    def _recent_move(self, ctx: MarketContext, seconds: float) -> float:
        prev = self._price_n_seconds_ago(ctx, seconds)
        if prev is None or prev <= 0.0:
            return 0.0
        return _safe_pct_change(float(ctx.current_binance_price), float(prev))

    def _judge_base_weights(self, ctx: MarketContext) -> dict[str, float]:
        weights = {j.name: 1.0 for j in self.judges}
        progress = _clamp01(float(ctx.seconds_elapsed) / 300.0)

        # Late window: StatisticalJudge becomes more reliable (less noise)
        if progress > 0.65:
            weights["StatisticalJudge"] = _clamp(weights.get("StatisticalJudge", 1.0) * 1.10, 0.70, 1.20)

        # Wide spreads: downweight OrderbookValueJudge
        up_ask = ctx.poly_up_ask if ctx.poly_up_ask is not None else ctx.poly_up_price
        down_ask = ctx.poly_down_ask if ctx.poly_down_ask is not None else ctx.poly_down_price
        up_bid = ctx.poly_up_bid if ctx.poly_up_bid is not None else max(ctx.poly_up_price - 0.01, 0.0)
        down_bid = ctx.poly_down_bid if ctx.poly_down_bid is not None else max(ctx.poly_down_price - 0.01, 0.0)
        if 0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0:
            up_spread = max(0.0, float(up_ask) - float(up_bid))
            down_spread = max(0.0, float(down_ask) - float(down_bid))
            overround = (float(up_ask) + float(down_ask)) - 1.0
            if (up_spread > 0.055) or (down_spread > 0.055) or (overround > 0.10):
                weights["OrderbookValueJudge"] = _clamp(weights.get("OrderbookValueJudge", 1.0) * 0.82, 0.70, 1.20)

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
        # Both 15s and 45s moves oppose the vote → counter-trend trade.
        # This is the primary loss pattern: betting against momentum.
        return 0.60

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

        # Require majority with no opposing votes.
        # UP UP ABSTAIN → OK.  UP UP DOWN → NO_TRADE.
        if n_up >= self.threshold and n_down == 0 and up_score >= down_score + self.min_score_margin:
            direction = "UP"
            final_vote = Vote.UP
            winning = up_votes
        elif n_down >= self.threshold and n_up == 0 and down_score >= up_score + self.min_score_margin:
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
