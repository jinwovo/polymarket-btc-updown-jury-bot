"""
Configuration for the Polymarket BTC Up/Down 5-minute trading bot.
"""
import os
from dataclasses import dataclass, field

# Try to load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
    # Optional generated creds file (created from POLYMARKET_PRIVATE_KEY).
    load_dotenv(".env.polymarket.generated", override=False)
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
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    # Optional signature type override:
    # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, -1=auto-detect.
    signature_type: int = field(default_factory=lambda: int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "-1")))
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
    # Live sizing mode:
    # - ADAPTIVE: confidence/edge based dynamic sizing (Kelly-style)
    # - FIXED: always use MAX_BET_SIZE as per-trade amount
    live_sizing_mode: str = field(
        default_factory=lambda: os.getenv("LIVE_SIZING_MODE", "ADAPTIVE").strip().upper()
    )
    # Adaptive live sizing (fraction of MAX_BET_SIZE cap, typically account balance in live-control adaptive mode)
    live_adaptive_base_frac: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ADAPTIVE_BASE_FRAC", "0.075"))
    )
    live_adaptive_min_frac: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ADAPTIVE_MIN_FRAC", "0.040"))
    )
    live_adaptive_max_frac: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ADAPTIVE_MAX_FRAC", "0.150"))
    )
    live_adaptive_edge_boost: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ADAPTIVE_EDGE_BOOST", "0.25"))
    )
    live_adaptive_conf_boost: float = field(
        default_factory=lambda: float(os.getenv("LIVE_ADAPTIVE_CONF_BOOST", "0.12"))
    )
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
    # Entry objective:
    # - PROBABILITY_FIRST: prioritize win-probability gate (p_win) over raw EV magnitude
    # - EV_FIRST: prioritize expected ROI gate
    # - HYBRID: require both
    entry_decision_mode: str = field(
        default_factory=lambda: os.getenv("ENTRY_DECISION_MODE", "PROBABILITY_FIRST").strip().upper()
    )
    # Probability-first controls:
    # require model p_win >= max(MIN_WIN_PROBABILITY, profit_break_even_prob + WIN_PROB_MARGIN)
    min_win_probability: float = field(
        default_factory=lambda: float(os.getenv("MIN_WIN_PROBABILITY", "0.53"))
    )
    win_prob_margin: float = field(
        default_factory=lambda: float(os.getenv("WIN_PROB_MARGIN", "0.005"))
    )
    # Close-probability stability gate:
    # - requires minimum directional displacement from window start
    # - penalizes entries when BTC remains too close to start boundary vs expected noise
    # Percent units: 0.015 => 0.015%
    close_prob_min_aligned_move_pct: float = field(
        default_factory=lambda: float(os.getenv("CLOSE_PROB_MIN_ALIGNED_MOVE_PCT", "0.015"))
    )
    # Probability-point penalty cap applied when alignment is weak.
    close_prob_alignment_penalty_max: float = field(
        default_factory=lambda: float(os.getenv("CLOSE_PROB_ALIGNMENT_PENALTY_MAX", "0.10"))
    )
    # Boundary uncertainty band multiplier (in sigma units).
    close_prob_boundary_sigma_mult: float = field(
        default_factory=lambda: float(os.getenv("CLOSE_PROB_BOUNDARY_SIGMA_MULT", "0.45"))
    )
    # Probability-point penalty cap for boundary uncertainty.
    close_prob_uncertainty_penalty_max: float = field(
        default_factory=lambda: float(os.getenv("CLOSE_PROB_UNCERTAINTY_PENALTY_MAX", "0.08"))
    )
    # UP-only meta filter (regime classifier):
    # blocks UP entries in noisy/whipsaw regimes even when EV is positive.
    up_regime_filter_enabled: bool = field(
        default_factory=lambda: os.getenv("UP_REGIME_FILTER_ENABLED", "true").lower() == "true"
    )
    up_regime_min_score: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MIN_SCORE", "0.56"))
    )
    # Percent units (0.060 => 0.060%)
    up_regime_move_scale_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MOVE_SCALE_PCT", "0.060"))
    )
    up_regime_mom10_scale_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MOM10_SCALE_PCT", "0.030"))
    )
    up_regime_mom30_scale_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MOM30_SCALE_PCT", "0.050"))
    )
    up_regime_mom60_scale_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MOM60_SCALE_PCT", "0.080"))
    )
    up_regime_whipsaw_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_WHIPSAW_PCT", "0.020"))
    )
    up_regime_max_jump_ratio: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MAX_JUMP_RATIO", "0.45"))
    )
    up_regime_min_mom30_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MIN_MOM30_PCT", "-0.010"))
    )
    up_regime_min_move_from_start_pct: float = field(
        default_factory=lambda: float(os.getenv("UP_REGIME_MIN_MOVE_FROM_START_PCT", "0.005"))
    )
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
        default_factory=lambda: float(os.getenv("LIVE_MAX_OPPOSITE_IMPLIED", "0.62"))
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
