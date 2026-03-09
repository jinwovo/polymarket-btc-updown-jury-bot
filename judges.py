"""
Five independent judges for BTC Up/Down 5-minute markets.

Judge 1: Technical indicators (RSI, momentum, Bollinger)
Judge 2: Binance-vs-Polymarket mispricing
Judge 3: Statistical regime and streak behavior
Judge 4: Multi-horizon trend persistence
Judge 5: Orderbook value/quality (ask edge, spread, overround)
"""
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


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


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm_cdf(z: float) -> float:
    # Standard normal CDF via erf to avoid heavy dependencies.
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Judge 1: Technical Analysis
# ---------------------------------------------------------------------------
class TechnicalJudge:
    name = "TechnicalJudge"

    def __init__(self, rsi_period: int = 14, bb_period: int = 20, bb_std: float = 2.0):
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_std = bb_std

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

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        if len(ctx.recent_prices) < 10:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient price data", self.name)

        prices = np.array(ctx.recent_prices, dtype=float)
        if len(prices) > 120:
            step = max(1, len(prices) // 60)
            candles = prices[::step]
        else:
            candles = prices

        rsi = self._compute_rsi(candles)
        momentum = self._compute_momentum(candles)
        bb_lower, _bb_mid, bb_upper = self._compute_bollinger(candles)
        current = float(ctx.current_binance_price)
        change_from_start = _safe_pct_change(current, ctx.market_start_price)

        signals: list[float] = []

        if rsi > 60:
            signals.append(0.3)
        elif rsi < 40:
            signals.append(-0.3)

        if momentum > 0.05:
            signals.append(0.4)
        elif momentum < -0.05:
            signals.append(-0.4)

        if bb_upper > bb_lower:
            bb_position = (current - bb_lower) / (bb_upper - bb_lower)
        else:
            bb_position = 0.5

        if bb_position > 0.9:
            signals.append(0.2 if momentum > 0.1 else -0.2)
        elif bb_position < 0.1:
            signals.append(-0.2 if momentum < -0.1 else 0.2)

        if abs(change_from_start) > 0.05:
            signals.append(0.3 if change_from_start > 0 else -0.3)

        total = sum(signals)
        confidence = min(abs(total), 1.0)
        if total > 0.15:
            vote = Vote.UP
        elif total < -0.15:
            vote = Vote.DOWN
        else:
            vote = Vote.ABSTAIN

        reason = (
            f"RSI={rsi:.1f}, mom={momentum:+.4f}%, "
            f"BB_pos={bb_position:.2f}, move={change_from_start:+.4f}%"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 2: Price Divergence / Arbitrage Judge
# ---------------------------------------------------------------------------
class ArbitrageJudge:
    name = "ArbitrageJudge"

    def __init__(self, min_edge: float = 0.02, min_samples: int = 24):
        self.min_edge = max(0.0, float(min_edge))
        self.min_samples = max(10, int(min_samples))

    def _estimate_diffusion_params(self, ctx: MarketContext) -> tuple[float, float] | tuple[None, None]:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < self.min_samples:
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
        if len(dlog) < self.min_samples // 2:
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

    def _estimate_fair_prob(self, ctx: MarketContext) -> float:
        mu, sigma = self._estimate_diffusion_params(ctx)
        if mu is None or sigma is None:
            return 0.5

        t = max(1.0, float(ctx.seconds_remaining))
        if ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return 0.5

        x = math.log(ctx.current_binance_price / ctx.market_start_price)
        drift = (mu - 0.5 * sigma * sigma) * t
        denom = sigma * math.sqrt(t)
        if denom < 1e-8:
            return 1.0 if (x + drift) > 0 else 0.0
        z = (x + drift) / denom
        return _clamp01(_norm_cdf(z))

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        if ctx.market_start_price == 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "No market start price", self.name)

        fair_prob_up = self._estimate_fair_prob(ctx)
        fair_prob_down = 1.0 - fair_prob_up

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
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                "Invalid market prices",
                self.name,
            )

        up_edge = fair_prob_up - up_px
        down_edge = fair_prob_down - down_px
        market_prob_up = up_px / max(1e-9, (up_px + down_px))

        if up_edge > down_edge + 0.003 and up_edge > self.min_edge:
            vote = Vote.UP
            edge = up_edge
        elif down_edge > up_edge + 0.003 and down_edge > self.min_edge:
            vote = Vote.DOWN
            edge = down_edge
        else:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"No significant edge: up={up_edge:+.3f}, down={down_edge:+.3f}",
                self.name,
            )

        certainty = abs(fair_prob_up - 0.5) * 2.0
        confidence = min(edge * (4.0 + 1.5 * certainty), 1.0)
        reason = (
            f"gbm_p_up={fair_prob_up:.3f}, mkt_p_up={market_prob_up:.3f}, "
            f"ask=({up_px:.3f}/{down_px:.3f}), edge={edge:+.3f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 3: Statistical / Pattern Judge
# ---------------------------------------------------------------------------
class StatisticalJudge:
    name = "StatisticalJudge"

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
        y = np.array(prices, dtype=float)
        x = np.arange(len(y), dtype=float)
        x_mean = np.mean(x)
        y_mean = np.mean(y)
        denom = max(np.sum((x - x_mean) ** 2), 1e-10)
        slope = np.sum((x - x_mean) * (y - y_mean)) / denom
        return (slope / y_mean) * 100.0

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

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20 or ctx.market_start_price <= 0 or ctx.current_binance_price <= 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient statistical features", self.name)

        look_prices = ctx.recent_prices[-300:] if len(ctx.recent_prices) > 300 else ctx.recent_prices
        look_ts = ctx.recent_timestamps[-300:] if len(ctx.recent_timestamps) > 300 else ctx.recent_timestamps
        rv, bv, jump_var = self._realized_variation_parts(look_prices, look_ts)
        jump_ratio = jump_var / max(rv, 1e-12)

        # Estimate diffusion sigma from jump-robust component.
        elapsed = max(1.0, float(ctx.seconds_elapsed))
        sigma_per_sqrt_sec = math.sqrt(max(bv, 1e-12) / elapsed)
        rem = max(1.0, float(ctx.seconds_remaining))
        denom = sigma_per_sqrt_sec * math.sqrt(rem)

        x = math.log(ctx.current_binance_price / ctx.market_start_price)
        z = (x / denom) if denom > 1e-10 else (8.0 if x > 0 else -8.0 if x < 0 else 0.0)
        p_up = _clamp01(_norm_cdf(z))

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
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Invalid market prices", self.name)

        trend = self._compute_trend_strength(look_prices[-40:] if len(look_prices) > 40 else look_prices)
        streak_dir, streak_len = self._recent_streak(ctx.recent_results or [])

        # Base edges from jump-robust probability model.
        up_edge = p_up - up_px
        down_edge = (1.0 - p_up) - down_px

        # Regime logic:
        # - high jump ratio: reduce aggression and prefer near-close strong z moves only
        # - low jump ratio: allow standard edge comparison
        jumpy = jump_ratio > 0.35
        edge_bar = 0.018 if not jumpy else 0.028

        score = 0.0
        if up_edge > down_edge + 0.004 and up_edge > edge_bar:
            score += 0.55
        elif down_edge > up_edge + 0.004 and down_edge > edge_bar:
            score -= 0.55

        # Trend consistency bonus.
        if abs(trend) > 0.0008:
            score += 0.20 if trend > 0 else -0.20

        # In jumpy regimes, only trust very strong standardized displacement near close.
        if jumpy:
            if rem < 100 and abs(z) > 1.4:
                score += 0.25 if z > 0 else -0.25
            else:
                # Avoid over-reacting to likely transient jumps early in the window.
                score *= 0.55

        # Small anti-streak regularizer (mean-reverting penalty only on long streaks).
        if streak_len >= 5 and streak_dir in ("UP", "DOWN"):
            score += -0.08 if streak_dir == "UP" else 0.08

        if score > 0.18:
            vote = Vote.UP
        elif score < -0.18:
            vote = Vote.DOWN
        else:
            vote = Vote.ABSTAIN

        confidence = min(abs(score) * (0.85 + min(abs(z), 2.0) * 0.18), 1.0)
        reason = (
            f"p_up={p_up:.3f}, z={z:+.2f}, edge=({up_edge:+.3f}/{down_edge:+.3f}), "
            f"rv={rv:.2e}, bv={bv:.2e}, jump={jump_ratio:.2f}, trend={trend:+.4f}%"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 4: Multi-Horizon Trend Persistence
# ---------------------------------------------------------------------------
class TrendPersistenceJudge:
    name = "TrendPersistenceJudge"

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

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 20:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "Insufficient trend lookback", self.name)

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

        signals: list[float] = []

        for ret, thr, w in (
            (r15, 0.012, 0.22),
            (r45, 0.020, 0.32),
            (r120, 0.030, 0.38),
        ):
            if ret > thr:
                signals.append(w)
            elif ret < -thr:
                signals.append(-w)

        if (r15 > 0 and r45 > 0) or (r15 < 0 and r45 < 0):
            signals.append(0.14 if r15 > 0 else -0.14)

        if ctx.seconds_remaining < 90 and abs(start_move) > 0.02:
            signals.append(0.20 if start_move > 0 else -0.20)

        total = sum(signals)
        confidence = min(abs(total), 1.0)
        if total > 0.18:
            vote = Vote.UP
        elif total < -0.18:
            vote = Vote.DOWN
        else:
            vote = Vote.ABSTAIN

        reason = (
            f"r15={r15:+.4f}% r45={r45:+.4f}% r120={r120:+.4f}% "
            f"start={start_move:+.4f}% score={total:+.3f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 5: Orderbook Value / Quality
# ---------------------------------------------------------------------------
class OrderbookValueJudge:
    name = "OrderbookValueJudge"

    def __init__(self, min_entry_edge: float = 0.02, max_spread: float = 0.08, max_overround: float = 0.12):
        self.min_entry_edge = min_entry_edge
        self.max_spread = max_spread
        self.max_overround = max_overround

    def _fair_prob_up(self, ctx: MarketContext) -> float:
        move_pct = _safe_pct_change(ctx.current_binance_price, ctx.market_start_price)
        progress = _clamp01(ctx.seconds_elapsed / 300.0)
        effective_move = move_pct * (0.8 + progress * 1.8)
        k = 22.0 + 14.0 * progress
        prob_up = 1.0 / (1.0 + math.exp(-k * effective_move / 100.0))
        return _clamp01(prob_up)

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

        fair_up = self._fair_prob_up(ctx)
        fair_down = 1.0 - fair_up
        up_edge = fair_up - up_ask
        down_edge = fair_down - down_ask

        if up_edge > down_edge + 0.01 and up_edge > self.min_entry_edge:
            vote = Vote.UP
            edge = up_edge
        elif down_edge > up_edge + 0.01 and down_edge > self.min_entry_edge:
            vote = Vote.DOWN
            edge = down_edge
        else:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"No ask edge: up={up_edge:+.3f} down={down_edge:+.3f}",
                self.name,
            )

        quality = 1.0
        quality -= min(0.6, (up_spread + down_spread) * 4.0)
        quality -= max(0.0, overround) * 1.5
        quality = max(0.25, quality)

        confidence = min(edge * 6.0, 1.0) * quality
        reason = (
            f"edge={edge:+.3f}, fair_up={fair_up:.3f}, asks=({up_ask:.3f}/{down_ask:.3f}), "
            f"spread=({up_spread:.3f}/{down_spread:.3f}), overround={overround:.3f}"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


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
        up_score = sum(v.confidence for v in up_votes)
        down_score = sum(v.confidence for v in down_votes)

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

        avg_confidence = sum(v.confidence for v in winning) / len(winning) if winning else 0.0
        max_confidence = max((v.confidence for v in winning), default=0.0)
        unanimous = (n_up == self.size and self.size > 0) or (n_down == self.size and self.size > 0)

        for v in verdicts:
            logger.info(
                "  [%s] %s (conf=%.3f): %s",
                v.judge_name,
                v.vote.value,
                v.confidence,
                v.reason,
            )
        logger.info(
            "  JURY: %s | votes UP=%d DOWN=%d ABSTAIN=%d | score UP=%.3f DOWN=%.3f | avg_conf=%.3f",
            direction,
            n_up,
            n_down,
            self.size - n_up - n_down,
            up_score,
            down_score,
            avg_confidence,
        )

        return JuryDecision(
            final_vote=final_vote,
            direction=direction,
            avg_confidence=avg_confidence,
            max_edge=max_confidence,
            verdicts=verdicts,
            unanimous=unanimous,
        )
