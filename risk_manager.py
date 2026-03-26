"""
Risk Manager - controls position sizing and prevents excessive losses.
"""
import time
import logging
from dataclasses import dataclass

from config import config

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    timestamp: float
    direction: str  # "UP" or "DOWN"
    amount: float   # USDC spent
    price: float    # price paid per share
    result: str = "PENDING"  # "WIN", "LOSS", "PENDING"
    pnl: float = 0.0


class RiskManager:
    """Manages risk by tracking trades, enforcing limits, and computing position sizes."""

    # Adaptive sizing: bet 5-20% of equity based on conviction
    # Sweep showed 5-20% gives +$768 PnL vs 5-15% with same drawdown profile
    BET_PCT_MIN = 0.10   # 5% of equity for weakest qualifying signals
    BET_PCT_MAX = 0.15   # 20% of equity for strongest signals

    def __init__(self, time_fn=None, initial_equity: float = 0.0):
        self.trades: list[TradeRecord] = []
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0
        # Allow injecting a custom time function for backtesting
        self._time = time_fn or time.time
        self.daily_reset_time: float = self._next_daily_reset()
        # Equity tracking for adaptive sizing
        self._initial_equity = initial_equity if initial_equity > 0 else config.trading.max_bet_size * 20
        self.equity: float = self._initial_equity

    # KST = UTC+9 (9 * 3600 = 32400 seconds)
    _TZ_OFFSET = 9 * 3600

    def _next_daily_reset(self) -> float:
        """Next midnight KST (UTC+9)."""
        now = self._time()
        # Shift to KST, find next midnight, shift back to UTC
        kst_now = now + self._TZ_OFFSET
        kst_midnight = (int(kst_now) // 86400 + 1) * 86400
        return float(kst_midnight - self._TZ_OFFSET)

    def _check_daily_reset(self):
        now = self._time()
        if now >= self.daily_reset_time:
            logger.info(f"Daily reset: PnL was ${self.daily_pnl:.2f}")
            self.daily_pnl = 0.0
            self.daily_reset_time = self._next_daily_reset()

    def can_trade(self) -> tuple[bool, str]:
        """Check if we're allowed to trade right now."""
        self._check_daily_reset()

        # Cooldown after loss streak
        if self._time() < self.cooldown_until:
            remaining = self.cooldown_until - self._time()
            return False, f"Cooldown active ({remaining:.0f}s remaining)"

        # Daily loss limit = 40% of current equity (auto-scales with balance)
        effective_daily_limit = self.equity * 0.40
        if effective_daily_limit < 1.0:
            effective_daily_limit = 1.0  # absolute floor $1
        if self.daily_pnl <= -effective_daily_limit:
            return False, f"Daily loss limit reached (${self.daily_pnl:.2f}, limit=${effective_daily_limit:.2f})"

        # Consecutive loss limit
        if self.consecutive_losses >= config.risk.max_consecutive_losses:
            self.cooldown_until = self._time() + config.risk.cooldown_after_loss_streak_seconds
            self.consecutive_losses = 0  # reset after applying cooldown
            return False, f"Max consecutive losses, entering cooldown"

        # Open positions limit
        open_count = sum(1 for t in self.trades if t.result == "PENDING")
        if open_count >= config.risk.max_open_positions:
            return False, f"Max open positions reached ({open_count})"

        return True, "OK"

    def compute_bet_size(self, confidence: float, edge: float) -> float:
        """
        Adaptive bet sizing: 5-15% of current equity based on conviction.

        conviction = edge * confidence (0..1 range, higher = stronger signal)
        bet_pct = lerp(5%, 15%, conviction_normalized)
        bet = equity * bet_pct

        Clamped to MAX_BET_SIZE as hard ceiling and MIN_BET_SIZE as floor.
        Reduced further on losing streaks.
        """
        if confidence <= 0 or edge <= 0:
            return 0.0

        # Direct confidence-based sizing: conf 0.3=5%, conf 1.0=20%
        # Low confidence = small bet, high confidence = big bet
        conf_norm = min(1.0, max(0.0, (confidence - 0.3) / 0.7))
        bet_pct = self.BET_PCT_MIN + conf_norm * (self.BET_PCT_MAX - self.BET_PCT_MIN)

        bet_amount = self.equity * bet_pct

        # Hard ceiling from config, floor from min_bet_size
        bet_amount = max(config.trading.min_bet_size, min(bet_amount, config.trading.max_bet_size))

        # Reduce if we're on a losing streak
        if self.consecutive_losses >= 2:
            reduction = 0.5 ** (self.consecutive_losses - 1)
            bet_amount *= reduction
            bet_amount = max(config.trading.min_bet_size, bet_amount)

        return round(bet_amount, 2)

    def record_trade(self, direction: str, amount: float, price: float) -> TradeRecord:
        """Record a new trade."""
        trade = TradeRecord(
            timestamp=self._time(),
            direction=direction,
            amount=amount,
            price=price,
        )
        self.trades.append(trade)
        logger.info(f"Trade recorded: {direction} ${amount:.2f} @ {price:.4f}")
        return trade

    def resolve_trade(self, trade: TradeRecord, won: bool, actual_pnl: float | None = None):
        """Resolve a pending trade with its outcome.

        Args:
            actual_pnl: If provided, use this PnL instead of recalculating
                        from settlement.  Needed when early exits produce a
                        PnL different from full win/loss settlement.
        """
        trade.result = "WIN" if won else "LOSS"
        if won:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        if actual_pnl is not None:
            trade.pnl = actual_pnl
        elif won:
            shares = trade.amount / trade.price if trade.price > 0 else 0
            trade.pnl = shares - trade.amount
        else:
            trade.pnl = -trade.amount

        self.daily_pnl += trade.pnl
        self.equity += trade.pnl
        logger.info(
            f"Trade resolved: {trade.result} | PnL: ${trade.pnl:+.2f} | "
            f"Equity: ${self.equity:.2f} | Daily: ${self.daily_pnl:+.2f} | "
            f"Streak: {self.consecutive_losses} losses"
        )

    def get_pending_trades(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.result == "PENDING"]

    def get_stats(self) -> dict:
        """Get summary statistics."""
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t.result == "WIN")
        losses = sum(1 for t in self.trades if t.result == "LOSS")
        pending = sum(1 for t in self.trades if t.result == "PENDING")
        total_pnl = sum(t.pnl for t in self.trades if t.result != "PENDING")

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": wins / max(wins + losses, 1),
            "total_pnl": total_pnl,
            "daily_pnl": self.daily_pnl,
            "consecutive_losses": self.consecutive_losses,
        }
