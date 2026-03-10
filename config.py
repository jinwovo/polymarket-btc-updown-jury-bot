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
    data_api_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_DATA_API_URL", "https://data-api.polymarket.com"))
    relayer_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com"))
    relayer_chain_id: int = field(default_factory=lambda: int(os.getenv("POLYMARKET_RELAYER_CHAIN_ID", "137")))
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    # Optional signature type override:
    # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, -1=auto-detect.
    signature_type: int = field(default_factory=lambda: int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "-1")))
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    # Builder creds are for relayer/redeem workflows, not CLOB trading auth.
    builder_api_key: str = field(default_factory=lambda: os.getenv("POLY_BUILDER_API_KEY", ""))
    builder_api_secret: str = field(default_factory=lambda: os.getenv("POLY_BUILDER_API_SECRET", ""))
    builder_api_passphrase: str = field(default_factory=lambda: os.getenv("POLY_BUILDER_API_PASSPHRASE", ""))
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
    # Live profit mode:
    # - AGGRESSIVE: maximize growth with larger Kelly-driven sizing + relaxed entry guards
    # - BALANCED: keep conservative baseline guards
    live_profit_mode: str = field(
        default_factory=lambda: os.getenv("LIVE_PROFIT_MODE", "AGGRESSIVE").strip().upper()
    )
    live_aggressive_entry_relax: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_ENTRY_RELAX", "0.20"))
    )
    live_aggressive_min_edge_relax: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_MIN_EDGE_RELAX", "0.20"))
    )
    live_aggressive_support_relax: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_SUPPORT_RELAX", "0.10"))
    )
    live_aggressive_max_frac: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_MAX_FRAC", "0.12"))
    )
    live_aggressive_kelly_frac: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_KELLY_FRAC", "0.50"))
    )
    live_aggressive_loss_deboost: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AGGRESSIVE_LOSS_DEBOOST", "0.82"))
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
    # In adaptive live sizing, refresh collateral balance cap at this interval.
    # Also refreshed immediately after a filled entry.
    live_balance_refresh_seconds: float = field(
        default_factory=lambda: float(os.getenv("LIVE_BALANCE_REFRESH_SECONDS", "30"))
    )
    # Attempt auto-claim/redeem on a rolling interval (best-effort).
    live_auto_claim_enabled: bool = field(
        default_factory=lambda: os.getenv("LIVE_AUTO_CLAIM_ENABLED", "true").lower() == "true"
    )
    live_auto_claim_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("LIVE_AUTO_CLAIM_INTERVAL_SECONDS", "90"))
    )
    # Post-settlement liquidation for previous 5m winning position.
    # Attempts to SELL the *previous* window position after rollover, then fall back to claim.
    live_settlement_exit_enabled: bool = field(
        default_factory=lambda: os.getenv("LIVE_SETTLEMENT_EXIT_ENABLED", "true").lower() == "true"
    )
    live_settlement_exit_delay1_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_SETTLEMENT_EXIT_DELAY1_SEC", "10"))
    )
    live_settlement_exit_delay2_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_SETTLEMENT_EXIT_DELAY2_SEC", "20"))
    )
    live_settlement_exit_min_bid: float = field(
        default_factory=lambda: float(os.getenv("LIVE_SETTLEMENT_EXIT_MIN_BID", "0.90"))
    )
    # Do not liquidate if this SELL would be interpreted as a loss.
    live_settlement_exit_min_roi_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_SETTLEMENT_EXIT_MIN_ROI_PCT", "0.0"))
    )
    # Live early-exit risk controls (mirrors paper sim behavior).
    live_enable_early_exit: bool = field(
        default_factory=lambda: os.getenv("LIVE_ENABLE_EARLY_EXIT", "true").lower() == "true"
    )
    live_early_exit_min_elapsed_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_MIN_ELAPSED_SEC", "25"))
    )
    live_early_exit_opposite_ask: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_OPPOSITE_ASK", "0.78"))
    )
    live_early_exit_opposite_min_loss_roi_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT", "-20.0"))
    )
    live_early_exit_opposite_confirm_polls: int = field(
        default_factory=lambda: int(os.getenv("LIVE_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS", "3"))
    )
    live_early_exit_stop_loss_roi_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_ROI_PCT", "-60.0"))
    )
    live_early_exit_stop_loss_min_hold_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC", "35"))
    )
    live_early_exit_stop_loss_high_conf_cutoff: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF", "0.75"))
    )
    live_early_exit_stop_loss_high_conf_min_hold_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC", "20"))
    )
    live_early_exit_stop_loss_low_conf_cutoff: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF", "0.60"))
    )
    live_early_exit_stop_loss_low_conf_relax_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT", "15"))
    )
    live_early_exit_stop_loss_require_btc_adverse: bool = field(
        default_factory=lambda: os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE", "true").lower() == "true"
    )
    live_early_exit_stop_loss_btc_adverse_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT", "0.090"))
    )
    live_early_exit_max_hold_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_MAX_HOLD_SEC", "220"))
    )
    live_early_exit_timestop_max_remain_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC", "20"))
    )
    live_early_exit_timestop_max_roi_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT", "-8.0"))
    )
    # Optional profit-locking liquidation near expiry to reduce claim dependency.
    # If enabled, bot will attempt SELL FAK close when remaining time is short and bid is strong enough.
    live_pre_expiry_liquidation_enabled: bool = field(
        default_factory=lambda: os.getenv("LIVE_PRE_EXPIRY_LIQUIDATION_ENABLED", "false").lower() == "true"
    )
    live_pre_expiry_liquidation_remain_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_PRE_EXPIRY_LIQUIDATION_REMAIN_SEC", "12"))
    )
    live_pre_expiry_liquidation_min_bid: float = field(
        default_factory=lambda: float(os.getenv("LIVE_PRE_EXPIRY_LIQUIDATION_MIN_BID", "0.90"))
    )
    live_pre_expiry_liquidation_min_roi_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_PRE_EXPIRY_LIQUIDATION_MIN_ROI_PCT", "0.0"))
    )
    live_recent_move_lookback_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_RECENT_MOVE_LOOKBACK_SEC", "20"))
    )
    live_min_recent_move_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MIN_RECENT_MOVE_PCT", "0.006"))
    )
    live_trend_align_lookback_sec: float = field(
        default_factory=lambda: float(os.getenv("LIVE_TREND_ALIGN_LOOKBACK_SEC", "75"))
    )
    live_trend_align_max_opposing_move_pct: float = field(
        default_factory=lambda: float(os.getenv("LIVE_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT", "0.004"))
    )
    live_max_opposite_implied: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MAX_OPPOSITE_IMPLIED", "0.62"))
    )
    live_min_entry_side_implied: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MIN_ENTRY_SIDE_IMPLIED", "0.22"))
    )
    live_max_contra_gap: float = field(
        default_factory=lambda: float(os.getenv("LIVE_MAX_CONTRA_GAP", "0.030"))
    )
    live_contra_override_min_model_prob: float = field(
        default_factory=lambda: float(os.getenv("LIVE_CONTRA_OVERRIDE_MIN_MODEL_PROB", "0.66"))
    )
    live_contra_override_min_conf: float = field(
        default_factory=lambda: float(os.getenv("LIVE_CONTRA_OVERRIDE_MIN_CONF", "0.75"))
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
