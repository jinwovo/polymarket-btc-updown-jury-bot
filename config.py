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
    # Position mode for live execution:
    # - BOTH: allow UP and DOWN
    # - UP_ONLY: only execute UP entries
    # - DOWN_ONLY: only execute DOWN entries
    position_mode: str = field(default_factory=lambda: os.getenv("POSITION_MODE", "BOTH").strip().upper())
    # Minimum edge (expected value advantage) required to place a trade
    min_edge: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE", "0.08")))
    # Approximate total fee/slippage drag per completed trade as a stake fraction.
    fee_rate: float = field(default_factory=lambda: float(os.getenv("TRADE_FEE_RATE", "0.010")))
    # Require this minimum expected ROI (after fee_rate) before entry.
    min_expected_roi: float = field(default_factory=lambda: float(os.getenv("MIN_EXPECTED_ROI", "0.003")))
    # Jury: minimum same-direction votes required to trade
    jury_threshold: int = field(default_factory=lambda: int(os.getenv("JURY_THRESHOLD", "3")))
    # How many seconds before market close to stop entering
    cutoff_before_close_seconds: int = 60
    # Kelly criterion fraction (fractional Kelly for safety)
    kelly_fraction: float = 0.25
    # Live execution mode:
    # - LIMIT_GTC: limit order at/near current ask, wait up to timeout, then cancel remainder
    # - LIMIT_FAK: limit order with fill-and-kill semantics
    # - MARKET: market/FOK style taker execution
    entry_order_mode: str = field(
        default_factory=lambda: os.getenv("ENTRY_ORDER_MODE", "LIMIT_GTC").strip().upper()
    )
    # Cancel outstanding limit order after this many seconds.
    limit_order_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LIMIT_ORDER_TIMEOUT_SECONDS", "2.5"))
    )
    # Poll interval while waiting for fills.
    order_poll_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("ORDER_POLL_INTERVAL_SECONDS", "0.35"))
    )
    # Entry protection:
    # reject if current ask drifts above reference ask by more than this absolute amount (price points).
    max_entry_price_drift_abs: float = field(
        default_factory=lambda: float(os.getenv("MAX_ENTRY_PRICE_DRIFT_ABS", "0.010"))
    )
    # Also allow relative drift cap (fraction of reference ask).
    max_entry_price_drift_ratio: float = field(
        default_factory=lambda: float(os.getenv("MAX_ENTRY_PRICE_DRIFT_RATIO", "0.03"))
    )
    # Conservative live-entry filters (for 5m close-direction consistency)
    live_entry_start_seconds: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ENTRY_START_SECONDS", "75"))
    )
    live_min_support_ratio: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MIN_SUPPORT_RATIO", "0.70"))
    )
    live_require_unanimous: bool = field(
        default_factory=lambda: os.getenv("LIVE_REQUIRE_UNANIMOUS", "false").lower() == "true"
    )
    live_recent_move_lookback_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_RECENT_MOVE_LOOKBACK_SEC", "20"))
    )
    live_min_recent_move_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MIN_RECENT_MOVE_PCT", "0.006"))
    )
    live_max_opposite_implied: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MAX_OPPOSITE_IMPLIED", "0.56"))
    )
    live_min_entry_side_implied: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MIN_ENTRY_SIDE_IMPLIED", "0.22"))
    )
    live_down_above_start_block_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_DOWN_ABOVE_START_BLOCK_PCT", "0.015"))
    )
    live_down_above_start_momentum_extra: float = field(
        default_factory=lambda: float(os.getenv("LIVE_DOWN_ABOVE_START_MOMENTUM_EXTRA", "0.006"))
    )
    live_down_above_start_ev_penalty: float = field(
        default_factory=lambda: float(os.getenv("LIVE_DOWN_ABOVE_START_EV_PENALTY", "0.020"))
    )
    # Fast-lane (judge bypass) for Binance lead vs Polymarket lag.
    fast_lane_enabled: bool = field(
        default_factory=lambda: os.getenv("FAST_LANE_ENABLED", "true").lower() == "true"
    )
    fast_lane_min_seconds_elapsed: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_SECONDS_ELAPSED", "20"))
    )
    fast_lane_max_seconds_elapsed: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MAX_SECONDS_ELAPSED", "160"))
    )
    fast_lane_min_seconds_remaining: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_SECONDS_REMAINING", "95"))
    )
    # Percent units (same convention as existing *_MOVE_PCT fields): 0.01 => 0.01%
    fast_lane_min_move_pct: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_MOVE_PCT", "0.040"))
    )
    fast_lane_max_move_pct: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MAX_MOVE_PCT", "0.300"))
    )
    fast_lane_recent_lookback_sec: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_RECENT_LOOKBACK_SEC", "10"))
    )
    fast_lane_min_recent_move_pct: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_RECENT_MOVE_PCT", "0.015"))
    )
    fast_lane_vol_lookback_sec: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_VOL_LOOKBACK_SEC", "150"))
    )
    fast_lane_drift_weight: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_DRIFT_WEIGHT", "0.30"))
    )
    fast_lane_max_entry_price: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MAX_ENTRY_PRICE", "0.46"))
    )
    fast_lane_min_direction_prob: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_DIRECTION_PROB", "0.57"))
    )
    fast_lane_min_prob_edge: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_PROB_EDGE", "0.050"))
    )
    fast_lane_min_expected_roi: float = field(
        default_factory=lambda: float(os.getenv("FAST_LANE_MIN_EXPECTED_ROI", "0.080"))
    )
    # Feature feed normalization for judge inputs.
    # Judges consume a fixed-interval series built from irregular Binance ticks.
    feature_lookback_seconds: int = field(
        default_factory=lambda: int(os.getenv("FEATURE_LOOKBACK_SECONDS", "600"))
    )
    feature_resample_seconds: float = field(
        default_factory=lambda: float(os.getenv("FEATURE_RESAMPLE_SECONDS", "1.0"))
    )
    feature_max_points: int = field(
        default_factory=lambda: int(os.getenv("FEATURE_MAX_POINTS", "900"))
    )


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
