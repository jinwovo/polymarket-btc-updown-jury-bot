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

    def __init__(self, min_price_move_pct: float = 0.03):
        self.min_price_move_pct = min_price_move_pct

    def _estimate_fair_prob(self, price_change_pct: float, seconds_remaining: float) -> float:
        time_factor = max(0.1, 1.0 - (seconds_remaining / 300.0))
        effective_move = price_change_pct * (1.0 + time_factor * 2.0)
        k = 30.0
        prob_up = 1.0 / (1.0 + math.exp(-k * effective_move / 100.0))
        return _clamp01(prob_up)

    def judge(self, ctx: MarketContext) -> JudgeVerdict:
        if ctx.market_start_price == 0:
            return JudgeVerdict(Vote.ABSTAIN, 0.0, "No market start price", self.name)

        price_change_pct = _safe_pct_change(
            ctx.current_binance_price,
            ctx.market_start_price,
        )
        if abs(price_change_pct) < self.min_price_move_pct:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"Price move too small: {price_change_pct:+.4f}%",
                self.name,
            )

        fair_prob_up = self._estimate_fair_prob(price_change_pct, ctx.seconds_remaining)
        fair_prob_down = 1.0 - fair_prob_up
        up_edge = fair_prob_up - ctx.poly_up_price
        down_edge = fair_prob_down - ctx.poly_down_price

        if up_edge > down_edge and up_edge > 0.05:
            vote = Vote.UP
            edge = up_edge
        elif down_edge > up_edge and down_edge > 0.05:
            vote = Vote.DOWN
            edge = down_edge
        else:
            return JudgeVerdict(
                Vote.ABSTAIN,
                0.0,
                f"No significant edge: up={up_edge:+.3f}, down={down_edge:+.3f}",
                self.name,
            )

        confidence = min(edge * 2.0, 1.0)
        reason = (
            f"fair_up={fair_prob_up:.3f}, fair_down={fair_prob_down:.3f}, "
            f"edge={edge:+.3f}, move={price_change_pct:+.4f}%"
        )
        return JudgeVerdict(vote, confidence, reason, self.name)


# ---------------------------------------------------------------------------
# Judge 3: Statistical / Pattern Judge
# ---------------------------------------------------------------------------
class StatisticalJudge:
    name = "StatisticalJudge"

    def _compute_volatility(self, prices: list[float]) -> float:
        if len(prices) < 2:
            return 0.0
        arr = np.array(prices, dtype=float)
        returns = np.diff(arr) / arr[:-1]
        if len(returns) == 0:
            return 0.0
        return float(np.std(returns)) * math.sqrt(len(returns))

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
        recent_ticks = ctx.recent_prices[-300:] if len(ctx.recent_prices) > 300 else ctx.recent_prices
        vol = self._compute_volatility(recent_ticks)
        price_change_pct = _safe_pct_change(ctx.current_binance_price, ctx.market_start_price)
        high_vol = vol > 0.001

        signals: list[float] = []

        if high_vol and abs(price_change_pct) > 0.02:
            signals.append(0.3 if price_change_pct > 0 else -0.3)
        elif (not high_vol) and abs(price_change_pct) > 0.05:
            signals.append(0.15 if price_change_pct > 0 else -0.15)

        if len(ctx.recent_prices) > 10:
            trend = self._compute_trend_strength(ctx.recent_prices[-30:])
            if abs(trend) > 0.001:
                signals.append(0.25 if trend > 0 else -0.25)

        results = ctx.recent_results or []
        if len(results) >= 3:
            streak_dir, streak_len = self._recent_streak(results)
            if streak_len >= 4:
                signals.append(-0.15 if streak_dir == "UP" else 0.15)
            elif streak_len <= 2:
                signals.append(0.1 if streak_dir == "UP" else -0.1)

        if ctx.seconds_remaining < 120 and abs(price_change_pct) > 0.01:
            time_weight = 0.3 * (1.0 - ctx.seconds_remaining / 300.0)
            signals.append(time_weight if price_change_pct > 0 else -time_weight)

        total = sum(signals)
        confidence = min(abs(total), 1.0)
        if total > 0.1:
            vote = Vote.UP
        elif total < -0.1:
            vote = Vote.DOWN
        else:
            vote = Vote.ABSTAIN

        reason = (
            f"vol={vol:.6f}, move={price_change_pct:+.4f}%, "
            f"signals={total:+.3f}, high_vol={high_vol}"
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
