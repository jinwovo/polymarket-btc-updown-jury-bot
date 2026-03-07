"""
Configuration for the Polymarket BTC Up/Down 5-minute trading bot.
"""
import os
from dataclasses import dataclass, field

# Try to load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class BinanceConfig:
    ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    rest_url: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    kline_url: str = "https://api.binance.com/api/v3/klines"
    aggtrades_url: str = "https://api.binance.com/api/v3/aggTrades"
    price_buffer_seconds: int = 600  # keep last 10 min of tick prices


@dataclass
class PolymarketConfig:
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    funder: str = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER", ""))
    market_slug_prefix: str = "btc-updown-5m"
    interval_seconds: int = 300  # 5 minutes


@dataclass
class TradingConfig:
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")
    max_bet_size: float = field(default_factory=lambda: float(os.getenv("MAX_BET_SIZE", "5.0")))
    min_bet_size: float = 0.5
    # Minimum edge (expected value advantage) required to place a trade
    min_edge: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE", "0.08")))
    # Jury: minimum same-direction votes required to trade
    jury_threshold: int = field(default_factory=lambda: int(os.getenv("JURY_THRESHOLD", "3")))
    # How many seconds before market close to stop entering
    cutoff_before_close_seconds: int = 60
    # Kelly criterion fraction (fractional Kelly for safety)
    kelly_fraction: float = 0.25


@dataclass
class RiskConfig:
    daily_loss_limit: float = field(
        default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
    )
    max_consecutive_losses: int = 5
    max_open_positions: int = 3
    cooldown_after_loss_streak_seconds: int = 300


@dataclass
class Config:
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


config = Config()
