"""
Risk Manager - controls position sizing and prevents excessive losses.
"""
import time
import logging
from dataclasses import dataclass, field

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

    def __init__(self, time_fn=None):
        self.trades: list[TradeRecord] = []
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0
        # Allow injecting a custom time function for backtesting
        self._time = time_fn or time.time
        self.daily_reset_time: float = self._next_daily_reset()

    def _next_daily_reset(self) -> float:
        """Next midnight UTC."""
        now = self._time()
        midnight = (int(now) // 86400 + 1) * 86400
        return float(midnight)

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

        # Daily loss limit
        if self.daily_pnl <= -config.risk.daily_loss_limit:
            return False, f"Daily loss limit reached (${self.daily_pnl:.2f})"

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
        Compute optimal bet size using fractional Kelly criterion.
        
        Kelly: f* = (bp - q) / b
        where b = decimal odds - 1, p = estimated probability, q = 1-p
        
        We use fractional Kelly (kelly_fraction) for safety.
        """
        if confidence <= 0 or edge <= 0:
            return 0.0

        # Simplified: use edge and confidence to size
        # edge is roughly (fair_price - market_price)
        # confidence is our conviction (0-1)

        # Base Kelly
        kelly_bet = edge * confidence

        # Apply fraction
        sized = kelly_bet * config.trading.kelly_fraction

        # Scale to max bet size
        bet_amount = sized * config.trading.max_bet_size

        # Clamp
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

    def resolve_trade(self, trade: TradeRecord, won: bool):
        """Resolve a pending trade with its outcome."""
        if won:
            # Payout is amount / price (number of shares) * $1 per share if won
            shares = trade.amount / trade.price if trade.price > 0 else 0
            payout = shares  # $1 per winning share
            trade.pnl = payout - trade.amount
            trade.result = "WIN"
            self.consecutive_losses = 0
        else:
            trade.pnl = -trade.amount
            trade.result = "LOSS"
            self.consecutive_losses += 1

        self.daily_pnl += trade.pnl
        logger.info(
            f"Trade resolved: {trade.result} | PnL: ${trade.pnl:+.2f} | "
            f"Daily: ${self.daily_pnl:+.2f} | Streak: {self.consecutive_losses} losses"
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
