"""
Main bot loop - orchestrates Binance price feed, Polymarket real-time odds,
jury deliberation, and trade execution for BTC Up/Down 5-minute markets.

Core strategy: Speed arbitrage.
Binance price moves first, Polymarket odds lag, so we buy the cheap side before odds adjust.
"""
import asyncio
import json
import time
import signal
import logging
import os
import sys
import math
from datetime import datetime, timezone
from typing import Any, Optional

from config import config
from db_config import (
    connect_db,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)
from entry_parity import (
    ParityAdaptiveConfig,
    ParityAdaptiveState,
    compute_parity_thresholds,
)
from exit_policy import ExitPolicyConfig, ExitPolicyInput, evaluate_exit_policy
from binance_ws import BinancePriceFeed
from polymarket_client import PolymarketClient, MarketInfo, compute_market_timestamps
from judges import Jury, MarketContext, Vote
from risk_manager import RiskManager, TradeRecord
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate
from telegram_notifier import send_telegram_message

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize_position_mode(raw: str) -> str:
    mode = str(raw or "BOTH").strip().upper()
    if mode in ("UP_ONLY", "DOWN_ONLY", "BOTH"):
        return mode
    return "BOTH"


def _normalize_sizing_mode(raw: str) -> str:
    mode = str(raw or "ADAPTIVE").strip().upper()
    if mode in ("ADAPTIVE", "FIXED"):
        return mode
    return "ADAPTIVE"


def _normalize_profit_mode(raw: str) -> str:
    mode = str(raw or "BALANCED").strip().upper()
    if mode in ("AGGRESSIVE", "BALANCED"):
        return mode
    return "BALANCED"


def _safe_prob(value: float | None) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        if 0.0 < v < 1.0:
            return v
    except Exception:
        return None
    return None


def _normalized_market_probs(
    up_ask: float | None,
    down_ask: float | None,
) -> tuple[float | None, float | None]:
    up = _safe_prob(up_ask)
    down = _safe_prob(down_ask)
    if up is None or down is None:
        return (None, None)
    total = float(up + down)
    if total <= 1e-9:
        return (None, None)
    up_prob = float(_clamp(up / total, 0.001, 0.999))
    return (up_prob, float(1.0 - up_prob))


def _recent_move_pct(
    prices: list[float],
    timestamps: list[float],
    now_ts: float,
    lookback_sec: float,
) -> float | None:
    n = min(len(prices), len(timestamps))
    if n <= 1:
        return None
    lo_ts = now_ts - max(1.0, float(lookback_sec))
    p0 = None
    p1 = None
    for i in range(n):
        try:
            ts = float(timestamps[i])
            px = float(prices[i])
        except Exception:
            continue
        if ts < lo_ts:
            continue
        if px <= 0.0:
            continue
        if p0 is None:
            p0 = px
        p1 = px
    if p0 is None or p1 is None or p0 <= 0.0:
        return None
    return ((p1 - p0) / p0) * 100.0


def _recent_move_pct_db(
    conn, window_start: int, now_ts: float, lookback_sec: float,
) -> float | None:
    """DB-based recent move — identical to paper_trade_sim._recent_move_pct.

    Using the same data source (btc_ticks table) ensures paper and live
    see identical momentum/trend values for the same timestamp.
    """
    lo_ts = max(float(window_start), float(now_ts) - max(1.0, float(lookback_sec)))
    hi_ts = float(now_ts)
    first_row = fetch_one(
        conn,
        "SELECT price FROM btc_ticks WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1",
        (lo_ts, hi_ts),
    )
    last_row = fetch_one(
        conn,
        "SELECT price FROM btc_ticks WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (lo_ts, hi_ts),
    )
    if not first_row or not last_row:
        return None
    try:
        p0 = float(first_row[0])
        p1 = float(last_row[0])
        if p0 <= 0.0:
            return None
        return ((p1 - p0) / p0) * 100.0
    except Exception:
        return None


def _build_live_exit_policy_config() -> ExitPolicyConfig:
    return ExitPolicyConfig(
        enabled=bool(config.trading.live_enable_early_exit),
        min_elapsed_sec=float(config.trading.live_early_exit_min_elapsed_sec),
        opposite_ask=float(config.trading.live_early_exit_opposite_ask),
        opposite_min_loss_roi_pct=float(config.trading.live_early_exit_opposite_min_loss_roi_pct),
        opposite_confirm_polls=int(config.trading.live_early_exit_opposite_confirm_polls),
        stop_loss_roi_pct=float(config.trading.live_early_exit_stop_loss_roi_pct),
        stop_loss_min_hold_sec=float(config.trading.live_early_exit_stop_loss_min_hold_sec),
        stop_loss_high_conf_cutoff=float(config.trading.live_early_exit_stop_loss_high_conf_cutoff),
        stop_loss_high_conf_min_hold_sec=float(config.trading.live_early_exit_stop_loss_high_conf_min_hold_sec),
        stop_loss_low_conf_cutoff=float(config.trading.live_early_exit_stop_loss_low_conf_cutoff),
        stop_loss_low_conf_relax_pct=float(config.trading.live_early_exit_stop_loss_low_conf_relax_pct),
        stop_loss_require_btc_adverse=bool(config.trading.live_early_exit_stop_loss_require_btc_adverse),
        stop_loss_btc_adverse_pct=float(config.trading.live_early_exit_stop_loss_btc_adverse_pct),
        max_hold_sec=float(config.trading.live_early_exit_max_hold_sec),
        timestop_max_remain_sec=float(config.trading.live_early_exit_timestop_max_remain_sec),
        timestop_max_roi_pct=float(config.trading.live_early_exit_timestop_max_roi_pct),
        trailing_stop_drop_pct=float(config.trading.live_early_exit_trailing_stop_drop_pct),
        trailing_stop_min_peak_pct=float(config.trading.live_early_exit_trailing_stop_min_peak_pct),
        trailing_stop_min_hold_sec=float(config.trading.live_early_exit_trailing_stop_min_hold_sec),
        profit_take_roi_pct=float(config.trading.live_early_exit_profit_take_roi_pct),
        profit_take_min_hold_sec=float(config.trading.live_early_exit_profit_take_min_hold_sec),
        time_weight_enabled=bool(config.trading.live_time_weighted_exit),
        early_opposite_ask_extra=float(config.trading.live_early_exit_early_opposite_ask_extra),
        early_opposite_loss_extra_pct=float(config.trading.live_early_exit_early_opposite_loss_extra_pct),
        early_stop_loss_extra_pct=float(config.trading.live_early_exit_early_stop_loss_extra_pct),
        early_trailing_drop_extra_pct=float(config.trading.live_early_exit_early_trailing_drop_extra_pct),
        early_trailing_peak_extra_pct=float(config.trading.live_early_exit_early_trailing_peak_extra_pct),
        early_profit_take_extra_pct=float(config.trading.live_early_exit_early_profit_take_extra_pct),
        strong_favor_sigma_mult=float(config.trading.live_early_exit_strong_favor_sigma_mult),
        strong_favor_min_move_pct=float(config.trading.live_early_exit_strong_favor_min_move_pct),
        favor_hold_min_remaining_sec=float(config.trading.live_early_exit_favor_hold_min_remaining_sec),
        favor_hold_break_even_floor_roi_pct=float(
            config.trading.live_early_exit_favor_hold_break_even_floor_roi_pct
        ),
        opposite_late_only_remaining_sec=float(config.trading.live_early_exit_opposite_late_only_remaining_sec),
        opposite_severe_adverse_sigma_mult=float(
            config.trading.live_early_exit_opposite_severe_adverse_sigma_mult
        ),
        opposite_severe_adverse_min_move_pct=float(
            config.trading.live_early_exit_opposite_severe_adverse_min_move_pct
        ),
        trailing_late_only_remaining_sec=float(config.trading.live_early_exit_trailing_late_only_remaining_sec),
        trailing_force_peak_pct=float(config.trading.live_early_exit_trailing_force_peak_pct),
        break_even_late_only_remaining_sec=float(
            config.trading.live_early_exit_break_even_late_only_remaining_sec
        ),
        break_even_force_peak_pct=float(config.trading.live_early_exit_break_even_force_peak_pct),
        profit_take_late_only_remaining_sec=float(
            config.trading.live_early_exit_profit_take_late_only_remaining_sec
        ),
        profit_take_force_roi_pct=float(config.trading.live_early_exit_profit_take_force_roi_pct),
    )


def _build_mirror_exit_policy_config() -> ExitPolicyConfig:
    return ExitPolicyConfig(
        enabled=bool(MIRROR_ENABLE_EARLY_EXIT),
        min_elapsed_sec=float(MIRROR_EARLY_EXIT_MIN_ELAPSED_SEC),
        opposite_ask=float(MIRROR_EARLY_EXIT_OPPOSITE_ASK),
        opposite_min_loss_roi_pct=float(MIRROR_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT),
        opposite_confirm_polls=int(MIRROR_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS),
        stop_loss_roi_pct=float(MIRROR_EARLY_EXIT_STOP_LOSS_ROI_PCT),
        stop_loss_min_hold_sec=float(MIRROR_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC),
        stop_loss_high_conf_cutoff=float(MIRROR_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF),
        stop_loss_high_conf_min_hold_sec=float(MIRROR_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC),
        stop_loss_low_conf_cutoff=float(MIRROR_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF),
        stop_loss_low_conf_relax_pct=float(MIRROR_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT),
        stop_loss_require_btc_adverse=bool(MIRROR_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE),
        stop_loss_btc_adverse_pct=float(MIRROR_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT),
        max_hold_sec=float(MIRROR_EARLY_EXIT_MAX_HOLD_SEC),
        timestop_max_remain_sec=float(MIRROR_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC),
        timestop_max_roi_pct=float(MIRROR_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT),
        trailing_stop_drop_pct=float(MIRROR_EARLY_EXIT_TRAILING_STOP_DROP_PCT),
        trailing_stop_min_peak_pct=float(MIRROR_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT),
        trailing_stop_min_hold_sec=float(MIRROR_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC),
        profit_take_roi_pct=float(MIRROR_EARLY_EXIT_PROFIT_TAKE_ROI_PCT),
        profit_take_min_hold_sec=float(MIRROR_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC),
        time_weight_enabled=bool(MIRROR_TIME_WEIGHTED_EXIT),
        early_opposite_ask_extra=float(MIRROR_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA),
        early_opposite_loss_extra_pct=float(MIRROR_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT),
        early_stop_loss_extra_pct=float(MIRROR_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT),
        early_trailing_drop_extra_pct=float(MIRROR_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT),
        early_trailing_peak_extra_pct=float(MIRROR_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT),
        early_profit_take_extra_pct=float(MIRROR_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT),
        strong_favor_sigma_mult=float(MIRROR_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT),
        strong_favor_min_move_pct=float(MIRROR_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT),
        favor_hold_min_remaining_sec=float(MIRROR_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC),
        favor_hold_break_even_floor_roi_pct=float(MIRROR_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT),
        opposite_late_only_remaining_sec=float(MIRROR_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC),
        opposite_severe_adverse_sigma_mult=float(MIRROR_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT),
        opposite_severe_adverse_min_move_pct=float(MIRROR_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT),
        trailing_late_only_remaining_sec=float(MIRROR_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC),
        trailing_force_peak_pct=float(MIRROR_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT),
        break_even_late_only_remaining_sec=float(MIRROR_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC),
        break_even_force_peak_pct=float(MIRROR_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT),
        profit_take_late_only_remaining_sec=float(MIRROR_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC),
        profit_take_force_roi_pct=float(MIRROR_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT),
    )


# Live/Paper parity gate defaults (mirrors paper_trade_sim.py).
LIVE_MIRROR_PAPER_GATES = os.getenv("LIVE_MIRROR_PAPER_GATES", "true").lower() == "true"
MIRROR_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.020"))
MIRROR_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.70"))
MIRROR_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.40"))
MIRROR_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.52"))
MIRROR_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
MIRROR_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "240"))
MIRROR_DOWN_ENTRY_END_SEC = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "160"))
MIRROR_DOWN_MIN_ENTRY_PRICE = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.42"))
MIRROR_MIN_SECONDS_REMAINING = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
MIRROR_MIN_TICK_SAMPLES = int(os.getenv("PAPER_MIN_TICK_SAMPLES", "100"))
MIRROR_MIN_ODDS_SAMPLES = int(os.getenv("PAPER_MIN_ODDS_SAMPLES", "16"))
MIRROR_RECENT_MOVE_LOOKBACK_SEC = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
MIRROR_MIN_RECENT_MOVE_PCT = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.006"))
MIRROR_MIN_BOUNDARY_DIST_PCT = float(os.getenv("PAPER_MIN_BOUNDARY_DIST_PCT", "0.040"))
MIRROR_DOWN_MIN_BOUNDARY_DIST_PCT = float(os.getenv("PAPER_DOWN_MIN_BOUNDARY_DIST_PCT", "0.050"))
MIRROR_TREND_ALIGN_LOOKBACK_SEC = float(os.getenv("PAPER_TREND_ALIGN_LOOKBACK_SEC", "75"))
MIRROR_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT = float(os.getenv("PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT", "0.004"))
MIRROR_MAX_OPPOSITE_IMPLIED = float(os.getenv("PAPER_MAX_OPPOSITE_IMPLIED", "0.62"))
MIRROR_MIN_ENTRY_SIDE_IMPLIED = float(os.getenv("PAPER_MIN_ENTRY_SIDE_IMPLIED", "0.38"))
MIRROR_MAX_CONTRA_GAP = float(os.getenv("PAPER_MAX_CONTRA_GAP", "0.50"))
MIRROR_CONTRA_OVERRIDE_MIN_MODEL_PROB = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB", "0.66"))
MIRROR_CONTRA_OVERRIDE_MIN_CONF = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_CONF", "0.75"))
MIRROR_DOWN_ABOVE_START_BLOCK_PCT = float(os.getenv("PAPER_DOWN_ABOVE_START_BLOCK_PCT", "0.050"))
MIRROR_DOWN_ABOVE_START_MOMENTUM_EXTRA = float(os.getenv("PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA", "0.006"))
MIRROR_DOWN_ABOVE_START_EV_PENALTY = float(os.getenv("PAPER_DOWN_ABOVE_START_EV_PENALTY", "0.020"))
MIRROR_BASE_TRADE_GAP_SEC = float(os.getenv("PAPER_BASE_TRADE_GAP_SEC", "120"))
MIRROR_TARGET_TRADE_GAP_SEC = float(os.getenv("PAPER_TARGET_TRADE_GAP_SEC", "600"))
MIRROR_STALE_RELAX_START_SEC = float(os.getenv("PAPER_STALE_RELAX_START_SEC", "1800"))
MIRROR_STALE_RELAX_FULL_SEC = float(os.getenv("PAPER_STALE_RELAX_FULL_SEC", "7200"))
MIRROR_STALE_RELAX_MAX = float(os.getenv("PAPER_STALE_RELAX_MAX", "0.75"))
MIRROR_STRICTNESS_UNANIMOUS_AT = float(os.getenv("PAPER_STRICTNESS_UNANIMOUS_AT", "0.90"))
MIRROR_ADAPTIVE_MAX_ASK_FLOOR = float(os.getenv("PAPER_ADAPTIVE_MAX_ASK_FLOOR", "0.47"))
MIRROR_PERF_PAUSE_SEC = float(os.getenv("PAPER_PERF_PAUSE_SEC", "1800"))
MIRROR_HIGH_QUALITY_EV = float(os.getenv("PAPER_HIGH_QUALITY_EV", "0.12"))
MIRROR_HIGH_QUALITY_CONF = float(os.getenv("PAPER_HIGH_QUALITY_CONF", "0.50"))
MIRROR_RECENT_PERF_WINDOW = int(os.getenv("PAPER_RECENT_PERF_WINDOW", "8"))
MIRROR_MIN_RECENT_WIN_RATE = float(os.getenv("PAPER_MIN_RECENT_WIN_RATE", "0.55"))
MIRROR_MAX_DRAWDOWN_STOP_PCT = float(os.getenv("LIVE_MAX_DRAWDOWN_STOP_PCT", "0.20"))
MIRROR_REQUIRE_UNANIMOUS = os.getenv("PAPER_REQUIRE_UNANIMOUS", "false").lower() == "true"
MIRROR_PROFIT_MODE = str(os.getenv("PAPER_PROFIT_MODE", "aggressive")).strip().lower()
MIRROR_AGGRESSIVE_ENTRY_RELAX = float(os.getenv("PAPER_AGGRESSIVE_ENTRY_RELAX", "0.20"))
MIRROR_AGGRESSIVE_GAP_MULT = float(os.getenv("PAPER_AGGRESSIVE_GAP_MULT", "0.65"))
MIRROR_EQUITY_SEED_CAPITAL = float(os.getenv("LIVE_EQUITY_SEED_CAPITAL", "1000"))
MIRROR_MIN_LAG_PROB_EDGE = float(os.getenv("PAPER_MIN_LAG_PROB_EDGE", "0.020"))
MIRROR_MACRO_TREND_LOOKBACK_SEC = float(os.getenv("PAPER_MACRO_TREND_LOOKBACK_SEC", "900"))
MIRROR_MACRO_TREND_BLOCK_PCT = float(os.getenv("PAPER_MACRO_TREND_BLOCK_PCT", "0.040"))
MIRROR_LIVE_MIN_BET = float(os.getenv("LIVE_MIN_BET", "1.00"))
MIRROR_ENABLE_EARLY_EXIT = os.getenv("PAPER_ENABLE_EARLY_EXIT", "true").lower() == "true"
MIRROR_EARLY_EXIT_MIN_ELAPSED_SEC = float(os.getenv("PAPER_EARLY_EXIT_MIN_ELAPSED_SEC", "25"))
MIRROR_EARLY_EXIT_OPPOSITE_ASK = float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_ASK", "0.78"))
MIRROR_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT", "-20.0")
)
MIRROR_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS = int(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS", "3"))
MIRROR_EARLY_EXIT_STOP_LOSS_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT", "-40.0"))
MIRROR_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC", "35"))
MIRROR_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF", "0.75")
)
MIRROR_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC", "20")
)
MIRROR_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF", "0.60")
)
MIRROR_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT", "15")
)
MIRROR_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE = (
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE", "true").lower() == "true"
)
MIRROR_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT", "0.090")
)
MIRROR_EARLY_EXIT_MAX_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_MAX_HOLD_SEC", "220"))
MIRROR_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC = float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC", "20"))
MIRROR_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT", "-8.0"))
MIRROR_EARLY_EXIT_TRAILING_STOP_DROP_PCT = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_DROP_PCT", "30.0"))
MIRROR_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT", "15.0"))
MIRROR_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC", "35"))
MIRROR_EARLY_EXIT_PROFIT_TAKE_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_ROI_PCT", "60.0"))
MIRROR_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC", "35"))
MIRROR_TIME_WEIGHTED_EXIT = os.getenv("PAPER_TIME_WEIGHTED_EXIT", "true").lower() == "true"
MIRROR_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA = float(os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA", "0.10"))
MIRROR_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT = float(os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT", "18.0"))
MIRROR_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT = float(os.getenv("PAPER_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT", "12.0"))
MIRROR_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT = float(os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT", "14.0"))
MIRROR_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT = float(os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT", "18.0"))
MIRROR_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT = float(os.getenv("PAPER_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT", "25.0"))
MIRROR_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT = float(os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT", "0.90"))
MIRROR_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT = float(os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT", "0.020"))
MIRROR_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC = float(os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC", "60"))
MIRROR_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT", "-8.0")
)
MIRROR_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC", "135")
)
MIRROR_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT", "1.35")
)
MIRROR_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT", "0.060")
)
MIRROR_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC", "140")
)
MIRROR_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT", "95")
)
MIRROR_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC", "120")
)
MIRROR_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT", "90")
)
MIRROR_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC", "115")
)
MIRROR_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT", "110")
)


def _live_equity_snapshot(conn, initial_capital: float) -> tuple[float, float]:
    closed_pnl_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(pnl), 0) FROM live_trades WHERE status='CLOSED'",
    )
    open_notional_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(stake), 0) FROM live_trades WHERE status='OPEN'",
    )
    closed_pnl = float(closed_pnl_row[0] or 0.0) if closed_pnl_row else 0.0
    open_notional = float(open_notional_row[0] or 0.0) if open_notional_row else 0.0
    realized_equity = float(initial_capital) + closed_pnl
    available_equity = max(0.0, realized_equity - open_notional)
    return realized_equity, available_equity


def _live_recent_risk_state(conn) -> tuple[int, float]:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl,
                  COALESCE(closed_at, window_end) AS closed_ts
           FROM live_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END DESC,
             id DESC
           LIMIT 6""",
    )
    if not rows:
        return 0, 0.0

    loss_streak = 0
    for row in rows:
        pnl = float(row.get("pnl") or 0.0)
        if pnl < 0.0:
            loss_streak += 1
        else:
            break

    # Time-decayed loss rate: trades older than 6h count less,
    # trades older than 24h barely count — prevents stale losses
    # from keeping the bot overly conservative.
    now = time.time()
    _HALF_LIFE_SEC = 6 * 3600  # 6 hours half-life
    weighted_losses = 0.0
    total_weight = 0.0
    for row in rows:
        age = max(0.0, now - float(row.get("closed_ts") or now))
        weight = 2.0 ** (-age / _HALF_LIFE_SEC)  # exponential decay
        total_weight += weight
        if float(row.get("pnl") or 0.0) < 0.0:
            weighted_losses += weight
    loss_rate = (weighted_losses / total_weight) if total_weight > 0 else 0.0
    return loss_streak, loss_rate


def _live_recent_performance(conn, limit: int) -> tuple[int, float, float]:
    lim = max(1, int(limit))
    rows = fetch_all_dicts(
        conn,
        """SELECT won, pnl
           FROM live_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END DESC,
             id DESC
           LIMIT ?""",
        (lim,),
    )
    if not rows:
        return 0, 0.0, 0.0
    wins = sum(1 for row in rows if int(row.get("won") or 0) == 1)
    win_rate = wins / float(len(rows))
    pnl_sum = sum(float(row.get("pnl") or 0.0) for row in rows)
    return len(rows), win_rate, pnl_sum


def _live_last_opened_at(conn) -> float:
    row = fetch_one(conn, "SELECT MAX(opened_at) FROM live_trades")
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except Exception:
        return 0.0


def _live_last_closed_at(conn) -> float:
    row = fetch_one(
        conn,
        """SELECT MAX(
               CASE
                 WHEN closed_at IS NOT NULL THEN closed_at
                 ELSE window_end
               END
             )
           FROM live_trades
           WHERE status='CLOSED'""",
    )
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except Exception:
        return 0.0


def _live_stale_relax_factor(last_opened_at: float, now_ts: float) -> float:
    if last_opened_at <= 0:
        return 0.0
    idle_sec = max(0.0, float(now_ts) - float(last_opened_at))
    start_sec = max(0.0, MIRROR_STALE_RELAX_START_SEC)
    full_sec = max(start_sec + 1.0, MIRROR_STALE_RELAX_FULL_SEC)
    if idle_sec <= start_sec:
        return 0.0
    span = full_sec - start_sec
    return _clamp((idle_sec - start_sec) / span, 0.0, 1.0)


def _live_equity_drawdown_pct(conn, initial_capital: float) -> float:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl
           FROM live_trades
           WHERE status='CLOSED'
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END ASC,
             id ASC""",
    )
    if not rows:
        return 0.0

    equity = float(initial_capital)
    peak = float(initial_capital)
    max_dd = 0.0
    for row in rows:
        equity += float(row.get("pnl") or 0.0)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _live_window_sample_counts(conn, window_start: int, now_ts: float) -> tuple[int, int]:
    tick_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?""",
        (float(window_start), float(now_ts)),
    )
    odds_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM poly_odds
           WHERE window_start = ? AND ts <= ?""",
        (int(window_start), float(now_ts)),
    )
    tick_cnt = int(tick_row[0] or 0) if tick_row else 0
    odds_cnt = int(odds_row[0] or 0) if odds_row else 0
    return tick_cnt, odds_cnt


def _live_macro_trend_pct(conn, now_ts: float, lookback_sec: float = 900.0) -> float | None:
    """BTC move % over the last *lookback_sec* seconds, crossing window boundaries."""
    lo_ts = max(0.0, float(now_ts) - max(60.0, float(lookback_sec)))
    hi_ts = float(now_ts)
    first_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    last_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts DESC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    if not first_row or not last_row:
        return None
    try:
        p0 = float(first_row[0])
        p1 = float(last_row[0])
        if p0 <= 0.0:
            return None
        return ((p1 - p0) / p0) * 100.0
    except Exception:
        return None


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def _resample_ticks_fixed_interval(
    ticks: list,
    interval_sec: float,
    max_points: int,
) -> tuple[list[float], list[float]]:
    """
    Convert irregular tick timestamps into a fixed-interval close series.
    Missing buckets are forward-filled from the latest observed tick.
    """
    if not ticks:
        return ([], [])
    interval = max(0.2, float(interval_sec))
    max_keep = max(10, int(max_points))

    # Bucket by integer index to avoid float-key precision issues.
    bucket_last_price: dict[int, float] = {}
    for t in ticks:
        try:
            ts = float(getattr(t, "timestamp"))
            px = float(getattr(t, "price"))
        except Exception:
            continue
        if px <= 0.0:
            continue
        idx = int(math.floor(ts / interval))
        bucket_last_price[idx] = px

    if not bucket_last_price:
        return ([], [])

    start_idx = min(bucket_last_price.keys())
    end_idx = max(bucket_last_price.keys())
    prices: list[float] = []
    timestamps: list[float] = []
    last_px: float | None = None

    for idx in range(start_idx, end_idx + 1):
        px = bucket_last_price.get(idx)
        if px is not None:
            last_px = float(px)
        if last_px is None:
            continue
        timestamps.append(float(idx) * interval)
        prices.append(last_px)

    if len(prices) > max_keep:
        prices = prices[-max_keep:]
        timestamps = timestamps[-max_keep:]

    return (prices, timestamps)


class TradingBot:
    def __init__(self):
        self.price_feed = BinancePriceFeed()
        self.poly_client = PolymarketClient()
        self.jury = Jury(threshold=config.trading.jury_threshold)
        self.risk_mgr = RiskManager()  # equity synced from real balance in _refresh_adaptive_balance_cap
        self.position_mode = _normalize_position_mode(config.trading.position_mode)
        self.live_sizing_mode = _normalize_sizing_mode(config.trading.live_sizing_mode)
        self.live_profit_mode = _normalize_profit_mode(config.trading.live_profit_mode)

        self.current_market: Optional[MarketInfo] = None
        self.current_trade: Optional[TradeRecord] = None
        self.current_trade_window_start: Optional[int] = None
        self.current_trade_signal_confidence: Optional[float] = None
        self.current_trade_signal_reason: Optional[str] = None
        self.current_trade_entry_source: Optional[str] = None
        self._trade_locked_window_start: Optional[int] = None
        self.market_start_price: Optional[float] = None
        self._market_start_official: bool = False
        self._market_start_source: str = "none"
        self._ptb_scrape_done: bool = False
        self._last_ptb_sync_ts: float = 0.0
        self.recent_results: list[str] = []
        self._kill_switch_reason: Optional[str] = None

        self._running = False
        self._check_interval = 0.5  # 500ms - fast enough to catch odds lag
        self._odds_task: Optional[asyncio.Task] = None
        self._last_odds_fetch: float = 0.0
        self._state_conn = None
        self._adaptive_balance_cap: Optional[float] = None
        self._last_balance_refresh_ts: float = 0.0
        self._balance_refresh_sec = max(
            10.0,
            float(config.trading.live_balance_refresh_seconds),
        )
        self._maintenance_guard_enabled = bool(
            (not config.trading.dry_run)
            and getattr(config.trading, "live_maintenance_guard_enabled", True)
        )
        self._maintenance_fail_streak: int = 0
        self._maintenance_recover_streak: int = 0
        self._maintenance_mode: bool = False
        self._maintenance_next_probe_ts: float = 0.0
        self._maintenance_last_reason: str = ""
        self._maintenance_last_skip_log_ts: float = 0.0
        self._last_auto_claim_ts: float = 0.0
        self._early_exit_opposite_hits: dict[int, int] = {}
        self._early_exit_peak_roi: dict[int, float] = {}
        self._pending_settlement_exit: Optional[dict[str, Any]] = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._telegram_warned_not_ready: bool = False
        # Retry state: when an entry order is rejected by the API (not by gates),
        # remember the signal so we can retry on subsequent ticks without waiting
        # for a new signal.  Cleared on new window, successful fill, or kill-switch.
        self._pending_entry_retry: Optional[dict[str, Any]] = None

    def _activate_maintenance_pause(
        self,
        *,
        reason: str,
        now_ts: float,
        probe_sec: Optional[float] = None,
    ):
        if not self._maintenance_guard_enabled:
            return
        probe = (
            max(15.0, float(probe_sec))
            if probe_sec is not None
            else max(
                30.0,
                float(getattr(config.trading, "live_maintenance_probe_interval_seconds", 300.0)),
            )
        )
        self._maintenance_mode = True
        self._maintenance_last_reason = str(reason or "maintenance pause")
        self._maintenance_next_probe_ts = float(now_ts) + probe
        self._maintenance_recover_streak = 0
        self._maintenance_last_skip_log_ts = 0.0
        logger.error(
            "Live maintenance pause ON: %s | next probe in %.0fs",
            self._maintenance_last_reason,
            probe,
        )

    def _record_market_data_failure(self, *, reason: str, now_ts: float):
        if not self._maintenance_guard_enabled:
            return
        self._maintenance_fail_streak += 1
        self._maintenance_recover_streak = 0
        threshold = max(1, int(getattr(config.trading, "live_maintenance_fail_threshold", 6)))
        probe_sec = max(
            30.0,
            float(getattr(config.trading, "live_maintenance_probe_interval_seconds", 300.0)),
        )

        if self._maintenance_mode:
            self._maintenance_last_reason = str(reason or self._maintenance_last_reason)
            self._maintenance_next_probe_ts = float(now_ts) + probe_sec
            logger.warning(
                "Live maintenance probe failed (%s). Next probe in %.0fs.",
                self._maintenance_last_reason or "unknown reason",
                probe_sec,
            )
            return

        if self._maintenance_fail_streak >= threshold:
            self._maintenance_mode = True
            self._maintenance_last_reason = str(reason or "market data unavailable")
            self._maintenance_next_probe_ts = float(now_ts) + probe_sec
            logger.error(
                "Live maintenance pause ON after %s consecutive data failures: %s | next probe in %.0fs",
                int(self._maintenance_fail_streak),
                self._maintenance_last_reason,
                probe_sec,
            )
            return

        # Avoid excessive log spam while still exposing instability.
        if self._maintenance_fail_streak in (1, 3) or (self._maintenance_fail_streak % 5 == 0):
            logger.warning(
                "Live data refresh failure streak: %s/%s (%s)",
                int(self._maintenance_fail_streak),
                threshold,
                str(reason or "unknown"),
            )

    def _record_market_data_success(self):
        if not self._maintenance_guard_enabled:
            return
        if not self._maintenance_mode:
            self._maintenance_fail_streak = 0
            self._maintenance_recover_streak = 0
            return

        self._maintenance_recover_streak += 1
        need = max(1, int(getattr(config.trading, "live_maintenance_recover_success_count", 1)))
        if self._maintenance_recover_streak < need:
            logger.warning(
                "Live maintenance recovery progress: %s/%s successful probes",
                int(self._maintenance_recover_streak),
                need,
            )
            return

        logger.warning(
            "Live maintenance pause OFF: market data recovered (success probes=%s). Trading resumed.",
            int(self._maintenance_recover_streak),
        )
        self._maintenance_mode = False
        self._maintenance_fail_streak = 0
        self._maintenance_recover_streak = 0
        self._maintenance_next_probe_ts = 0.0
        self._maintenance_last_reason = ""
        self._maintenance_last_skip_log_ts = 0.0

    async def _probe_live_market_data_health(self, current_start: int) -> bool:
        """
        Probe Polymarket health for live trading recovery.
        Returns True only if we can resolve the current market and refresh orderbook odds.
        """
        market = self.current_market
        if market is None or int(market.start_timestamp) != int(current_start):
            market = await self.poly_client.find_market(int(current_start))
            if market is not None:
                self.current_market = market

        if market is None:
            return False

        if not (market.up_token_id and market.down_token_id):
            refreshed_market = await self.poly_client.find_market(int(current_start))
            if refreshed_market is None:
                return False
            self.current_market = refreshed_market
            market = refreshed_market
            if not (market.up_token_id and market.down_token_id):
                return False

            # Restart odds polling with recovered token ids.
            self.poly_client.stop_odds_polling()
            if self._odds_task:
                self._odds_task.cancel()
                self._odds_task = None
            self._odds_task = asyncio.create_task(
                self.poly_client.start_odds_polling(market, interval=1.0)
            )

        ok = await self.poly_client.refresh_odds(market)
        if not ok:
            return False
        if _safe_prob(market.up_best_ask) is None or _safe_prob(market.down_best_ask) is None:
            return False
        reason = str(self._maintenance_last_reason or "").lower()
        if "entry-uncertain" in reason or "open orders remain" in reason or "uncertain fill" in reason:
            return await self._reconcile_exchange_for_current_market("maintenance probe")
        return True

    def _log_rejected_live(self, decision, ctx, sec_elapsed, reason_tag: str, message: str):
        """Log a rejected live entry for debugging."""
        logger.info("Skip live entry (%s): %s", reason_tag, message)

    def _spawn_background_task(self, coro: asyncio.Future):
        try:
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)
            task.add_done_callback(lambda t: self._bg_tasks.discard(t))
        except Exception:
            pass

    def _telegram_ready(self) -> tuple[bool, str, str]:
        enabled = bool(getattr(config.trading, "live_telegram_enabled", False))
        token = str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
        chat_id = str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
        ready = bool(enabled and token and chat_id)
        if enabled and not ready and not self._telegram_warned_not_ready:
            self._telegram_warned_not_ready = True
            logger.warning(
                "Live Telegram enabled, but config is incomplete (token/chat_id missing)."
            )
        if ready:
            self._telegram_warned_not_ready = False
        return ready, token, chat_id

    async def _send_live_telegram(self, text: str, *, reason: str):
        ready, token, chat_id = self._telegram_ready()
        if not ready:
            return
        try:
            result = await asyncio.to_thread(
                send_telegram_message,
                token=token,
                chat_id=chat_id,
                text=text,
                timeout=8.0,
                auto_resolve_chat=False,
            )
            if not bool(result.get("ok")):
                logger.warning(
                    "Live Telegram send failed (%s): %s",
                    reason,
                    result.get("error") or "unknown",
                )
        except Exception as e:
            logger.warning("Live Telegram send exception (%s): %s", reason, e)

    def _format_live_open_telegram(
        self,
        *,
        trade: TradeRecord,
        direction: str,
        source: str,
        signal_confidence: Optional[float],
        recovered_reason: Optional[str] = None,
    ) -> str:
        stake = float(trade.amount or 0.0)
        entry_px = float(trade.price or 0.0)
        to_win_total = (stake / entry_px) if (stake > 0.0 and 0.0 < entry_px < 1.0) else 0.0
        expected_pnl = max(0.0, to_win_total - stake)
        start_price = float(self.market_start_price or 0.0)
        current_price = float(self.price_feed.current_price or 0.0)
        up_ask = _safe_prob(self.current_market.up_best_ask if self.current_market else None)
        down_ask = _safe_prob(self.current_market.down_best_ask if self.current_market else None)
        slug = (self.current_market.slug if self.current_market else None) or "--"
        ts_utc = datetime.now(timezone.utc).isoformat()
        conf_txt = (
            f"{float(signal_confidence):.3f}"
            if signal_confidence is not None
            else (
                f"{float(self.current_trade_signal_confidence):.3f}"
                if self.current_trade_signal_confidence is not None
                else "--"
            )
        )
        recovered_txt = str(recovered_reason or "").strip()
        recovery_line = f"recovery: {recovered_txt}\n" if recovered_txt else ""
        return (
            "[LIVE OPEN]\n"
            f"time(UTC): {ts_utc}\n"
            f"side: {direction}\n"
            f"slug: {slug}\n"
            f"source: {source}\n"
            f"{recovery_line}"
            f"5m start price: {start_price:,.2f}\n"
            f"current price: {current_price:,.2f}\n"
            f"stake: ${stake:,.2f}\n"
            f"entry odds: {entry_px:.3f}\n"
            f"Polymarket Buy Odds ask (UP/DOWN): "
            f"{up_ask if up_ask is not None else '--'} / {down_ask if down_ask is not None else '--'}\n"
            f"expected to-win total: ${to_win_total:,.2f}\n"
            f"expected pnl: ${expected_pnl:,.2f}\n"
            f"confidence: {conf_txt}"
        )

    async def _log_live_exposure_snapshot(self, phase: str):
        if config.trading.dry_run or self.current_market is None:
            return
        try:
            exposure = await self.poly_client.inspect_market_exposure(self.current_market)
        except Exception as e:
            logger.warning("%s: exposure snapshot failed: %s", phase, e)
            return
        if not bool(exposure.get("ok")):
            logger.warning("%s: exposure snapshot error: %s", phase, exposure.get("error") or "unknown")
            return
        logger.warning(
            "%s: exposure snapshot up_balance=%.6f down_balance=%.6f up_orders=%s down_orders=%s total_orders=%s",
            phase,
            float(exposure.get("up_balance") or 0.0),
            float(exposure.get("down_balance") or 0.0),
            int(exposure.get("up_open_orders") or 0),
            int(exposure.get("down_open_orders") or 0),
            int(exposure.get("open_orders_total") or 0),
        )

    def _format_live_closed_telegram(
        self,
        *,
        trade: TradeRecord,
        actual_outcome: str,
        close_reason: str,
        start_price: float,
        end_price: float,
        btc_exit_price: float | None = None,
    ) -> str:
        stake = float(trade.amount or 0.0)
        entry_px = float(trade.price or 0.0)
        pnl = float(trade.pnl or 0.0)
        roi_pct = ((pnl / stake) * 100.0) if stake > 0.0 else 0.0
        result = str(trade.result or "UNKNOWN").upper()
        slug = (self.current_market.slug if self.current_market else None) or "--"
        ts_utc = datetime.now(timezone.utc).isoformat()
        btc_exit_str = f"{float(btc_exit_price):,.2f}" if btc_exit_price is not None and btc_exit_price > 0 else "--"
        return (
            f"[LIVE CLOSED] {result}\n"
            f"time(UTC): {ts_utc}\n"
            f"side: {str(trade.direction or '').upper()}\n"
            f"slug: {slug}\n"
            f"close reason: {close_reason}\n"
            f"actual outcome: {str(actual_outcome or '--').upper()}\n"
            f"stake: ${stake:,.2f}\n"
            f"entry odds: {entry_px:.3f}\n"
            f"5m start/end(Binance): {float(start_price):,.2f} / {float(end_price):,.2f}\n"
            f"BTC at exit: {btc_exit_str}\n"
            f"realized pnl: ${pnl:,.2f} ({roi_pct:+.2f}%)"
        )

    def _current_live_cap(self) -> float:
        if self.live_sizing_mode == "ADAPTIVE" and not config.trading.dry_run:
            if self._adaptive_balance_cap is not None:
                return max(0.0, float(self._adaptive_balance_cap))
        return max(0.0, float(config.trading.max_bet_size))

    async def _refresh_adaptive_balance_cap(
        self,
        *,
        force: bool = False,
        reason: str = "periodic",
    ):
        if config.trading.dry_run:
            return
        if self.live_sizing_mode != "ADAPTIVE":
            return
        now = float(time.time())
        if not force and (now - self._last_balance_refresh_ts) < self._balance_refresh_sec:
            return

        balance = await self.poly_client.get_collateral_balance()
        self._last_balance_refresh_ts = now
        if balance is None:
            return

        balance = max(0.0, float(balance))
        prev = self._adaptive_balance_cap
        self._adaptive_balance_cap = balance
        # Sync RiskManager equity with real on-chain balance
        self.risk_mgr.equity = balance
        if prev is None:
            logger.info(
                "Adaptive live balance cap initialized (%s): $%.2f",
                reason,
                balance,
            )
            return

        delta = abs(balance - float(prev))
        rel = delta / max(float(prev), 1e-9)
        if force or delta >= 0.5 or rel >= 0.05:
            logger.info(
                "Adaptive live balance cap refresh (%s): $%.2f -> $%.2f",
                reason,
                float(prev),
                balance,
            )

    def _compute_entry_bet_size(
        self,
        confidence: float,
        edge: float,
        *,
        expected_roi: float | None = None,
        model_prob: float | None = None,
        entry_price: float | None = None,
    ) -> float:
        """Adaptive bet sizing: 5-15% of real balance based on conviction.
        Same logic as RiskManager.compute_bet_size / paper_trade_sim."""
        if self.live_sizing_mode == "FIXED":
            fixed = float(config.trading.max_bet_size)
            if fixed <= 0.0:
                return 0.0
            return round(
                max(float(config.trading.min_bet_size), fixed),
                2,
            )

        # Use real on-chain balance as equity
        equity = self._current_live_cap()
        if equity <= 0.0:
            return 0.0

        # Conviction = edge * confidence, typical 0.01-0.15
        conviction = _clamp(float(edge), 0.0, 0.30) * _clamp(float(confidence), 0.0, 1.0)
        # Normalize: 0.02 = weak, 0.12+ = very strong
        conv_norm = _clamp((conviction - 0.02) / 0.10, 0.0, 1.0)

        # Lerp 5%-15% of equity
        BET_PCT_MIN = 0.05
        BET_PCT_MAX = 0.15
        bet_pct = BET_PCT_MIN + conv_norm * (BET_PCT_MAX - BET_PCT_MIN)
        bet = equity * bet_pct

        # Reduce on losing streak
        if self.risk_mgr.consecutive_losses >= 2:
            reduction = 0.5 ** (self.risk_mgr.consecutive_losses - 1)
            bet *= reduction

        # Cap at 20% of equity (matched to paper/backtest)
        # Floor at LIVE_MIN_BET to avoid tiny bets where fees eat the edge
        live_min = max(float(config.trading.min_bet_size), float(MIRROR_LIVE_MIN_BET))
        bet = _clamp(bet, live_min, equity * 0.20)
        return round(float(bet), 2)

    def _estimate_fast_lane_prob_up(self, ctx: MarketContext) -> float | None:
        n = min(len(ctx.recent_prices), len(ctx.recent_timestamps))
        if n < 6:
            return None
        if ctx.market_start_price <= 0.0 or ctx.current_binance_price <= 0.0:
            return None

        now_ts = float(ctx.recent_timestamps[n - 1])
        lookback = max(15.0, float(config.trading.fast_lane_vol_lookback_sec))
        min_ts = now_ts - lookback

        prices: list[float] = []
        timestamps: list[float] = []
        for i in range(n):
            try:
                ts = float(ctx.recent_timestamps[i])
                px = float(ctx.recent_prices[i])
            except Exception:
                continue
            if ts < min_ts or px <= 0.0:
                continue
            prices.append(px)
            timestamps.append(ts)

        if len(prices) < 6:
            return None

        dlogs: list[float] = []
        dts: list[float] = []
        for i in range(1, len(prices)):
            dt = float(timestamps[i] - timestamps[i - 1])
            if dt <= 1e-6:
                continue
            ratio = float(prices[i] / max(prices[i - 1], 1e-12))
            if ratio <= 0.0:
                continue
            dlogs.append(math.log(ratio))
            dts.append(dt)

        if len(dlogs) < 4:
            return None

        total_dt = float(sum(dts))
        if total_dt <= 1e-6:
            return None

        mu = float(sum(dlogs) / total_dt)
        resid_sq = 0.0
        for i in range(len(dlogs)):
            dt = max(dts[i], 1e-6)
            err = float(dlogs[i] - (mu * dt))
            resid_sq += (err * err) / dt
        var = float(resid_sq / max(len(dlogs), 1))
        sigma = math.sqrt(max(var, 1e-12))

        t = max(1.0, float(ctx.seconds_remaining))
        x = math.log(float(ctx.current_binance_price) / float(ctx.market_start_price))
        drift_weight = _clamp(float(config.trading.fast_lane_drift_weight), 0.0, 2.0)
        drift = _clamp((mu - 0.5 * sigma * sigma) * t * drift_weight, -0.0035, 0.0035)
        denom = max(sigma * math.sqrt(t), 1e-8)
        z = _clamp((x + drift) / denom, -8.0, 8.0)
        return _clamp(_normal_cdf(z), 0.001, 0.999)

    def _evaluate_fast_lane_signal(
        self,
        ctx: MarketContext,
        now_ts: float,
    ) -> dict[str, float | str] | None:
        if not bool(config.trading.fast_lane_enabled):
            return None

        elapsed = float(ctx.seconds_elapsed)
        remaining = float(ctx.seconds_remaining)
        if elapsed < float(config.trading.fast_lane_min_seconds_elapsed):
            return None
        if elapsed > float(config.trading.fast_lane_max_seconds_elapsed):
            return None
        if remaining < float(config.trading.fast_lane_min_seconds_remaining):
            return None

        start_price = float(ctx.market_start_price)
        current_price = float(ctx.current_binance_price)
        if start_price <= 0.0 or current_price <= 0.0:
            return None

        move_pct = ((current_price - start_price) / start_price) * 100.0
        abs_move_pct = abs(move_pct)
        if abs_move_pct < float(config.trading.fast_lane_min_move_pct):
            return None
        if abs_move_pct > float(config.trading.fast_lane_max_move_pct):
            return None

        direction = "UP" if move_pct > 0.0 else "DOWN"
        recent_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=float(now_ts),
            lookback_sec=float(config.trading.fast_lane_recent_lookback_sec),
        )
        if recent_move is None:
            return None
        min_recent = float(config.trading.fast_lane_min_recent_move_pct)
        if direction == "UP" and recent_move < min_recent:
            return None
        if direction == "DOWN" and recent_move > -min_recent:
            return None
        trend_move = _recent_move_pct(
            prices=list(ctx.recent_prices),
            timestamps=list(ctx.recent_timestamps),
            now_ts=float(now_ts),
            lookback_sec=float(config.trading.live_trend_align_lookback_sec),
        )
        if trend_move is None:
            return None
        trend_thr = abs(float(config.trading.live_trend_align_max_opposing_move_pct))
        if direction == "UP" and trend_move < -trend_thr:
            return None
        if direction == "DOWN" and trend_move > trend_thr:
            return None

        up_ask = _safe_prob(ctx.poly_up_ask) or _safe_prob(ctx.poly_up_price)
        down_ask = _safe_prob(ctx.poly_down_ask) or _safe_prob(ctx.poly_down_price)
        side_ask = up_ask if direction == "UP" else down_ask
        if side_ask is None or not (0.01 < side_ask < 0.99):
            return None
        if side_ask > float(config.trading.fast_lane_max_entry_price):
            return None
        opposite_ask = down_ask if direction == "UP" else up_ask
        market_up_prob, market_down_prob = _normalized_market_probs(up_ask, down_ask)
        if market_up_prob is None or market_down_prob is None:
            return None

        p_up = self._estimate_fast_lane_prob_up(ctx)
        if p_up is None:
            move_scale = max(float(config.trading.fast_lane_min_move_pct), 1e-6)
            z = _clamp((move_pct / move_scale) * 0.60, -8.0, 8.0)
            p_up = _clamp(_normal_cdf(z), 0.001, 0.999)
        p_dir = p_up if direction == "UP" else (1.0 - p_up)
        p_dir = _clamp(float(p_dir), 0.001, 0.999)
        market_dir_prob = float(market_up_prob) if direction == "UP" else float(market_down_prob)
        lag_prob_edge = float(p_dir - market_dir_prob)
        if lag_prob_edge < float(config.trading.fast_lane_min_lag_prob_edge):
            return None
        if p_dir < float(config.trading.fast_lane_min_direction_prob):
            return None

        prob_edge = float(p_dir - side_ask)
        if prob_edge < float(config.trading.fast_lane_min_prob_edge):
            return None

        fee_rate = max(0.0, float(config.trading.fee_rate))
        expected_roi = float((p_dir / side_ask) - 1.0 - fee_rate)
        if expected_roi < float(config.trading.fast_lane_min_expected_roi):
            return None

        confidence = _clamp(
            0.45 + (p_dir - 0.5) * 1.20 + prob_edge * 0.50,
            0.0,
            1.0,
        )
        if side_ask is not None and opposite_ask is not None:
            contra_gap = float(opposite_ask) - float(side_ask)
            if contra_gap > float(config.trading.live_max_contra_gap):
                if not (
                    p_dir >= float(config.trading.live_contra_override_min_model_prob)
                    and confidence >= float(config.trading.live_contra_override_min_conf)
                ):
                    return None
        reason = (
            f"fast_lane: move={move_pct:+.4f}% recent={recent_move:+.4f}% "
            f"p={p_dir:.3f} mkt_p={market_dir_prob:.3f} lag={lag_prob_edge:+.3f} "
            f"ask={side_ask:.3f} net_ev={expected_roi:+.3%}"
        )

        return {
            "direction": direction,
            "confidence": float(confidence),
            "prob_edge": float(prob_edge),
            "entry_price": float(side_ask),
            "expected_roi": float(expected_roi),
            "direction_prob": float(p_dir),
            "market_prob_dir": float(market_dir_prob),
            "lag_prob_edge": float(lag_prob_edge),
            "move_pct": float(move_pct),
            "recent_move_pct": float(recent_move),
            "reason": reason,
        }

    def _ensure_state_conn(self):
        if self._state_conn is not None:
            return self._state_conn
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
        self._state_conn = conn
        return conn

    def _close_state_conn(self):
        if self._state_conn is None:
            return
        try:
            self._state_conn.close()
        except Exception:
            pass
        self._state_conn = None

    def _persist_runtime_state(self):
        try:
            conn = self._ensure_state_conn()
            risk_payload = {
                "daily_pnl": float(self.risk_mgr.daily_pnl),
                "consecutive_losses": int(self.risk_mgr.consecutive_losses),
                "cooldown_until": float(self.risk_mgr.cooldown_until),
                "daily_reset_time": float(self.risk_mgr.daily_reset_time),
            }
            trade_payload = None
            if self.current_trade is not None and self.current_trade.result == "PENDING":
                trade_payload = {
                    "timestamp": float(self.current_trade.timestamp),
                    "direction": str(self.current_trade.direction),
                    "amount": float(self.current_trade.amount),
                    "price": float(self.current_trade.price),
                    "result": str(self.current_trade.result),
                    "window_start": int(self.current_trade_window_start or 0),
                    "market_start_price": float(self.market_start_price or 0.0),
                    "signal_confidence": (
                        float(self.current_trade_signal_confidence)
                        if self.current_trade_signal_confidence is not None
                        else None
                    ),
                    "signal_reason": (
                        str(self.current_trade_signal_reason)
                        if self.current_trade_signal_reason
                        else None
                    ),
                    "entry_source": (
                        str(self.current_trade_entry_source)
                        if self.current_trade_entry_source
                        else None
                    ),
                }
            now_ts = float(time.time())
            risk_json = json.dumps(risk_payload, ensure_ascii=True)
            trade_json = json.dumps(trade_payload, ensure_ascii=True) if trade_payload else None
            kill_switch = 1 if self._kill_switch_reason else 0
            kill_reason = str(self._kill_switch_reason or "")
            sql = (
                "INSERT INTO bot_runtime_state "
                "(id, updated_at, risk_json, trade_json, kill_switch, kill_reason) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON DUPLICATE KEY UPDATE "
                "updated_at=VALUES(updated_at), "
                "risk_json=VALUES(risk_json), "
                "trade_json=VALUES(trade_json), "
                "kill_switch=VALUES(kill_switch), "
                "kill_reason=VALUES(kill_reason)"
            )
            execute_write(
                conn,
                sql,
                (now_ts, risk_json, trade_json, kill_switch, kill_reason),
            )
            conn.commit()
        except Exception as e:
            logger.warning("runtime state persist failed: %s", e)

    def _load_runtime_state(self):
        try:
            conn = self._ensure_state_conn()
            row = fetch_one_dict(
                conn,
                "SELECT risk_json, trade_json, kill_switch, kill_reason FROM bot_runtime_state WHERE id=1",
            )
            if not row:
                return

            try:
                risk_payload = json.loads(str(row.get("risk_json") or "{}"))
                self.risk_mgr.daily_pnl = float(risk_payload.get("daily_pnl") or 0.0)
                self.risk_mgr.consecutive_losses = int(risk_payload.get("consecutive_losses") or 0)
                self.risk_mgr.cooldown_until = float(risk_payload.get("cooldown_until") or 0.0)
                reset_ts = float(risk_payload.get("daily_reset_time") or 0.0)
                if reset_ts > 0.0:
                    self.risk_mgr.daily_reset_time = reset_ts
            except Exception as e:
                logger.warning("runtime risk state load failed: %s", e)

            kill_flag = bool(int(row.get("kill_switch") or 0))
            if kill_flag:
                reason = str(row.get("kill_reason") or "").strip()
                self._kill_switch_reason = reason or "latched kill-switch"

            trade_raw = row.get("trade_json")
            if trade_raw:
                try:
                    trade_payload = json.loads(str(trade_raw))
                except Exception:
                    trade_payload = None
                if isinstance(trade_payload, dict):
                    direction = str(trade_payload.get("direction") or "").upper()
                    amount = float(trade_payload.get("amount") or 0.0)
                    price = float(trade_payload.get("price") or 0.0)
                    if direction in {"UP", "DOWN"} and amount > 0.0 and 0.0 < price < 1.0:
                        self.current_trade = TradeRecord(
                            timestamp=float(trade_payload.get("timestamp") or time.time()),
                            direction=direction,
                            amount=amount,
                            price=price,
                            result="PENDING",
                            pnl=0.0,
                        )
                        ws = int(trade_payload.get("window_start") or 0)
                        self.current_trade_window_start = ws if ws > 0 else None
                        self._trade_locked_window_start = self.current_trade_window_start
                        conf = trade_payload.get("signal_confidence")
                        self.current_trade_signal_confidence = (
                            float(conf)
                            if conf is not None
                            else None
                        )
                        self.current_trade_signal_reason = (
                            str(trade_payload.get("signal_reason") or "").strip() or None
                        )
                        self.current_trade_entry_source = (
                            str(trade_payload.get("entry_source") or "").strip() or None
                        )
                        saved_start_px = float(trade_payload.get("market_start_price") or 0.0)
                        if saved_start_px > 0.0:
                            self.market_start_price = saved_start_px
                        logger.warning(
                            "Recovered pending local runtime trade: %s amount=$%.2f @ %.4f ws=%s",
                            direction,
                            amount,
                            price,
                            self.current_trade_window_start,
                        )
                        self._upsert_live_trade_open(
                            trade=self.current_trade,
                            window_start=self.current_trade_window_start,
                            signal_confidence=None,
                            signal_reason="Recovered pending trade from runtime state",
                            entry_source="runtime_recover",
                        )
        except Exception as e:
            logger.warning("runtime state load failed: %s", e)

    def _set_kill_switch(self, reason: str):
        msg = str(reason or "unknown kill-switch").strip()
        if not msg:
            msg = "unknown kill-switch"
        self._kill_switch_reason = msg
        self._running = False
        logger.error("KILL-SWITCH TRIGGERED: %s", msg)
        self._persist_runtime_state()

    def _clear_kill_switch(self):
        self._kill_switch_reason = None
        self._persist_runtime_state()

    def _resolve_window_bounds(
        self,
        *,
        window_start: Optional[int],
        trade_timestamp: Optional[float] = None,
    ) -> tuple[int, int]:
        interval = max(1, int(config.polymarket.interval_seconds))
        ws = int(window_start or 0)
        if ws <= 0 and trade_timestamp is not None and float(trade_timestamp) > 0.0:
            ws = int(float(trade_timestamp) // interval) * interval
        if ws <= 0 and self.current_market is not None:
            ws = int(self.current_market.start_timestamp)
        if ws <= 0:
            now = time.time()
            ws = int(now // interval) * interval

        if self.current_market is not None and int(self.current_market.start_timestamp) == ws:
            we = int(self.current_market.end_timestamp)
        else:
            we = int(ws + interval)
        return ws, we

    def _upsert_live_trade_open(
        self,
        *,
        trade: TradeRecord,
        window_start: Optional[int],
        signal_confidence: Optional[float],
        signal_reason: Optional[str],
        entry_source: str,
    ):
        try:
            entry_price = float(trade.price)
            stake = float(trade.amount)
            if not (0.0 < entry_price < 1.0) or stake <= 0.0:
                return
            ws, we = self._resolve_window_bounds(
                window_start=window_start,
                trade_timestamp=float(trade.timestamp),
            )
            shares = float(stake / entry_price)
            payout_multiple = float(1.0 / entry_price)
            potential_win_pnl = float(shares - stake)
            opened_at = float(trade.timestamp or time.time())
            conf = (
                max(0.0, min(1.0, float(signal_confidence)))
                if signal_confidence is not None
                else None
            )
            reason = str(signal_reason or "").strip() or None
            source = str(entry_source or "").strip() or None

            conn = self._ensure_state_conn()
            sql = (
                "INSERT INTO live_trades "
                "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                "potential_win_pnl, signal_confidence, signal_reason, entry_source, status, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?) "
                "ON DUPLICATE KEY UPDATE "
                "window_end=VALUES(window_end), "
                "direction=VALUES(direction), "
                "stake=VALUES(stake), "
                "entry_price=VALUES(entry_price), "
                "payout_multiple=VALUES(payout_multiple), "
                "shares=VALUES(shares), "
                "potential_win_pnl=VALUES(potential_win_pnl), "
                "signal_confidence=COALESCE(VALUES(signal_confidence), signal_confidence), "
                "signal_reason=COALESCE(VALUES(signal_reason), signal_reason), "
                "entry_source=COALESCE(VALUES(entry_source), entry_source), "
                "status='OPEN', "
                "opened_at=VALUES(opened_at), "
                "closed_at=NULL, "
                "actual_outcome=NULL, "
                "won=NULL, "
                "pnl=NULL, "
                "roi_pct=NULL, "
                "close_reason=NULL"
            )
            execute_write(
                conn,
                sql,
                (
                    int(ws),
                    int(we),
                    str(trade.direction),
                    float(stake),
                    float(entry_price),
                    float(payout_multiple),
                    float(shares),
                    float(potential_win_pnl),
                    conf,
                    reason,
                    source,
                    float(opened_at),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning("live trade OPEN upsert failed: %s", e)

    def _upsert_live_trade_closed(
        self,
        *,
        trade: TradeRecord,
        window_start: Optional[int],
        actual_outcome: str,
        close_reason: str,
    ):
        try:
            entry_price = float(trade.price)
            stake = float(trade.amount)
            if not (0.0 < entry_price < 1.0) or stake <= 0.0:
                return
            ws, we = self._resolve_window_bounds(
                window_start=window_start,
                trade_timestamp=float(trade.timestamp),
            )
            shares = float(stake / entry_price)
            payout_multiple = float(1.0 / entry_price)
            potential_win_pnl = float(shares - stake)
            opened_at = float(trade.timestamp or time.time())
            closed_at = float(time.time())
            pnl = float(trade.pnl)
            roi_pct = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0
            won = 1 if str(trade.result or "").upper() == "WIN" else 0
            actual = str(actual_outcome or "").upper() or None
            close = str(close_reason or "").strip() or None

            conn = self._ensure_state_conn()
            sql = (
                "INSERT INTO live_trades "
                "(window_start, window_end, direction, stake, entry_price, payout_multiple, shares, "
                "potential_win_pnl, status, opened_at, closed_at, actual_outcome, won, pnl, roi_pct, close_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?) "
                "ON DUPLICATE KEY UPDATE "
                "window_end=VALUES(window_end), "
                "direction=VALUES(direction), "
                "stake=VALUES(stake), "
                "entry_price=VALUES(entry_price), "
                "payout_multiple=VALUES(payout_multiple), "
                "shares=VALUES(shares), "
                "potential_win_pnl=VALUES(potential_win_pnl), "
                "status='CLOSED', "
                "closed_at=VALUES(closed_at), "
                "actual_outcome=VALUES(actual_outcome), "
                "won=VALUES(won), "
                "pnl=VALUES(pnl), "
                "roi_pct=VALUES(roi_pct), "
                "close_reason=VALUES(close_reason)"
            )
            execute_write(
                conn,
                sql,
                (
                    int(ws),
                    int(we),
                    str(trade.direction),
                    float(stake),
                    float(entry_price),
                    float(payout_multiple),
                    float(shares),
                    float(potential_win_pnl),
                    float(opened_at),
                    float(closed_at),
                    actual,
                    int(won),
                    float(pnl),
                    float(roi_pct),
                    close,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning("live trade CLOSED upsert failed: %s", e)

    def _reconcile_stale_open_live_trades(self):
        """
        Backfill OPEN live trades that already have resolved market outcomes.
        This handles cases where runtime stopped before normal rollover settlement.
        """
        try:
            conn = self._ensure_state_conn()
            rows = fetch_all_dicts(
                conn,
                """
                SELECT
                    lt.id, lt.window_start, lt.window_end, lt.direction,
                    lt.stake, lt.entry_price, lt.opened_at,
                    mw.actual_outcome
                FROM live_trades lt
                JOIN market_windows mw ON mw.window_start = lt.window_start
                WHERE lt.status='OPEN'
                  AND mw.actual_outcome IN ('UP','DOWN')
                ORDER BY lt.window_start ASC
                """,
            )
            if not rows:
                return

            now_ts = float(time.time())
            fixed = 0
            for row in rows:
                trade_id = int(row.get("id") or 0)
                stake = float(row.get("stake") or 0.0)
                entry_price = float(row.get("entry_price") or 0.0)
                direction = str(row.get("direction") or "").upper()
                actual = str(row.get("actual_outcome") or "").upper()
                if trade_id <= 0 or stake <= 0.0 or not (0.0 < entry_price < 1.0):
                    continue
                if direction not in {"UP", "DOWN"} or actual not in {"UP", "DOWN"}:
                    continue

                won = 1 if direction == actual else 0
                if won:
                    shares = float(stake / entry_price)
                    pnl = float(shares - stake)
                else:
                    pnl = float(-stake)
                roi_pct = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0

                execute_write(
                    conn,
                    """
                    UPDATE live_trades
                    SET status='CLOSED',
                        closed_at=?,
                        actual_outcome=?,
                        won=?,
                        pnl=?,
                        roi_pct=?,
                        close_reason=COALESCE(close_reason, 'recovered_expiry_settlement')
                    WHERE id=?
                    """,
                    (
                        float(now_ts),
                        str(actual),
                        int(won),
                        float(pnl),
                        float(roi_pct),
                        int(trade_id),
                    ),
                )
                fixed += 1

            if fixed > 0:
                conn.commit()
                logger.warning(
                    "Recovered %s stale OPEN live trade(s) from resolved market outcomes.",
                    fixed,
                )

            if self.current_trade is not None and self.current_trade.result == "PENDING":
                ws = int(self.current_trade_window_start or 0)
                if ws > 0:
                    resolved = fetch_one_dict(
                        conn,
                        "SELECT actual_outcome FROM market_windows "
                        "WHERE window_start=? AND actual_outcome IN ('UP','DOWN') "
                        "LIMIT 1",
                        (int(ws),),
                    )
                    if resolved:
                        logger.warning(
                            "Clearing stale pending runtime trade for resolved window ws=%s outcome=%s",
                            ws,
                            str(resolved.get("actual_outcome") or ""),
                        )
                        self.current_trade = None
                        self.current_trade_window_start = None
                        self.current_trade_signal_confidence = None
                        self.current_trade_signal_reason = None
                        self.current_trade_entry_source = None
                        self._trade_locked_window_start = None
                        self._early_exit_opposite_hits.clear()
                        self._early_exit_peak_roi.clear()
                        self._persist_runtime_state()
        except Exception as e:
            logger.warning("stale OPEN live trade reconcile failed: %s", e)

    async def _reconcile_exchange_for_current_market(self, phase: str) -> bool:
        if config.trading.dry_run:
            return True
        if self.current_market is None:
            return True
        phase_lower = str(phase or "").lower()
        transient_entry_reconcile = ("entry-uncertain" in phase_lower) or ("maintenance probe" in phase_lower)

        exposure = await self.poly_client.inspect_market_exposure(self.current_market)
        if not bool(exposure.get("ok")):
            if transient_entry_reconcile:
                self._activate_maintenance_pause(
                    reason=f"{phase} exposure check failed: {exposure.get('error') or 'unknown exchange error'}",
                    now_ts=float(time.time()),
                    probe_sec=30.0,
                )
                return False
            self._set_kill_switch(
                f"{phase} reconcile failed: {exposure.get('error') or 'unknown exchange error'}"
            )
            return False

        open_orders_total = int(exposure.get("open_orders_total") or 0)
        if open_orders_total > 0:
            logger.warning(
                "%s reconcile: found %s open orders; attempting cancel",
                phase,
                open_orders_total,
            )
            cancel_res = await self.poly_client.cancel_market_orders(self.current_market)
            if not bool(cancel_res.get("ok", False)):
                logger.warning("%s reconcile: cancel errors=%s", phase, cancel_res.get("errors"))
            await asyncio.sleep(0.2)
            exposure = await self.poly_client.inspect_market_exposure(self.current_market)
            open_orders_total = int(exposure.get("open_orders_total") or 0)
            if open_orders_total > 0:
                if transient_entry_reconcile:
                    self._activate_maintenance_pause(
                        reason=f"{phase}: {open_orders_total} open orders remain after cancel",
                        now_ts=float(time.time()),
                        probe_sec=30.0,
                    )
                    return False
                self._set_kill_switch(
                    f"{phase} reconcile blocked: {open_orders_total} open orders remain after cancel"
                )
                return False

        up_balance = float(exposure.get("up_balance") or 0.0)
        down_balance = float(exposure.get("down_balance") or 0.0)
        eps = 1e-9
        if up_balance > eps and down_balance > eps:
            self._set_kill_switch(
                f"{phase} reconcile blocked: both-side balances detected (UP={up_balance:.6f}, DOWN={down_balance:.6f})"
            )
            return False

        if self.current_trade is not None and self.current_trade.result == "PENDING":
            expected = str(self.current_trade.direction or "").upper()
            live_side_balance = up_balance if expected == "UP" else down_balance
            opposite_balance = down_balance if expected == "UP" else up_balance
            if opposite_balance > eps:
                self._set_kill_switch(
                    f"{phase} reconcile mismatch: opposite-side balance detected ({opposite_balance:.6f})"
                )
                return False
            if live_side_balance <= eps:
                self._set_kill_switch(
                    f"{phase} reconcile mismatch: local pending trade exists but on-exchange balance is zero"
                )
                return False
            return True

        if up_balance > eps or down_balance > eps:
            recovered_direction = "UP" if up_balance >= down_balance else "DOWN"
            recovered_shares = up_balance if recovered_direction == "UP" else down_balance
            ref_ask = (
                _safe_prob(self.current_market.up_best_ask)
                if recovered_direction == "UP"
                else _safe_prob(self.current_market.down_best_ask)
            )
            if ref_ask is None:
                ref_ask = (
                    _safe_prob(self.current_market.up_price)
                    if recovered_direction == "UP"
                    else _safe_prob(self.current_market.down_price)
                )
            if ref_ask is None:
                ref_ask = 0.5
            recovered_amount = max(
                float(config.trading.min_bet_size),
                float(recovered_shares * ref_ask),
            )
            self.current_trade = TradeRecord(
                timestamp=float(time.time()),
                direction=recovered_direction,
                amount=float(recovered_amount),
                price=float(ref_ask),
                result="PENDING",
                pnl=0.0,
            )
            self.current_trade_window_start = int(self.current_market.start_timestamp)
            self.current_trade_signal_confidence = None
            self.current_trade_signal_reason = None
            self.current_trade_entry_source = "reconcile"
            self._trade_locked_window_start = self.current_trade_window_start
            if self.market_start_price is None or self.market_start_price <= 0.0:
                self.market_start_price = self.price_feed.current_price
            logger.warning(
                "%s reconcile: recovered exchange position as pending trade "
                "(dir=%s shares=%.6f est_notional=$%.2f)",
                phase,
                recovered_direction,
                recovered_shares,
                recovered_amount,
            )
            self._upsert_live_trade_open(
                trade=self.current_trade,
                window_start=self.current_trade_window_start,
                signal_confidence=None,
                signal_reason=f"Recovered exchange position during {phase}",
                entry_source="reconcile",
            )
            self._persist_runtime_state()
        return True

    def _handle_order_result(
        self,
        result: Optional[dict],
        *,
        direction: str,
        fallback_amount: float,
        fallback_price: float,
        source: str,
        signal_confidence: Optional[float] = None,
        signal_reason: Optional[str] = None,
    ) -> bool:
        if result is None:
            self._set_kill_switch(f"{source}: order result missing (possible unknown fill)")
            return False

        if bool(result.get("uncertain_fill", False)):
            reason = str(result.get("reason") or "unknown-fill error")
            self._set_kill_switch(f"{source}: uncertain fill detected -> {reason}")
            return False

        if not bool(result.get("accepted", True)):
            logger.info(
                "%s order blocked/rejected: mode=%s status=%s reason=%s",
                source,
                result.get("mode"),
                result.get("status"),
                result.get("reason"),
            )
            return False

        if not bool(result.get("filled", False)):
            logger.info(
                "%s order not filled: mode=%s status=%s reason=%s",
                source,
                result.get("mode"),
                result.get("status"),
                result.get("reason"),
            )
            return False

        executed_notional = float(result.get("executed_notional") or 0.0)
        executed_price = float(result.get("executed_price") or 0.0)
        executed_size = float(result.get("executed_size") or 0.0)

        # Prefer exchange-reported fill size*price to keep later exit sizing aligned.
        if executed_size > 0.0 and 0.0 < executed_price < 1.0:
            executed_notional = float(executed_size * executed_price)

        if executed_notional <= 0.0:
            executed_notional = float(fallback_amount)
        if not (0.0 < executed_price < 1.0):
            executed_price = float(fallback_price)

        self.current_trade = self.risk_mgr.record_trade(
            direction=direction,
            amount=executed_notional,
            price=executed_price,
        )
        self.current_trade_window_start = (
            int(self.current_market.start_timestamp) if self.current_market is not None else None
        )
        self.current_trade_signal_confidence = (
            max(0.0, min(1.0, float(signal_confidence)))
            if signal_confidence is not None
            else None
        )
        self.current_trade_signal_reason = str(signal_reason or "").strip() or None
        self.current_trade_entry_source = str(source or "").strip() or None
        self._trade_locked_window_start = self.current_trade_window_start
        self._upsert_live_trade_open(
            trade=self.current_trade,
            window_start=self.current_trade_window_start,
            signal_confidence=self.current_trade_signal_confidence,
            signal_reason=self.current_trade_signal_reason,
            entry_source=source,
        )
        self._persist_runtime_state()
        if bool(getattr(config.trading, "live_telegram_notify_open", True)):
            msg = self._format_live_open_telegram(
                trade=self.current_trade,
                direction=direction,
                source=source,
                signal_confidence=signal_confidence,
            )
            self._spawn_background_task(
                self._send_live_telegram(msg, reason="trade_open")
            )
        return True

    async def _handle_entry_order_result(
        self,
        result: Optional[dict],
        *,
        direction: str,
        fallback_amount: float,
        fallback_price: float,
        source: str,
        signal_confidence: Optional[float] = None,
        signal_reason: Optional[str] = None,
    ) -> bool:
        if bool((result or {}).get("uncertain_fill", False)):
            recovered = await self._recover_uncertain_entry_result(
                direction=direction,
                source=source,
                signal_confidence=signal_confidence,
                signal_reason=signal_reason,
            )
            if recovered:
                return True
            # Recovery confirmed no position on exchange — order never filled.
            # Safe to skip without kill-switch; next signal will retry naturally.
            if self._maintenance_mode:
                logger.warning(
                    "%s uncertain entry unresolved; maintenance pause active, suppressing kill-switch fallback",
                    source,
                )
            else:
                logger.warning(
                    "%s uncertain entry not filled; skipping (will retry on next signal)",
                    source,
                )
            return False
        return self._handle_order_result(
            result,
            direction=direction,
            fallback_amount=fallback_amount,
            fallback_price=fallback_price,
            source=source,
            signal_confidence=signal_confidence,
            signal_reason=signal_reason,
        )

    def _save_entry_retry(
        self,
        *,
        direction: str,
        token_id: str,
        price: float,
        bet_size: float,
        source: str,
        signal_confidence: float,
        signal_reason: str,
    ):
        """Save a rejected entry order for retry on subsequent ticks.
        Retry expires after 30s or at window end (cutoff)."""
        window_start = (
            int(self.current_market.start_timestamp)
            if self.current_market is not None
            else None
        )
        self._pending_entry_retry = {
            "direction": direction,
            "token_id": token_id,
            "price": price,
            "bet_size": bet_size,
            "source": source,
            "signal_confidence": signal_confidence,
            "signal_reason": signal_reason,
            "window_start": window_start,
            "created_ts": time.time(),
            "attempts": 1,
        }
        logger.info(
            "Entry retry saved: dir=%s price=%.4f size=$%.2f source=%s (expires in 30s)",
            direction, price, bet_size, source,
        )

    async def _attempt_entry_retry(
        self, now: float, seconds_remaining: float, current_start: int
    ) -> bool:
        """Try to re-place a previously rejected entry order.
        Returns True if we should skip normal signal evaluation this tick."""
        retry = self._pending_entry_retry
        if retry is None:
            return False

        # Clear if window changed
        if retry.get("window_start") != int(current_start):
            logger.info("Entry retry expired: window changed")
            self._pending_entry_retry = None
            return False

        # Clear if older than 30 seconds
        age = now - float(retry["created_ts"])
        if age > 30.0:
            logger.info(
                "Entry retry expired: %.1fs > 30s (dir=%s price=%.4f)",
                age, retry["direction"], retry["price"],
            )
            self._pending_entry_retry = None
            return False

        # Don't retry too close to expiry
        cutoff = float(config.trading.cutoff_before_close_seconds)
        if seconds_remaining < cutoff:
            logger.info("Entry retry expired: too close to expiry (%.1fs remaining)", seconds_remaining)
            self._pending_entry_retry = None
            return False

        # Throttle: at least 2s between attempts
        last_attempt = float(retry.get("last_attempt_ts", 0.0))
        if (now - last_attempt) < 2.0:
            return True  # skip signal eval but wait for next tick

        retry["last_attempt_ts"] = now
        retry["attempts"] = int(retry.get("attempts", 0)) + 1

        # Check that the current ask price still matches (same direction, close price)
        if self.current_market is not None:
            if retry["direction"] == "UP":
                current_ask = (
                    _safe_prob(self.current_market.up_best_ask)
                    or _safe_prob(self.current_market.up_price)
                )
            else:
                current_ask = (
                    _safe_prob(self.current_market.down_best_ask)
                    or _safe_prob(self.current_market.down_price)
                )
            if current_ask is not None and abs(current_ask - retry["price"]) > 0.03:
                logger.info(
                    "Entry retry: price moved too far (saved=%.4f current=%.4f), cancelling",
                    retry["price"], current_ask,
                )
                self._pending_entry_retry = None
                return False

        logger.info(
            "Entry retry attempt #%d: dir=%s price=%.4f $%.2f (age=%.1fs)",
            retry["attempts"], retry["direction"], retry["price"],
            retry["bet_size"], age,
        )

        result = await self.poly_client.place_entry_order(
            token_id=retry["token_id"],
            side=retry["direction"],
            amount=retry["bet_size"],
            reference_ask=retry["price"],
        )
        handled = await self._handle_entry_order_result(
            result,
            direction=retry["direction"],
            fallback_amount=retry["bet_size"],
            fallback_price=retry["price"],
            source=f"{retry['source']}-retry",
            signal_confidence=retry["signal_confidence"],
            signal_reason=retry["signal_reason"],
        )
        if handled:
            logger.info("Entry retry FILLED on attempt #%d", retry["attempts"])
            self._pending_entry_retry = None
            await self._refresh_adaptive_balance_cap(force=True, reason="post_fill")
            return True

        if self._kill_switch_reason:
            self._pending_entry_retry = None
            return True

        # Still rejected — will retry on next tick
        return True

    async def _recover_uncertain_entry_result(
        self,
        *,
        direction: str,
        source: str,
        signal_confidence: Optional[float],
        signal_reason: Optional[str],
    ) -> bool:
        if config.trading.dry_run or self.current_market is None:
            return False

        attempts = 4
        delay_sec = 0.6
        no_position_count = 0
        for idx in range(attempts):
            if idx > 0:
                await asyncio.sleep(delay_sec)
            ok = await self._reconcile_exchange_for_current_market(f"{source} entry-uncertain")
            if not ok:
                await self._log_live_exposure_snapshot(f"{source} entry-uncertain reconcile-failed")
                return False
            trade = self.current_trade
            if trade is None or trade.result != "PENDING":
                no_position_count += 1
                await self._log_live_exposure_snapshot(f"{source} entry-uncertain attempt={idx + 1} no-position")
                continue
            self.current_trade_window_start = (
                int(self.current_market.start_timestamp) if self.current_market is not None else None
            )
            self.current_trade_signal_confidence = (
                max(0.0, min(1.0, float(signal_confidence)))
                if signal_confidence is not None
                else self.current_trade_signal_confidence
            )
            self.current_trade_signal_reason = str(signal_reason or "").strip() or self.current_trade_signal_reason
            self.current_trade_entry_source = str(source or "").strip() or self.current_trade_entry_source
            self._trade_locked_window_start = self.current_trade_window_start
            self._upsert_live_trade_open(
                trade=trade,
                window_start=self.current_trade_window_start,
                signal_confidence=self.current_trade_signal_confidence,
                signal_reason=self.current_trade_signal_reason,
                entry_source=self.current_trade_entry_source or source,
            )
            self._persist_runtime_state()
            logger.warning(
                "%s uncertain entry recovered via exchange reconcile (attempt=%s dir=%s amount=$%.2f @ %.4f)",
                source,
                idx + 1,
                str(trade.direction),
                float(trade.amount),
                float(trade.price),
            )
            if bool(getattr(config.trading, "live_telegram_notify_open", True)):
                msg = self._format_live_open_telegram(
                    trade=trade,
                    direction=str(trade.direction),
                    source=str(self.current_trade_entry_source or source),
                    signal_confidence=self.current_trade_signal_confidence,
                    recovered_reason="request exception -> exchange reconcile",
                )
                self._spawn_background_task(
                    self._send_live_telegram(msg, reason="trade_open_recovered")
                )
            return True
        await self._log_live_exposure_snapshot(f"{source} entry-uncertain final")
        # If ALL attempts confirmed no position on exchange, the order simply
        # never went through (API hiccup).  Safe to skip — no kill-switch needed.
        if no_position_count >= attempts:
            logger.warning(
                "%s uncertain entry confirmed NOT filled after %s attempts "
                "(balance=0, orders=0). Skipping — next signal will retry.",
                source,
                attempts,
            )
            return False
        logger.warning(
            "%s uncertain entry could not be confirmed via reconcile after %s attempts",
            source,
            attempts,
        )
        return False

    async def _maybe_clear_latched_uncertain_fill_on_startup(self) -> bool:
        reason = str(self._kill_switch_reason or "").lower()
        if "uncertain fill detected" not in reason:
            return False
        if config.trading.dry_run or self.current_market is None:
            return False
        try:
            exposure = await self.poly_client.inspect_market_exposure(self.current_market)
        except Exception as e:
            logger.warning("Startup uncertain-fill recovery exposure check failed: %s", e)
            return False
        if not bool(exposure.get("ok")):
            logger.warning(
                "Startup uncertain-fill recovery exposure error: %s",
                exposure.get("error") or "unknown",
            )
            return False

        eps = 1e-9
        up_balance = float(exposure.get("up_balance") or 0.0)
        down_balance = float(exposure.get("down_balance") or 0.0)
        open_orders_total = int(exposure.get("open_orders_total") or 0)

        if self.current_trade is not None and self.current_trade.result == "PENDING":
            logger.warning(
                "Clearing latched uncertain-fill kill-switch: recovered pending trade exists "
                "(dir=%s amount=$%.2f @ %.4f)",
                str(self.current_trade.direction),
                float(self.current_trade.amount),
                float(self.current_trade.price),
            )
            self._clear_kill_switch()
            return True

        if open_orders_total == 0 and up_balance <= eps and down_balance <= eps:
            logger.warning(
                "Clearing latched uncertain-fill kill-switch: no residual exposure/open orders remain"
            )
            self._clear_kill_switch()
            return True

        logger.warning(
            "Latched uncertain-fill still requires review: up_balance=%.6f down_balance=%.6f total_orders=%s",
            up_balance,
            down_balance,
            open_orders_total,
        )
        return False

    async def _cap_exit_shares_by_balance(
        self,
        *,
        market: Optional[MarketInfo],
        direction: str,
        requested_shares: float,
        phase: str,
    ) -> float:
        shares = max(0.0, float(requested_shares or 0.0))
        if shares <= 0.0 or market is None or config.trading.dry_run:
            return shares

        exposure = await self.poly_client.inspect_market_exposure(market)
        if not bool(exposure.get("ok")):
            logger.warning(
                "%s: exposure check failed, using requested shares %.6f (%s)",
                phase,
                shares,
                exposure.get("error"),
            )
            return shares

        side = str(direction or "").upper()
        available = 0.0
        if side == "UP":
            available = float(exposure.get("up_balance") or 0.0)
        elif side == "DOWN":
            available = float(exposure.get("down_balance") or 0.0)

        if available <= 0.0:
            logger.warning("%s: no on-exchange %s balance available (requested=%.6f)", phase, side, shares)
            return 0.0

        capped = min(shares, max(0.0, available * 0.995))
        if capped + 1e-9 < shares:
            logger.warning(
                "%s: capping exit shares by exchange balance (requested=%.6f, available=%.6f, used=%.6f)",
                phase,
                shares,
                available,
                capped,
            )
        return max(0.0, capped)

    def _resolve_trade_quotes(self, direction: str) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
        if self.current_market is None:
            return None, None, None, None
        d = str(direction or "").upper()
        if d == "UP":
            token_id = str(self.current_market.up_token_id or "")
            side_bid = _safe_prob(self.current_market.up_best_bid) or _safe_prob(self.current_market.up_price)
            side_ask = _safe_prob(self.current_market.up_best_ask) or _safe_prob(self.current_market.up_price)
            opposite_ask = _safe_prob(self.current_market.down_best_ask) or _safe_prob(self.current_market.down_price)
            return token_id, side_bid, side_ask, opposite_ask
        if d == "DOWN":
            token_id = str(self.current_market.down_token_id or "")
            side_bid = _safe_prob(self.current_market.down_best_bid) or _safe_prob(self.current_market.down_price)
            side_ask = _safe_prob(self.current_market.down_best_ask) or _safe_prob(self.current_market.down_price)
            opposite_ask = _safe_prob(self.current_market.up_best_ask) or _safe_prob(self.current_market.up_price)
            return token_id, side_bid, side_ask, opposite_ask
        return None, None, None, None

    def _mark_to_market_trade(self, trade: TradeRecord, side_bid: Optional[float]) -> tuple[float, float, float]:
        stake = max(0.0, float(trade.amount or 0.0))
        entry_price = float(trade.price or 0.0)
        if stake <= 0.0 or not (0.0 < entry_price < 1.0):
            return 0.0, 0.0, 0.0
        shares = float(stake / entry_price)
        px = _safe_prob(side_bid)
        if px is None:
            return 0.0, 0.0, 0.0
        current_value = float(shares * px)
        raw_pnl = float(current_value - stake)
        pnl = float(apply_fee_to_pnl(raw_pnl, stake))
        roi_pct = float((pnl / stake) * 100.0) if stake > 0.0 else 0.0
        return float(px), float(pnl), float(roi_pct)

    def _finalize_trade_close(
        self,
        *,
        trade: TradeRecord,
        pnl: float,
        close_reason: str,
        actual_outcome: str,
    ):
        realized = float(pnl)
        trade.pnl = realized
        won = bool(realized >= 0.0)
        trade.result = "WIN" if won else "LOSS"
        if won:
            self.risk_mgr.consecutive_losses = 0
        else:
            self.risk_mgr.consecutive_losses += 1
        self.risk_mgr.daily_pnl += realized
        self._upsert_live_trade_closed(
            trade=trade,
            window_start=self.current_trade_window_start,
            actual_outcome=actual_outcome,
            close_reason=close_reason,
        )
        if bool(getattr(config.trading, "live_telegram_notify_close", True)):
            start_price = float(self.market_start_price or 0.0)
            end_price = float(self.price_feed.current_price or 0.0)
            btc_exit_price = float(self.price_feed.current_price or 0.0)
            msg = self._format_live_closed_telegram(
                trade=trade,
                actual_outcome=actual_outcome,
                close_reason=close_reason,
                start_price=start_price,
                end_price=end_price,
                btc_exit_price=btc_exit_price,
            )
            self._spawn_background_task(
                self._send_live_telegram(msg, reason="trade_close")
            )
        self.current_trade = None
        self.current_trade_signal_confidence = None
        self.current_trade_signal_reason = None
        self.current_trade_entry_source = None
        self._early_exit_opposite_hits.clear()
        self._early_exit_peak_roi.clear()
        self._persist_runtime_state()

    def _schedule_settlement_exit_for_previous_window(
        self,
        *,
        trade: TradeRecord,
        market: Optional[MarketInfo],
        won: bool,
    ) -> None:
        self._pending_settlement_exit = None
        if config.trading.dry_run:
            return
        if not bool(config.trading.live_settlement_exit_enabled):
            return
        if not won:
            return
        if market is None:
            return

        direction = str(trade.direction or "").upper()
        if direction == "UP":
            token_id = str(market.up_token_id or "")
        elif direction == "DOWN":
            token_id = str(market.down_token_id or "")
        else:
            return
        if not token_id:
            return

        stake = max(0.0, float(trade.amount or 0.0))
        entry_price = float(trade.price or 0.0)
        if stake <= 0.0 or not (0.0 < entry_price < 1.0):
            return
        shares = float(stake / entry_price)
        if shares <= 0.0:
            return

        d1 = max(0.0, float(config.trading.live_settlement_exit_delay1_sec))
        d2 = max(0.0, float(config.trading.live_settlement_exit_delay2_sec))
        offsets = sorted({float(d1), float(d2)})
        if not offsets:
            return

        self._pending_settlement_exit = {
            "market": market,
            "slug": str(market.slug or ""),
            "window_start": int(market.start_timestamp or 0),
            "window_end": int(market.end_timestamp or 0),
            "direction": direction,
            "token_id": token_id,
            "shares": float(shares),
            "stake": float(stake),
            "entry_price": float(entry_price),
            "attempt_offsets": offsets,
            "attempt_index": 0,
        }
        logger.info(
            "Scheduled post-settlement exit: slug=%s dir=%s shares=%.6f @+%ss/+%ss",
            str(market.slug or ""),
            direction,
            float(shares),
            int(offsets[0]),
            int(offsets[-1]),
        )

    async def _maybe_run_pending_settlement_exit(self, *, now_ts: float) -> None:
        pending = self._pending_settlement_exit
        if pending is None or config.trading.dry_run:
            return

        offsets = [float(x) for x in list(pending.get("attempt_offsets") or []) if float(x) >= 0.0]
        if not offsets:
            self._pending_settlement_exit = None
            return

        attempt_idx = int(pending.get("attempt_index") or 0)
        if attempt_idx >= len(offsets):
            logger.info("Post-settlement exit attempts exhausted; fallback to claim.")
            self._pending_settlement_exit = None
            await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        window_end = float(pending.get("window_end") or 0.0)
        due_ts = window_end + float(offsets[attempt_idx])
        if float(now_ts) < due_ts:
            return

        # Consume this slot now to avoid repeated retries on the same timestamp.
        pending["attempt_index"] = attempt_idx + 1

        market = pending.get("market")
        if not isinstance(market, MarketInfo):
            self._pending_settlement_exit = None
            return

        try:
            await self.poly_client.refresh_odds(market)
        except Exception as e:
            logger.warning("Post-settlement exit odds refresh failed: %s", e)
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        direction = str(pending.get("direction") or "").upper()
        if direction == "UP":
            side_bid = _safe_prob(market.up_best_bid) or _safe_prob(market.up_price)
        elif direction == "DOWN":
            side_bid = _safe_prob(market.down_best_bid) or _safe_prob(market.down_price)
        else:
            side_bid = None

        token_id = str(pending.get("token_id") or "")
        requested_shares = float(pending.get("shares") or 0.0)
        shares = await self._cap_exit_shares_by_balance(
            market=market,
            direction=direction,
            requested_shares=float(requested_shares),
            phase="post-settlement-exit",
        )
        stake = float(pending.get("stake") or 0.0)
        if not token_id or shares <= 0.0 or stake <= 0.0 or side_bid is None:
            logger.warning(
                "Post-settlement exit skipped (attempt %s/%s): invalid quote/token",
                int(pending.get("attempt_index") or 0),
                len(offsets),
            )
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        est_notional = float(shares * float(side_bid))
        est_pnl = float(apply_fee_to_pnl(est_notional - stake, stake))
        est_roi_pct = float((est_pnl / stake) * 100.0) if stake > 0.0 else 0.0
        min_bid = float(config.trading.live_settlement_exit_min_bid)
        min_roi = float(config.trading.live_settlement_exit_min_roi_pct)
        if float(side_bid) < min_bid or float(est_roi_pct) < min_roi:
            logger.info(
                "Post-settlement exit skipped (attempt %s/%s): bid=%.3f roi=%+.2f%% (need bid>=%.3f roi>=%.2f%%)",
                int(pending.get("attempt_index") or 0),
                len(offsets),
                float(side_bid),
                float(est_roi_pct),
                float(min_bid),
                float(min_roi),
            )
            if int(pending.get("attempt_index") or 0) >= len(offsets):
                self._pending_settlement_exit = None
                await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")
            return

        exit_result = await self.poly_client.place_exit_order(
            token_id=str(token_id),
            side="SELL",
            shares=float(shares),
            reference_bid=float(side_bid),
        )

        if exit_result is None:
            logger.warning("Post-settlement exit failed: missing order result")
        elif bool(exit_result.get("uncertain_fill", False)):
            logger.warning("Post-settlement exit uncertain fill: %s", exit_result.get("reason"))
        elif not bool(exit_result.get("accepted", True)):
            logger.warning(
                "Post-settlement exit rejected: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
        elif not bool(exit_result.get("filled", False)):
            logger.warning(
                "Post-settlement exit not filled: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
        else:
            executed_size = float(exit_result.get("executed_size") or 0.0)
            if executed_size <= 0.0 or executed_size < (shares * 0.95):
                logger.warning(
                    "Post-settlement exit partial/invalid fill: %.6f / %.6f",
                    executed_size,
                    shares,
                )
            else:
                executed_notional = float(exit_result.get("executed_notional") or 0.0)
                executed_price = float(exit_result.get("executed_price") or 0.0)
                if executed_notional <= 0.0 and executed_size > 0.0 and 0.0 < executed_price < 1.0:
                    executed_notional = float(executed_size * executed_price)
                realized_pnl = float(apply_fee_to_pnl(executed_notional - stake, stake))
                logger.info(
                    "Post-settlement exit success: slug=%s dir=%s fill_px=%.3f pnl=$%+.2f roi=%+.2f%%",
                    str(pending.get("slug") or ""),
                    direction,
                    float(executed_price),
                    float(realized_pnl),
                    (float(realized_pnl) / max(float(stake), 1e-9)) * 100.0,
                )
                self._pending_settlement_exit = None
                await self._refresh_adaptive_balance_cap(force=True, reason="post_settlement_exit")
                return

        if int(pending.get("attempt_index") or 0) >= len(offsets):
            self._pending_settlement_exit = None
            await self._maybe_auto_claim(now_ts=float(now_ts), force=True, reason="post_settlement_fallback")

    async def _maybe_auto_claim(self, *, now_ts: float, force: bool = False, reason: str = "periodic"):
        if config.trading.dry_run:
            return
        if not force and not bool(config.trading.live_auto_claim_enabled):
            return
        if self._pending_settlement_exit is not None and str(reason or "").strip().lower() == "post_settlement":
            logger.info("Deferring immediate claim: pending post-settlement exit window active.")
            return
        if self._pending_settlement_exit is not None and not force:
            return
        interval = max(10.0, float(config.trading.live_auto_claim_interval_seconds))
        if not force and (now_ts - self._last_auto_claim_ts) < interval:
            return
        self._last_auto_claim_ts = float(now_ts)
        result = await self.poly_client.auto_claim_winnings()
        if bool(result.get("ok")):
            claimed = float(result.get("claimed") or 0.0)
            if claimed > 0.0:
                logger.info("Auto-claim success (%s): +$%.2f", reason, claimed)
            return
        if bool(result.get("supported", False)):
            logger.warning(
                "Auto-claim failed (%s): %s",
                reason,
                result.get("error") or result.get("status") or "unknown",
            )

    async def _maybe_early_exit_open_trade(self, *, now_ts: float, seconds_remaining: float) -> bool:
        trade = self.current_trade
        if trade is None or trade.result != "PENDING":
            return False
        if self.current_market is None:
            return False
        exit_cfg = (
            _build_mirror_exit_policy_config()
            if LIVE_MIRROR_PAPER_GATES
            else _build_live_exit_policy_config()
        )
        if not exit_cfg.enabled:
            return False

        if self.current_market.last_odds_update < now_ts - 1.0:
            await self.poly_client.refresh_odds(self.current_market)

        hold_sec = float(now_ts - float(trade.timestamp or now_ts))
        if hold_sec < float(exit_cfg.min_elapsed_sec):
            return False

        token_id, side_bid, _side_ask, opposite_ask = self._resolve_trade_quotes(trade.direction)
        if side_bid is None or token_id is None:
            return False

        exit_px, mtm_pnl, mtm_roi_pct = self._mark_to_market_trade(trade, side_bid)
        if exit_px <= 0.0:
            return False

        window_key = int(self.current_trade_window_start or int(trade.timestamp))
        early_reason: Optional[str] = None

        btc_adverse_ok = True
        btc_move_from_entry_pct: Optional[float] = None
        current_btc_px = float(self.price_feed.current_price or 0.0)
        if bool(exit_cfg.stop_loss_require_btc_adverse):
            btc_entry_px = self.price_feed.get_price_at(float(trade.timestamp or now_ts))
            if btc_entry_px is not None and btc_entry_px > 0.0 and current_btc_px > 0.0:
                btc_move_from_entry_pct = ((current_btc_px - float(btc_entry_px)) / float(btc_entry_px)) * 100.0
                adverse_thr = abs(float(exit_cfg.stop_loss_btc_adverse_pct))
                if str(trade.direction).upper() == "UP":
                    btc_adverse_ok = float(btc_move_from_entry_pct) <= -adverse_thr
                else:
                    btc_adverse_ok = float(btc_move_from_entry_pct) >= adverse_thr
            else:
                btc_adverse_ok = False

        recent_ticks = self.price_feed.get_recent_prices(180)
        recent_prices = [float(t.price) for t in recent_ticks if float(t.price) > 0.0]
        recent_timestamps = [float(t.timestamp) for t in recent_ticks if float(t.price) > 0.0]
        if not recent_prices:
            fallback_px = float(current_btc_px or self.price_feed.get_price_at(float(trade.timestamp or now_ts)) or 0.0)
            if fallback_px > 0.0:
                recent_prices = [fallback_px]
                recent_timestamps = [float(now_ts)]
                current_btc_px = fallback_px
        elif current_btc_px <= 0.0:
            current_btc_px = float(recent_prices[-1])

        start_btc_px = float(self.market_start_price or 0.0)
        if start_btc_px <= 0.0 and current_btc_px > 0.0:
            start_btc_px = float(current_btc_px)
        window_start_ts = float(
            self.current_trade_window_start
            or getattr(self.current_market, "start_timestamp", 0)
            or float(trade.timestamp or now_ts)
        )
        seconds_elapsed = max(1.0, float(now_ts - window_start_ts))
        peak_roi = max(float(self._early_exit_peak_roi.get(window_key, -999.0)), float(mtm_roi_pct))
        self._early_exit_peak_roi[window_key] = peak_roi

        exit_decision = evaluate_exit_policy(
            ExitPolicyInput(
                direction=str(trade.direction),
                hold_sec=float(hold_sec),
                seconds_elapsed=float(seconds_elapsed),
                seconds_remaining=max(0.0, float(seconds_remaining)),
                signal_confidence=float(self.current_trade_signal_confidence or 0.5),
                mtm_roi_pct=float(mtm_roi_pct),
                current_price=float(current_btc_px),
                start_price=float(start_btc_px if start_btc_px > 0.0 else current_btc_px),
                peak_roi_pct=float(peak_roi),
                opposite_ask=float(opposite_ask) if opposite_ask is not None else None,
                recent_prices=list(recent_prices),
                recent_timestamps=list(recent_timestamps),
                btc_adverse_ok=bool(btc_adverse_ok),
                btc_move_from_entry_pct=(
                    float(btc_move_from_entry_pct)
                    if btc_move_from_entry_pct is not None
                    else None
                ),
                opposite_hits=int(self._early_exit_opposite_hits.get(window_key, 0)),
            ),
            exit_cfg,
        )
        if exit_decision.opposite_hits > 0:
            self._early_exit_opposite_hits[window_key] = int(exit_decision.opposite_hits)
        else:
            self._early_exit_opposite_hits.pop(window_key, None)
        early_reason = exit_decision.reason

        if (
            early_reason is None
            and bool(config.trading.live_pre_expiry_liquidation_enabled)
            and float(seconds_remaining) <= float(config.trading.live_pre_expiry_liquidation_remain_sec)
            and float(side_bid) >= float(config.trading.live_pre_expiry_liquidation_min_bid)
            and float(mtm_roi_pct) >= float(config.trading.live_pre_expiry_liquidation_min_roi_pct)
        ):
            self._early_exit_opposite_hits.pop(window_key, None)
            self._early_exit_peak_roi.pop(window_key, None)
            early_reason = (
                f"pre_expiry_liquidation(rem={float(seconds_remaining):.1f}s"
                f", bid={float(side_bid):.3f} >= {float(config.trading.live_pre_expiry_liquidation_min_bid):.3f}"
                f", roi={float(mtm_roi_pct):+.2f}% >= {float(config.trading.live_pre_expiry_liquidation_min_roi_pct):+.2f}%)"
            )

        if not early_reason:
            return False

        requested_shares = float(trade.amount / max(float(trade.price), 1e-9))
        shares = await self._cap_exit_shares_by_balance(
            market=self.current_market,
            direction=str(trade.direction),
            requested_shares=float(requested_shares),
            phase="early-exit",
        )
        if shares <= 0.0:
            logger.warning(
                "Early-exit skipped: no exitable balance (requested=%.6f shares)",
                requested_shares,
            )
            return False
        exit_result = await self.poly_client.place_exit_order(
            token_id=str(token_id),
            side="SELL",
            shares=float(shares),
            reference_bid=float(exit_px),
        )
        if exit_result is None:
            self._set_kill_switch("early-exit order result missing (possible unknown fill)")
            return False
        if bool(exit_result.get("uncertain_fill", False)):
            self._set_kill_switch(
                f"early-exit uncertain fill: {exit_result.get('reason') or 'unknown'}"
            )
            return False
        if not bool(exit_result.get("accepted", True)):
            logger.warning(
                "Early-exit rejected: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
            return False
        if not bool(exit_result.get("filled", False)):
            logger.warning(
                "Early-exit not filled: status=%s reason=%s",
                exit_result.get("status"),
                exit_result.get("reason"),
            )
            return False

        executed_size = float(exit_result.get("executed_size") or 0.0)
        if executed_size > 0.0 and executed_size < (shares * 0.95):
            self._set_kill_switch(
                f"early-exit partial fill too small: {executed_size:.6f}/{shares:.6f} shares"
            )
            return False

        executed_notional = float(exit_result.get("executed_notional") or 0.0)
        executed_price = float(exit_result.get("executed_price") or 0.0)
        if executed_notional <= 0.0 and executed_size > 0.0 and 0.0 < executed_price < 1.0:
            executed_notional = float(executed_size * executed_price)
        if executed_notional <= 0.0:
            executed_notional = float(shares * exit_px)

        realized_pnl = float(apply_fee_to_pnl(executed_notional - float(trade.amount), float(trade.amount)))
        close_reason = (
            f"{early_reason} | exit_px={float(exit_px):.3f}"
            f" fill_px={float(executed_price):.3f}"
            f" fill_notional=${float(executed_notional):.2f}"
        )
        logger.warning(
            "EARLY EXIT ws=%s dir=%s reason=%s pnl=$%+.2f roi=%+.2f%%",
            self.current_trade_window_start,
            trade.direction,
            early_reason,
            realized_pnl,
            (realized_pnl / max(float(trade.amount), 1e-9)) * 100.0,
        )
        self._finalize_trade_close(
            trade=trade,
            pnl=realized_pnl,
            close_reason=close_reason,
            actual_outcome="EARLY_EXIT",
        )
        self._early_exit_peak_roi.pop(window_key, None)
        await self._refresh_adaptive_balance_cap(force=True, reason="post_early_exit")
        return True

    async def start(self):
        logger.info("=" * 60)
        logger.info("Polymarket BTC Up/Down 5m Speed Arbitrage Bot")
        logger.info(f"Mode: {'DRY RUN' if config.trading.dry_run else '*** LIVE TRADING ***'}")
        logger.info(f"Max bet: ${config.trading.max_bet_size} | Min edge: {config.trading.min_edge}")
        logger.info(
            "Entry gate: fee=%s%% | min_expected_roi=%s%%",
            round(config.trading.fee_rate * 100.0, 3),
            round(config.trading.min_expected_roi * 100.0, 3),
        )
        logger.info(
            "Live/Paper gate parity: enabled=%s",
            bool(LIVE_MIRROR_PAPER_GATES),
        )
        logger.info(
            "Jury: %s/%s | Check interval: %ss",
            config.trading.jury_threshold,
            self.jury.size,
            self._check_interval,
        )
        logger.info("Position mode: %s | Sizing mode: %s", self.position_mode, self.live_sizing_mode)
        logger.info(
            "Entry execution: mode=%s | timeout=%.2fs | poll=%.2fs | drift_abs=%.4f | drift_ratio=%.2f%%",
            config.trading.entry_order_mode,
            float(config.trading.limit_order_timeout_seconds),
            float(config.trading.order_poll_interval_seconds),
            float(config.trading.max_entry_price_drift_abs),
            float(config.trading.max_entry_price_drift_ratio) * 100.0,
        )
        logger.info(
            "Adaptive sizing: base=%.2f%% min=%.2f%% max=%.2f%% edge_boost=%.3f conf_boost=%.3f",
            float(config.trading.live_adaptive_base_frac) * 100.0,
            float(config.trading.live_adaptive_min_frac) * 100.0,
            float(config.trading.live_adaptive_max_frac) * 100.0,
            float(config.trading.live_adaptive_edge_boost),
            float(config.trading.live_adaptive_conf_boost),
        )
        logger.info(
            "Live profit mode: %s | relax(entry=%.0f%% edge=%.0f%% support=%.0f%%) "
            "kelly=%.2f max_frac=%.2f loss_deboost=%.2f",
            self.live_profit_mode,
            float(config.trading.live_aggressive_entry_relax) * 100.0,
            float(config.trading.live_aggressive_min_edge_relax) * 100.0,
            float(config.trading.live_aggressive_support_relax) * 100.0,
            float(config.trading.live_aggressive_kelly_frac),
            float(config.trading.live_aggressive_max_frac),
            float(config.trading.live_aggressive_loss_deboost),
        )
        logger.info(
            "Live guards: entry_start=%.0fs support>=%.0f%% unanim=%s move>=%.4f%%(lookback=%.0fs) "
            "trend(lookback=%.0fs opp<=%.4f%%) implied(side>=%.2f opp<=%.2f) "
            "contra_gap<=%.3f(ovr p>=%.2f conf>=%.2f) lag_edge>=%.3f down_block>=%.4f%%",
            float(config.trading.live_entry_start_seconds),
            float(config.trading.live_min_support_ratio) * 100.0,
            config.trading.live_require_unanimous,
            float(config.trading.live_min_recent_move_pct),
            float(config.trading.live_recent_move_lookback_sec),
            float(config.trading.live_trend_align_lookback_sec),
            float(config.trading.live_trend_align_max_opposing_move_pct),
            float(config.trading.live_min_entry_side_implied),
            float(config.trading.live_max_opposite_implied),
            float(config.trading.live_max_contra_gap),
            float(config.trading.live_contra_override_min_model_prob),
            float(config.trading.live_contra_override_min_conf),
            float(config.trading.live_min_lag_prob_edge),
            float(config.trading.live_down_above_start_block_pct),
        )
        if LIVE_MIRROR_PAPER_GATES:
            logger.info(
                "Parity mode ON: live entry/exit thresholds mirror paper. Live-specific guard values above are informational only."
            )
            logger.info(
                "Parity guards: entry=[%.0fs, %.0fs] remain>=%.0fs support>=%.0f%% conf>=%.0f%% "
                "ev>=%.2f%% ask<=%.2f unanim@strict>=%.2f",
                float(MIRROR_ENTRY_START_SEC),
                float(MIRROR_ENTRY_END_SEC),
                float(MIRROR_MIN_SECONDS_REMAINING),
                float(MIRROR_MIN_SUPPORT_RATIO) * 100.0,
                float(MIRROR_MIN_CONFIDENCE) * 100.0,
                float(MIRROR_MIN_EXPECTED_ROI) * 100.0,
                float(MIRROR_MAX_ENTRY_PRICE),
                float(MIRROR_STRICTNESS_UNANIMOUS_AT),
            )
        logger.info(
            "Fast-lane: enabled=%s elapsed=[%.0f, %.0f]s remain>=%.0fs move=[%.4f%%, %.4f%%] "
            "recent>=%.4f%% ask<=%.3f p>=%.3f lag>=%.3f edge>=%.3f ev>=%.3f%%",
            bool(config.trading.fast_lane_enabled and (not LIVE_MIRROR_PAPER_GATES)),
            float(config.trading.fast_lane_min_seconds_elapsed),
            float(config.trading.fast_lane_max_seconds_elapsed),
            float(config.trading.fast_lane_min_seconds_remaining),
            float(config.trading.fast_lane_min_move_pct),
            float(config.trading.fast_lane_max_move_pct),
            float(config.trading.fast_lane_min_recent_move_pct),
            float(config.trading.fast_lane_max_entry_price),
            float(config.trading.fast_lane_min_direction_prob),
            float(config.trading.fast_lane_min_lag_prob_edge),
            float(config.trading.fast_lane_min_prob_edge),
            float(config.trading.fast_lane_min_expected_roi) * 100.0,
        )
        logger.info(
            "Feature feed: lookback=%ss | resample=%ss | max_points=%s",
            int(config.trading.feature_lookback_seconds),
            float(config.trading.feature_resample_seconds),
            int(config.trading.feature_max_points),
        )
        logger.info(
            "Adaptive balance refresh: every %.0fs + post-fill",
            self._balance_refresh_sec,
        )
        logger.info(
            "Live maintenance guard: enabled=%s fail_threshold=%s probe_every=%.0fs recover_success=%s",
            bool(self._maintenance_guard_enabled),
            int(getattr(config.trading, "live_maintenance_fail_threshold", 6)),
            float(getattr(config.trading, "live_maintenance_probe_interval_seconds", 300.0)),
            int(getattr(config.trading, "live_maintenance_recover_success_count", 1)),
        )
        logger.info(
            "Live early-exit: enabled=%s min_elapsed=%.0fs opp_ask>=%.2f stop_loss<=%.1f%%",
            bool(config.trading.live_enable_early_exit),
            float(config.trading.live_early_exit_min_elapsed_sec),
            float(config.trading.live_early_exit_opposite_ask),
            float(config.trading.live_early_exit_stop_loss_roi_pct),
        )
        logger.info(
            "Pre-expiry liquidation: enabled=%s remain<=%.0fs bid>=%.2f roi>=%.1f%%",
            bool(config.trading.live_pre_expiry_liquidation_enabled),
            float(config.trading.live_pre_expiry_liquidation_remain_sec),
            float(config.trading.live_pre_expiry_liquidation_min_bid),
            float(config.trading.live_pre_expiry_liquidation_min_roi_pct),
        )
        logger.info(
            "Post-settlement exit: enabled=%s at +%.0fs/+%.0fs bid>=%.2f roi>=%.1f%% (else claim)",
            bool(config.trading.live_settlement_exit_enabled),
            float(config.trading.live_settlement_exit_delay1_sec),
            float(config.trading.live_settlement_exit_delay2_sec),
            float(config.trading.live_settlement_exit_min_bid),
            float(config.trading.live_settlement_exit_min_roi_pct),
        )
        logger.info(
            "Auto-claim: enabled=%s interval=%.0fs (best-effort)",
            bool(config.trading.live_auto_claim_enabled),
            float(config.trading.live_auto_claim_interval_seconds),
        )
        logger.info(
            "Live Telegram: enabled=%s open_notify=%s close_notify=%s configured=%s",
            bool(getattr(config.trading, "live_telegram_enabled", False)),
            bool(getattr(config.trading, "live_telegram_notify_open", True)),
            bool(getattr(config.trading, "live_telegram_notify_close", True)),
            bool(
                str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
                and str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
            ),
        )
        logger.info("=" * 60)

        self._load_runtime_state()
        self._reconcile_stale_open_live_trades()
        defer_uncertain_fill_startup_check = False
        if self._kill_switch_reason:
            kill_reason_lower = str(self._kill_switch_reason or "").lower()
            deterministic_balance_reject = (
                ("early-exit uncertain fill" in kill_reason_lower)
                and (
                    ("not enough balance" in kill_reason_lower)
                    or ("allowance" in kill_reason_lower)
                    or ("insufficient balance" in kill_reason_lower)
                )
            )
            deterministic_fak_no_match = (
                ("early-exit uncertain fill" in kill_reason_lower)
                and (
                    ("no orders found to match with fak order" in kill_reason_lower)
                    or (
                        ("fak order" in kill_reason_lower or "fak orders" in kill_reason_lower)
                        and ("no match" in kill_reason_lower)
                    )
                    or (
                        ("fak order" in kill_reason_lower or "fak orders" in kill_reason_lower)
                        and ("partially filled or killed" in kill_reason_lower)
                    )
                )
            )
            if deterministic_balance_reject or deterministic_fak_no_match:
                logger.warning(
                    "Clearing non-fatal latched kill-switch from deterministic reject: %s",
                    self._kill_switch_reason,
                )
                self._clear_kill_switch()
            elif "uncertain fill detected" in kill_reason_lower:
                defer_uncertain_fill_startup_check = True
                logger.warning(
                    "Deferring latched uncertain-fill kill-switch validation until startup market reconcile: %s",
                    self._kill_switch_reason,
                )

        if self._kill_switch_reason:
            allow_reset = os.getenv("LIVE_KILL_SWITCH_RESET_ON_START", "false").lower() == "true"
            if allow_reset:
                logger.warning(
                    "LIVE_KILL_SWITCH_RESET_ON_START=true -> clearing latched kill-switch: %s",
                    self._kill_switch_reason,
                )
                self._clear_kill_switch()
                defer_uncertain_fill_startup_check = False
            elif defer_uncertain_fill_startup_check:
                pass
            else:
                logger.error(
                    "Kill-switch is latched from previous run: %s",
                    self._kill_switch_reason,
                )
                logger.error(
                    "Refusing to start live loop. After manual verification set "
                    "LIVE_KILL_SWITCH_RESET_ON_START=true for one restart."
                )
                self._close_state_conn()
                return

        self._running = True
        price_task = asyncio.create_task(self.price_feed.connect())

        logger.info("Waiting for Binance price data...")
        for _ in range(30):
            if self.price_feed.current_price is not None:
                break
            await asyncio.sleep(1)

        if self.price_feed.current_price is None:
            logger.error("Failed to get initial price data, exiting")
            self._running = False
            price_task.cancel()
            self._close_state_conn()
            return

        logger.info(f"BTC price: ${self.price_feed.current_price:,.2f}")

        # Initialize current 5m market immediately so startup reconcile can run once.
        ts0 = compute_market_timestamps(time.time())
        await self._on_new_market(int(ts0["current"]["start"]), float(ts0["seconds_elapsed"]))
        if not self._running:
            price_task.cancel()
            self._close_state_conn()
            return

        if self._kill_switch_reason and defer_uncertain_fill_startup_check:
            cleared = await self._maybe_clear_latched_uncertain_fill_on_startup()
            if not cleared and self._kill_switch_reason:
                logger.error(
                    "Kill-switch remains latched after startup uncertain-fill validation: %s",
                    self._kill_switch_reason,
                )
                self._running = False
                price_task.cancel()
                self._close_state_conn()
                return

        await self._refresh_adaptive_balance_cap(force=True, reason="startup")
        await self._maybe_auto_claim(now_ts=float(time.time()), force=True, reason="startup")

        # Start continuous Polymarket price sync (calibrates Binance offset every 1s)
        price_sync_task = asyncio.create_task(self._polymarket_price_sync_loop())

        try:
            await self._trading_loop()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            self._running = False
            price_sync_task.cancel()
            self.price_feed.stop()
            self.poly_client.stop_odds_polling()
            self.poly_client.close_scraper()
            if self._odds_task:
                self._odds_task.cancel()
            await self.poly_client.close()
            self._persist_runtime_state()
            self._close_state_conn()
            price_task.cancel()

            stats = self.risk_mgr.get_stats()
            logger.info("=" * 60)
            logger.info("FINAL STATS:")
            logger.info(f"  Total trades: {stats['total_trades']}")
            logger.info(f"  Wins: {stats['wins']} | Losses: {stats['losses']}")
            logger.info(f"  Win rate: {stats['win_rate']:.1%}")
            logger.info(f"  Total PnL: ${stats['total_pnl']:+.2f}")
            logger.info("=" * 60)

    async def _trading_loop(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _tick(self):
        if self._kill_switch_reason:
            self._running = False
            return
        now = time.time()
        ts = compute_market_timestamps(now)

        current_start = ts["current"]["start"]
        seconds_elapsed = ts["seconds_elapsed"]
        seconds_remaining = ts["seconds_remaining"]

        # ---- New 5-min window? ----
        if self.current_market is None or self.current_market.start_timestamp != current_start:
            await self._on_new_market(current_start, seconds_elapsed)
            if not self._running:
                return

        if self.current_market is None:
            return

        await self._refresh_adaptive_balance_cap(reason="periodic")
        await self._maybe_run_pending_settlement_exit(now_ts=float(now))
        await self._maybe_auto_claim(now_ts=float(now), reason="periodic")

        # ---- Resolve previous trade ----
        if self.current_trade and self.current_trade.result == "PENDING":
            if self.current_trade.timestamp < (self.current_market.start_timestamp - 10):
                await self._resolve_previous_trade()
                await self._maybe_run_pending_settlement_exit(now_ts=float(now))
                await self._maybe_auto_claim(now_ts=float(now), force=True, reason="post_settlement")
                if self.current_trade and self.current_trade.result == "PENDING":
                    self._set_kill_switch(
                        "Pending trade remained unresolved after rollover check; stopping for safety"
                    )
                    return

        # ---- Manage open trade (early exit) ----
        if self.current_trade and self.current_trade.result == "PENDING":
            await self._maybe_early_exit_open_trade(
                now_ts=float(now),
                seconds_remaining=float(seconds_remaining),
            )
            if self.current_trade and self.current_trade.result == "PENDING":
                return

        # ---- One trade per 5m window ----
        if (
            self._trade_locked_window_start is not None
            and int(self._trade_locked_window_start) == int(current_start)
        ):
            return

        # ---- Maintenance/outage pause gate (live only) ----
        if self._maintenance_guard_enabled and self._maintenance_mode:
            if float(now) < float(self._maintenance_next_probe_ts):
                if (float(now) - float(self._maintenance_last_skip_log_ts)) >= 30.0:
                    wait_left = max(0.0, float(self._maintenance_next_probe_ts) - float(now))
                    logger.warning(
                        "Live maintenance pause active: %s | next probe in %.0fs",
                        self._maintenance_last_reason or "market data unavailable",
                        wait_left,
                    )
                    self._maintenance_last_skip_log_ts = float(now)
                return

            probe_ok = await self._probe_live_market_data_health(int(current_start))
            if not probe_ok:
                self._record_market_data_failure(
                    reason="maintenance probe failed",
                    now_ts=float(now),
                )
                return
            self._record_market_data_success()

        # ---- Timing filters ----
        if seconds_remaining < config.trading.cutoff_before_close_seconds:
            return

        # ---- Risk check ----
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            if "entering cooldown" in str(reason).lower():
                self._persist_runtime_state()
            return

        # ---- Refresh Polymarket odds (high-frequency) ----
        # Only fetch if stale (>1s old) to avoid hammering API
        data_refresh_attempted = False
        data_refresh_ok = True
        if self.current_market.last_odds_update < now - 1.0:
            data_refresh_attempted = True
            data_refresh_ok = False
            if self.current_market.up_token_id and self.current_market.down_token_id:
                data_refresh_ok = bool(await self.poly_client.refresh_odds(self.current_market))
                if not data_refresh_ok:
                    market = await self.poly_client.find_market(current_start)
                    if market:
                        self.current_market = market
                        if self.current_market.up_token_id and self.current_market.down_token_id:
                            data_refresh_ok = bool(
                                await self.poly_client.refresh_odds(self.current_market)
                            )
            else:
                # Try to find market again
                market = await self.poly_client.find_market(current_start)
                if market:
                    self.current_market = market
                    if self.current_market.up_token_id and self.current_market.down_token_id:
                        data_refresh_ok = bool(await self.poly_client.refresh_odds(self.current_market))

        if self._maintenance_guard_enabled and data_refresh_attempted:
            if data_refresh_ok:
                self._record_market_data_success()
            else:
                self._record_market_data_failure(
                    reason="odds/market refresh failed",
                    now_ts=float(now),
                )
                return

        await self._maybe_sync_market_start_price(
            now_ts=float(now),
            seconds_elapsed=float(seconds_elapsed),
        )

        # ---- Retry previously rejected entry order ----
        # Placed after odds refresh so price comparison uses fresh data.
        if self._pending_entry_retry is not None:
            retry_handled = await self._attempt_entry_retry(
                now, seconds_remaining, current_start
            )
            if retry_handled:
                return  # either filled or still waiting for retry

        # ---- Block entry until Price to Beat is confirmed ----
        if not self._market_start_official:
            return

        # ---- Build context ----
        ctx = self._build_context(seconds_elapsed, seconds_remaining)
        if ctx is None:
            return

        # ---- Quick divergence check BEFORE full jury (save CPU) ----
        if self.market_start_price and self.market_start_price > 0:
            btc_change_pct = abs(
                (self.price_feed.current_price - self.market_start_price)
                / self.market_start_price * 100
            )
            # If BTC hasn't moved much, no opportunity
            if btc_change_pct < 0.02 and seconds_elapsed < 120:
                return

        # ---- Fast-lane: Binance lead / Polymarket lag (judge bypass) ----
        fast_signal = None
        if not LIVE_MIRROR_PAPER_GATES:
            fast_signal = self._evaluate_fast_lane_signal(ctx, now)
        if fast_signal is not None:
            fast_direction = str(fast_signal.get("direction", ""))
            if self.position_mode == "UP_ONLY" and fast_direction != "UP":
                fast_signal = None
            elif self.position_mode == "DOWN_ONLY" and fast_direction != "DOWN":
                fast_signal = None

        if fast_signal is not None:
            fast_direction = str(fast_signal["direction"])
            fast_conf = float(fast_signal["confidence"])
            fast_edge = float(fast_signal["prob_edge"])
            fast_price = float(fast_signal["entry_price"])
            fast_p = float(fast_signal["direction_prob"])
            fast_market_p = float(fast_signal.get("market_prob_dir", 0.0))
            fast_lag = float(fast_signal.get("lag_prob_edge", 0.0))
            fast_ev = float(fast_signal["expected_roi"])
            fast_move = float(fast_signal["move_pct"])
            fast_recent = float(fast_signal["recent_move_pct"])

            bet_size = self._compute_entry_bet_size(
                fast_conf,
                fast_edge,
                expected_roi=float(fast_ev),
                model_prob=float(fast_p),
                entry_price=float(fast_price),
            )
            if bet_size >= config.trading.min_bet_size:
                if fast_direction == "UP":
                    token_id = self.current_market.up_token_id
                    price = (
                        _safe_prob(self.current_market.up_best_ask)
                        or _safe_prob(self.current_market.up_price)
                    )
                else:
                    token_id = self.current_market.down_token_id
                    price = (
                        _safe_prob(self.current_market.down_best_ask)
                        or _safe_prob(self.current_market.down_price)
                    )

                if price is None:
                    price = fast_price

                if price is not None and 0.01 < price < 0.99 and token_id:
                    logger.info(
                        ">>> FAST-LANE TRADE: %s | $%.2f @ %.4f | p=%.3f mkt=%.3f lag=%+.3f edge=%.3f ev=%+.3f%% | "
                        "move=%+.4f%% recent=%+.4f%%",
                        fast_direction,
                        bet_size,
                        float(price),
                        fast_p,
                        fast_market_p,
                        fast_lag,
                        fast_edge,
                        fast_ev * 100.0,
                        fast_move,
                        fast_recent,
                    )
                    result = await self.poly_client.place_entry_order(
                        token_id=token_id,
                        side=fast_direction,
                        amount=bet_size,
                        reference_ask=float(price),
                    )
                    handled = await self._handle_entry_order_result(
                        result,
                        direction=fast_direction,
                        fallback_amount=float(bet_size),
                        fallback_price=float(price),
                        source="Fast-lane",
                        signal_confidence=float(fast_conf),
                        signal_reason=str(fast_signal.get("reason") or "fast_lane"),
                    )
                    if handled:
                        self._pending_entry_retry = None
                        await self._refresh_adaptive_balance_cap(force=True, reason="post_fill")
                    elif not self._kill_switch_reason and not bool(
                        (result or {}).get("uncertain_fill", False)
                    ):
                        self._save_entry_retry(
                            direction=fast_direction,
                            token_id=token_id,
                            price=float(price),
                            bet_size=float(bet_size),
                            source="Fast-lane",
                            signal_confidence=float(fast_conf),
                            signal_reason=str(fast_signal.get("reason") or "fast_lane"),
                        )
                    if handled or self._kill_switch_reason:
                        return

        # Jury timing floor/range.
        entry_start_sec = float(config.trading.live_entry_start_seconds)
        entry_end_sec = float(config.polymarket.interval_seconds - config.trading.cutoff_before_close_seconds)
        min_seconds_remaining = float(config.trading.cutoff_before_close_seconds)
        if LIVE_MIRROR_PAPER_GATES:
            entry_start_sec = float(MIRROR_ENTRY_START_SEC)
            entry_end_sec = float(MIRROR_ENTRY_END_SEC)
            min_seconds_remaining = float(MIRROR_MIN_SECONDS_REMAINING)
        if seconds_elapsed < entry_start_sec or seconds_elapsed > entry_end_sec:
            return
        if seconds_remaining < min_seconds_remaining:
            return

        # ---- Jury deliberation ----
        decision = self.jury.deliberate(ctx)

        if decision.direction == "NO_TRADE":
            return

        # DOWN-specific entry time cutoff (late DOWN entries lose money)
        down_entry_end = (
            float(MIRROR_DOWN_ENTRY_END_SEC)
            if LIVE_MIRROR_PAPER_GATES
            else float(getattr(config.trading, "down_entry_end_seconds", 160))
        )
        if decision.direction == "DOWN" and seconds_elapsed > down_entry_end:
            self._log_rejected_live(
                decision, ctx, seconds_elapsed, "down_late_entry",
                f"DOWN late entry: elapsed={seconds_elapsed:.0f}s > {down_entry_end:.0f}s",
            )
            return

        if self.position_mode == "UP_ONLY" and decision.direction != "UP":
            return
        if self.position_mode == "DOWN_ONLY" and decision.direction != "DOWN":
            return

        support_votes = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
        support_ratio = (support_votes / float(len(decision.verdicts))) if decision.verdicts else 0.0
        if not LIVE_MIRROR_PAPER_GATES:
            required_min_edge = float(config.trading.min_edge)
            required_support_ratio = float(config.trading.live_min_support_ratio)
            if self.live_profit_mode == "AGGRESSIVE":
                required_min_edge *= (
                    1.0 - _clamp(float(config.trading.live_aggressive_min_edge_relax), 0.0, 0.60)
                )
                required_support_ratio -= _clamp(float(config.trading.live_aggressive_support_relax), 0.0, 0.25)
            required_min_edge = _clamp(required_min_edge, 0.02, 0.95)
            required_support_ratio = _clamp(required_support_ratio, 0.50, 1.0)

            if decision.avg_confidence < required_min_edge:
                return
            if support_ratio < required_support_ratio:
                return
            if config.trading.live_require_unanimous and not decision.unanimous:
                return

        if decision.direction == "UP":
            token_id = self.current_market.up_token_id
        else:
            token_id = self.current_market.down_token_id

        if not token_id:
            return

        # ── Fresh CLOB fetch (parity mode) ─────────────────────────
        # Use the same point-in-time CLOB snapshot as paper_trade_sim
        # so both see identical ask prices at the moment of entry decision.
        if LIVE_MIRROR_PAPER_GATES:
            from polymarket_client import fetch_clob_book_sync
            (_up_bid, _up_ask_clob, _), (_dn_bid, _dn_ask_clob, _) = await asyncio.gather(
                asyncio.to_thread(fetch_clob_book_sync, str(self.current_market.up_token_id)),
                asyncio.to_thread(fetch_clob_book_sync, str(self.current_market.down_token_id)),
            )
            up_ask = _safe_prob(_up_ask_clob) or _safe_prob(self.current_market.up_best_ask) or _safe_prob(self.current_market.up_price)
            down_ask = _safe_prob(_dn_ask_clob) or _safe_prob(self.current_market.down_best_ask) or _safe_prob(self.current_market.down_price)
        else:
            up_ask = (
                _safe_prob(self.current_market.up_best_ask)
                or _safe_prob(self.current_market.up_price)
            )
            down_ask = (
                _safe_prob(self.current_market.down_best_ask)
                or _safe_prob(self.current_market.down_price)
            )

        price = up_ask if decision.direction == "UP" else down_ask

        if price is None or price <= 0.01 or price >= 0.99:
            return

        side_ask = up_ask if decision.direction == "UP" else down_ask
        opposite_ask = down_ask if decision.direction == "UP" else up_ask

        if LIVE_MIRROR_PAPER_GATES:
            conn = self._ensure_state_conn()
            tick_samples, odds_samples = _live_window_sample_counts(conn, int(current_start), float(now))
            if tick_samples < int(MIRROR_MIN_TICK_SAMPLES) or odds_samples < int(MIRROR_MIN_ODDS_SAMPLES):
                return

        min_entry_side_implied = (
            float(MIRROR_MIN_ENTRY_SIDE_IMPLIED)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_min_entry_side_implied)
        )
        max_opposite_implied = (
            float(MIRROR_MAX_OPPOSITE_IMPLIED)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_max_opposite_implied)
        )
        # Use the best of: raw side_ask, complement of opposite_ask, or
        # Gamma API initial mid-price (survives end-of-window CLOB collapse).
        effective_side_implied = side_ask
        if side_ask is not None and opposite_ask is not None:
            implied_from_opposite = 1.0 - opposite_ask
            effective_side_implied = max(side_ask, implied_from_opposite)
        # Fallback: Gamma API initial price (never overwritten by refresh_odds).
        # When CLOB is thin (e.g. UP ask=0.19 early in window), gamma_up_price
        # still holds the discovery-time value (~0.50), preventing false blocks.
        if self.current_market is not None:
            gamma_mid = (
                _safe_prob(getattr(self.current_market, "gamma_up_price", None))
                if decision.direction == "UP"
                else _safe_prob(getattr(self.current_market, "gamma_down_price", None))
            )
            if gamma_mid is not None and effective_side_implied is not None:
                effective_side_implied = max(effective_side_implied, gamma_mid)
        if effective_side_implied is not None and effective_side_implied < min_entry_side_implied:
            logger.info(
                "Skip live implied-side guard: dir=%s side_ask=%.3f eff=%.3f < %.3f",
                decision.direction,
                side_ask or 0.0,
                effective_side_implied,
                min_entry_side_implied,
            )
            return
        # DOWN-specific min entry price (cheap DOWN tokens are traps)
        down_min_price = (
            float(MIRROR_DOWN_MIN_ENTRY_PRICE)
            if LIVE_MIRROR_PAPER_GATES
            else float(getattr(config.trading, "down_min_entry_price", 0.42))
        )
        # Use gamma initial price as floor for cheap-token check (CLOB can
        # temporarily show 0.22 while real market is ~0.50).
        _cheap_check_price = side_ask
        if decision.direction == "DOWN" and self.current_market is not None:
            _gamma_dn = _safe_prob(getattr(self.current_market, "gamma_down_price", None))
            if _gamma_dn is not None and _cheap_check_price is not None:
                _cheap_check_price = max(_cheap_check_price, _gamma_dn)
        if decision.direction == "DOWN" and _cheap_check_price is not None and _cheap_check_price < down_min_price:
            self._log_rejected_live(
                decision, ctx, seconds_elapsed, "down_cheap_token",
                f"cheap DOWN token: ask={side_ask:.3f} gamma={_gamma_dn or 0:.3f} eff={_cheap_check_price:.3f} < {down_min_price:.3f}",
            )
            return
        # Use gamma initial price as ceiling for opposite-implied check
        _opp_check = opposite_ask
        if self.current_market is not None and _opp_check is not None:
            _gamma_opp = (
                _safe_prob(getattr(self.current_market, "gamma_down_price", None))
                if decision.direction == "UP"
                else _safe_prob(getattr(self.current_market, "gamma_up_price", None))
            )
            if _gamma_opp is not None:
                _opp_check = min(_opp_check, _gamma_opp)
        if _opp_check is not None and _opp_check > max_opposite_implied:
            logger.info(
                "Skip live opposite-implied guard: dir=%s opp_ask=%.3f eff=%.3f > %.3f",
                decision.direction,
                opposite_ask or 0.0,
                _opp_check,
                max_opposite_implied,
            )
            return

        btc_move_from_start_pct = (
            ((float(ctx.current_binance_price) - float(ctx.market_start_price)) / float(ctx.market_start_price)) * 100.0
            if ctx.market_start_price > 0
            else 0.0
        )
        # Divergence risk filter: Binance-Chainlink gap can flip settlement
        # DOWN needs wider boundary due to higher mean-reversion + Chainlink UP bias
        if decision.direction == "DOWN":
            min_boundary = (
                float(MIRROR_DOWN_MIN_BOUNDARY_DIST_PCT)
                if LIVE_MIRROR_PAPER_GATES
                else float(getattr(config.trading, "down_min_boundary_dist_pct", 0.050))
            )
        else:
            min_boundary = (
                float(MIRROR_MIN_BOUNDARY_DIST_PCT)
                if LIVE_MIRROR_PAPER_GATES
                else float(getattr(config.trading, "min_boundary_dist_pct", 0.040))
            )
        if abs(btc_move_from_start_pct) < min_boundary:
            self._log_rejected_live(
                decision, ctx, seconds_elapsed, "divergence_risk",
                f"divergence risk: |btc_move|={abs(btc_move_from_start_pct):.4f}% < {min_boundary:.4f}%",
            )
            return
        recent_move_lookback_sec = (
            float(MIRROR_RECENT_MOVE_LOOKBACK_SEC)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_recent_move_lookback_sec)
        )
        # Use DB ticks (same source as paper) when parity mode is on.
        if LIVE_MIRROR_PAPER_GATES:
            recent_move = _recent_move_pct_db(
                conn, int(current_start), now, recent_move_lookback_sec,
            )
        else:
            recent_move = _recent_move_pct(
                prices=list(ctx.recent_prices),
                timestamps=list(ctx.recent_timestamps),
                now_ts=now,
                lookback_sec=recent_move_lookback_sec,
            )
        if recent_move is None:
            return
        base_move_thr = (
            float(MIRROR_MIN_RECENT_MOVE_PCT)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_min_recent_move_pct)
        )
        # If btc_vs_start strongly confirms the SAME direction as the signal,
        # relax momentum threshold.  Only applies when start-move agrees with
        # signal direction (UP+positive or DOWN+negative).
        _strong_start_factor = 1.0
        _directional_start_move = (
            btc_move_from_start_pct if decision.direction == "UP"
            else -btc_move_from_start_pct
        )
        if _directional_start_move >= 0.06:
            _strong_start_factor = max(0.0, 1.0 - (_directional_start_move - 0.06) / 0.06)
        if decision.direction == "UP" and recent_move < base_move_thr * _strong_start_factor:
            logger.info(
                "Skip live momentum guard: dir=UP move=%.4f%% < +%.4f%% (adj=%.4f%%)",
                recent_move,
                base_move_thr,
                base_move_thr * _strong_start_factor,
            )
            return
        down_move_thr = base_move_thr
        if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
            down_move_extra = (
                float(MIRROR_DOWN_ABOVE_START_MOMENTUM_EXTRA)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_down_above_start_momentum_extra)
            )
            down_move_thr += down_move_extra
        effective_down_thr = down_move_thr * _strong_start_factor
        if decision.direction == "DOWN" and recent_move > -effective_down_thr:
            logger.info(
                "Skip live momentum guard: dir=DOWN move=%.4f%% > -%.4f%% (adj=-%.4f%%, btc_vs_start=%+.4f%%)",
                recent_move,
                down_move_thr,
                effective_down_thr,
                btc_move_from_start_pct,
            )
            return
        trend_lookback_sec = (
            float(MIRROR_TREND_ALIGN_LOOKBACK_SEC)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_trend_align_lookback_sec)
        )
        if LIVE_MIRROR_PAPER_GATES:
            trend_move = _recent_move_pct_db(
                conn, int(current_start), now, trend_lookback_sec,
            )
        else:
            trend_move = _recent_move_pct(
                prices=list(ctx.recent_prices),
                timestamps=list(ctx.recent_timestamps),
                now_ts=now,
                lookback_sec=trend_lookback_sec,
            )
        if trend_move is None:
            return
        trend_opp_thr = abs(
            float(MIRROR_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.live_trend_align_max_opposing_move_pct)
        )
        # Also relax trend guard when start-move strongly confirms direction
        _effective_trend_thr = trend_opp_thr / max(_strong_start_factor, 0.05)
        if decision.direction == "UP" and trend_move < -_effective_trend_thr:
            logger.info(
                "Skip live trend-align guard: dir=UP trend_move=%.4f%% < -%.4f%% (lookback=%.0fs)",
                trend_move,
                _effective_trend_thr,
                trend_lookback_sec,
            )
            return
        if decision.direction == "DOWN" and trend_move > _effective_trend_thr:
            logger.info(
                "Skip live trend-align guard: dir=DOWN trend_move=%.4f%% > +%.4f%% (lookback=%.0fs)",
                trend_move,
                _effective_trend_thr,
                trend_lookback_sec,
            )
            return

        dynamic_min_roi = (
            float(MIRROR_MIN_EXPECTED_ROI)
            if LIVE_MIRROR_PAPER_GATES
            else float(config.trading.min_expected_roi)
        )
        if decision.direction == "DOWN" and btc_move_from_start_pct > 0.0:
            block_thr = (
                float(MIRROR_DOWN_ABOVE_START_BLOCK_PCT)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_down_above_start_block_pct)
            )
            if btc_move_from_start_pct >= block_thr:
                logger.info(
                    "Skip live DOWN-above-start hard block: btc_vs_start=%+.4f%% >= %.4f%%",
                    btc_move_from_start_pct,
                    block_thr,
                )
                return
            ratio = btc_move_from_start_pct / max(block_thr, 1e-9)
            ev_penalty = (
                float(MIRROR_DOWN_ABOVE_START_EV_PENALTY)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_down_above_start_ev_penalty)
            )
            dynamic_min_roi += ev_penalty * _clamp(ratio, 0.0, 1.0)
        use_aggressive_profit = (
            MIRROR_PROFIT_MODE == "aggressive"
            if LIVE_MIRROR_PAPER_GATES
            else self.live_profit_mode == "AGGRESSIVE"
        )
        if use_aggressive_profit:
            relax = (
                _clamp(float(MIRROR_AGGRESSIVE_ENTRY_RELAX), 0.0, 0.60)
                if LIVE_MIRROR_PAPER_GATES
                else _clamp(float(config.trading.live_aggressive_entry_relax), 0.0, 0.60)
            )
            dynamic_min_roi = max(0.0, dynamic_min_roi * (1.0 - relax))

        gate = evaluate_entry_gate(
            direction=decision.direction,
            entry_price=float(price),
            current_price=float(ctx.current_binance_price),
            start_price=float(ctx.market_start_price),
            seconds_elapsed=float(seconds_elapsed),
            jury_confidence=float(decision.avg_confidence),
            support_ratio=float(support_ratio),
            seconds_remaining=float(seconds_remaining),
            recent_prices=list(ctx.recent_prices),
            recent_timestamps=list(ctx.recent_timestamps),
            poly_up_ask=ctx.poly_up_ask,
            poly_down_ask=ctx.poly_down_ask,
            recent_results=(None if LIVE_MIRROR_PAPER_GATES else list(ctx.recent_results or [])),
        )
        if not gate.allow:
            logger.info("Skip trade by entry gate: %s", gate.reason)
            return
        market_up_prob, market_down_prob = _normalized_market_probs(up_ask, down_ask)
        market_dir_prob = None
        lag_prob_edge = None
        if market_up_prob is not None and market_down_prob is not None:
            market_dir_prob = (
                float(market_up_prob)
                if decision.direction == "UP"
                else float(market_down_prob)
            )
            lag_prob_edge = float(gate.model_prob) - float(market_dir_prob)
            min_lag_prob_edge = (
                float(MIRROR_MIN_LAG_PROB_EDGE)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_min_lag_prob_edge)
            )
            if lag_prob_edge < min_lag_prob_edge:
                logger.info(
                    "Skip live lag-edge guard: dir=%s model_p=%.3f mkt_p=%.3f lag=%+.3f < %.3f",
                    decision.direction,
                    float(gate.model_prob),
                    float(market_dir_prob),
                    float(lag_prob_edge),
                    min_lag_prob_edge,
                )
                return
        if side_ask is not None and opposite_ask is not None:
            contra_gap = float(opposite_ask) - float(side_ask)
            max_contra_gap = (
                float(MIRROR_MAX_CONTRA_GAP)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_max_contra_gap)
            )
            override_min_prob = (
                float(MIRROR_CONTRA_OVERRIDE_MIN_MODEL_PROB)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_contra_override_min_model_prob)
            )
            override_min_conf = (
                float(MIRROR_CONTRA_OVERRIDE_MIN_CONF)
                if LIVE_MIRROR_PAPER_GATES
                else float(config.trading.live_contra_override_min_conf)
            )
            if contra_gap > max_contra_gap:
                if not (
                    float(gate.model_prob) >= override_min_prob
                    and float(decision.avg_confidence) >= override_min_conf
                ):
                    logger.info(
                        "Skip live contra-gap guard: dir=%s gap=+%.3f > %.3f (p=%.3f conf=%.3f, need p>=%.3f conf>=%.3f)",
                        decision.direction,
                        contra_gap,
                        max_contra_gap,
                        float(gate.model_prob),
                        float(decision.avg_confidence),
                        override_min_prob,
                        override_min_conf,
                    )
                    return
        # ── Macro trend filter (mirrors paper_trade_sim) ──
        if LIVE_MIRROR_PAPER_GATES:
            try:
                _mt_conn = self._ensure_state_conn()
                macro_move = _live_macro_trend_pct(
                    _mt_conn, float(now), float(MIRROR_MACRO_TREND_LOOKBACK_SEC)
                )
                if macro_move is not None:
                    macro_opposing = (
                        (decision.direction == "DOWN" and macro_move > float(MIRROR_MACRO_TREND_BLOCK_PCT))
                        or (decision.direction == "UP" and macro_move < -float(MIRROR_MACRO_TREND_BLOCK_PCT))
                    )
                    if macro_opposing:
                        logger.info(
                            "Skip live macro-trend guard: dir=%s macro_move=%+.4f%% (threshold=%.4f%%, lookback=%.0fs)",
                            decision.direction,
                            macro_move,
                            float(MIRROR_MACRO_TREND_BLOCK_PCT),
                            float(MIRROR_MACRO_TREND_LOOKBACK_SEC),
                        )
                        return
            except Exception as e:
                logger.debug("Live macro trend check failed: %s", e)
        if gate.expected_roi < dynamic_min_roi:
            logger.info(
                "Skip live dynamic EV guard: net_ev=%+.3f%% < %.3f%%",
                gate.expected_roi * 100.0,
                dynamic_min_roi * 100.0,
            )
            return
        if LIVE_MIRROR_PAPER_GATES:
            try:
                conn = self._ensure_state_conn()
                loss_streak, recent_loss_rate = _live_recent_risk_state(conn)
                perf_count, perf_wr, perf_pnl = _live_recent_performance(conn, MIRROR_RECENT_PERF_WINDOW)
                seed_capital = max(1.0, float(MIRROR_EQUITY_SEED_CAPITAL))
                realized_equity, _available_equity = _live_equity_snapshot(conn, seed_capital)
                drawdown_pct = _live_equity_drawdown_pct(conn, seed_capital)

                last_opened_at = _live_last_opened_at(conn)
                stale_relax = _live_stale_relax_factor(last_opened_at=last_opened_at, now_ts=float(now))
                parity_thresholds = compute_parity_thresholds(
                    ParityAdaptiveConfig(
                        min_expected_roi=float(MIRROR_MIN_EXPECTED_ROI),
                        min_support_ratio=float(MIRROR_MIN_SUPPORT_RATIO),
                        min_confidence=float(MIRROR_MIN_CONFIDENCE),
                        max_entry_price=float(MIRROR_MAX_ENTRY_PRICE),
                        adaptive_max_ask_floor=float(MIRROR_ADAPTIVE_MAX_ASK_FLOOR),
                        require_unanimous=bool(MIRROR_REQUIRE_UNANIMOUS),
                        strictness_unanimous_at=float(MIRROR_STRICTNESS_UNANIMOUS_AT),
                        base_trade_gap_sec=float(MIRROR_BASE_TRADE_GAP_SEC),
                        target_trade_gap_sec=float(MIRROR_TARGET_TRADE_GAP_SEC),
                        stale_relax_max=float(MIRROR_STALE_RELAX_MAX),
                        profit_mode=str(MIRROR_PROFIT_MODE),
                        aggressive_entry_relax=float(MIRROR_AGGRESSIVE_ENTRY_RELAX),
                        aggressive_gap_mult=float(MIRROR_AGGRESSIVE_GAP_MULT),
                    ),
                    ParityAdaptiveState(
                        loss_streak=int(loss_streak),
                        recent_loss_rate=float(recent_loss_rate),
                        drawdown_pct=float(drawdown_pct),
                        perf_count=int(perf_count),
                        perf_pnl=float(perf_pnl),
                        stale_relax=float(stale_relax),
                    ),
                )
                strictness = float(parity_thresholds.strictness)
                strictness_eff = float(parity_thresholds.strictness_eff)
                adaptive_min_ev = float(parity_thresholds.adaptive_min_ev)
                adaptive_min_support = float(parity_thresholds.adaptive_min_support)
                adaptive_min_conf = float(parity_thresholds.adaptive_min_conf)
                adaptive_max_ask = float(parity_thresholds.adaptive_max_ask)
                dynamic_gap = float(parity_thresholds.dynamic_gap)

                since_last = (float(now) - float(last_opened_at)) if last_opened_at > 0 else 999999.0
                high_quality_override = (
                    float(gate.expected_roi) >= float(MIRROR_HIGH_QUALITY_EV)
                    and float(decision.avg_confidence) >= float(MIRROR_HIGH_QUALITY_CONF)
                    and float(support_ratio) >= max(float(adaptive_min_support), 0.80)
                )
                if since_last < dynamic_gap and not high_quality_override:
                    return
                if gate.expected_roi < adaptive_min_ev:
                    logger.info(
                        "Skip live weak EV(parity): net_ev=%+.3f%% < %.3f%% (loss_streak=%s recent_loss_rate=%.0f%%)",
                        gate.expected_roi * 100.0,
                        adaptive_min_ev * 100.0,
                        int(loss_streak),
                        float(recent_loss_rate) * 100.0,
                    )
                    return
                if support_ratio < adaptive_min_support:
                    logger.info(
                        "Skip live weak jury(parity): support=%.1f%% < %.1f%%",
                        support_ratio * 100.0,
                        adaptive_min_support * 100.0,
                    )
                    return
                require_unanimous = bool(parity_thresholds.require_unanimous)
                if require_unanimous and support_ratio < 1.0:
                    logger.info(
                        "Skip live non-unanimous(parity): strictness=%.2f->%.2f support=%.1f%%",
                        strictness,
                        strictness_eff,
                        support_ratio * 100.0,
                    )
                    return
                if float(decision.avg_confidence) < adaptive_min_conf:
                    logger.info(
                        "Skip live low confidence(parity): conf=%.3f < %.3f",
                        float(decision.avg_confidence),
                        adaptive_min_conf,
                    )
                    return
                if float(price) > adaptive_max_ask:
                    logger.info(
                        "Skip live expensive entry(parity): ask=%.3f > %.3f",
                        float(price),
                        adaptive_max_ask,
                    )
                    return
                # Underdog guard: cheap tokens (ask < 0.40) have low win rate.
                # Require higher confidence and EV to justify low base-rate.
                if float(price) < 0.40:
                    _underdog_min_conf = adaptive_min_conf + 0.10
                    _underdog_min_ev = adaptive_min_ev * 2.0
                    if float(decision.avg_confidence) < _underdog_min_conf:
                        logger.info(
                            "Skip live underdog(parity): ask=%.3f conf=%.3f < %.3f",
                            float(price), float(decision.avg_confidence), _underdog_min_conf,
                        )
                        return
                    if gate.expected_roi < _underdog_min_ev:
                        logger.info(
                            "Skip live underdog EV(parity): ask=%.3f ev=%.3f%% < %.3f%%",
                            float(price), gate.expected_roi * 100, _underdog_min_ev * 100,
                        )
                        return
                # Use real on-chain balance for drawdown stop (seed_capital may
                # be stale if the user deposited/withdrew since setting it).
                real_balance = self._current_live_cap()
                stop_level = seed_capital * (1.0 - float(MIRROR_MAX_DRAWDOWN_STOP_PCT))
                drawdown_equity = real_balance if real_balance > 0 else realized_equity
                if drawdown_equity <= stop_level:
                    logger.info(
                        "Skip live drawdown stop(parity): balance=$%.2f (seed=$%.2f) <= stop=$%.2f",
                        float(drawdown_equity),
                        float(seed_capital),
                        float(stop_level),
                    )
                    return
                if (
                    perf_count >= max(4, int(MIRROR_RECENT_PERF_WINDOW))
                    and perf_wr < float(MIRROR_MIN_RECENT_WIN_RATE)
                    and perf_pnl < 0.0
                ):
                    last_closed_at = _live_last_closed_at(conn)
                    since_closed = (float(now) - float(last_closed_at)) if last_closed_at > 0 else 999999.0
                    if since_closed < float(MIRROR_PERF_PAUSE_SEC):
                        logger.info(
                            "Skip live perf pause(parity): trades=%s wr=%.1f%% pnl=$%+.2f cooldown_left=%.0fs",
                            int(perf_count),
                            float(perf_wr) * 100.0,
                            float(perf_pnl),
                            float(MIRROR_PERF_PAUSE_SEC) - since_closed,
                        )
                        return
                dynamic_min_roi = max(float(dynamic_min_roi), float(adaptive_min_ev))
            except Exception as e:
                logger.warning("Live parity gate metrics unavailable; skip entry for safety: %s", e)
                return

        bet_size = self._compute_entry_bet_size(
            decision.avg_confidence,
            decision.max_edge,
            expected_roi=float(gate.expected_roi),
            model_prob=float(gate.model_prob),
            entry_price=float(price),
        )
        if bet_size < max(float(config.trading.min_bet_size), float(MIRROR_LIVE_MIN_BET)):
            return

        lag_display = f"{float(lag_prob_edge):+.3f}" if lag_prob_edge is not None else "n/a"
        logger.info(
            f">>> TRADE: {decision.direction} | ${bet_size:.2f} @ {price:.4f} | "
            f"conf={decision.avg_confidence:.3f} | unan={decision.unanimous} | "
            f"net_ev={gate.expected_roi:+.3%} | "
            f"lag={lag_display} | "
            f"BTC_chg={ctx.current_binance_price - ctx.market_start_price:+.2f} | "
            f"poly_up={ctx.poly_up_price:.3f} poly_down={ctx.poly_down_price:.3f}"
        )
        lag_reason = (
            f", lag={float(lag_prob_edge):+.3f}, mkt_p={float(market_dir_prob):.3f}"
            if lag_prob_edge is not None and market_dir_prob is not None
            else ""
        )
        jury_reason = (
            f"{support_votes}/{len(decision.verdicts)} {decision.direction} votes | "
            f"net_ev={gate.expected_roi:+.3%} >= target={dynamic_min_roi:.3%} "
            f"({gate.reason}{lag_reason})"
        )

        result = await self.poly_client.place_entry_order(
            token_id=token_id,
            side=decision.direction,
            amount=bet_size,
            reference_ask=float(price),
        )
        handled = await self._handle_entry_order_result(
            result,
            direction=decision.direction,
            fallback_amount=float(bet_size),
            fallback_price=float(price),
            source="Jury",
            signal_confidence=float(decision.avg_confidence),
            signal_reason=jury_reason,
        )
        if handled:
            self._pending_entry_retry = None
            await self._refresh_adaptive_balance_cap(force=True, reason="post_fill")
        elif not self._kill_switch_reason and not bool(
            (result or {}).get("uncertain_fill", False)
        ):
            self._save_entry_retry(
                direction=decision.direction,
                token_id=token_id,
                price=float(price),
                bet_size=float(bet_size),
                source="Jury",
                signal_confidence=float(decision.avg_confidence),
                signal_reason=jury_reason,
            )

    async def _on_new_market(self, start_timestamp: int, seconds_elapsed: float):
        self._pending_entry_retry = None  # clear retry on window change
        if self.current_trade and self.current_trade.result == "PENDING":
            await self._resolve_previous_trade()
        if self.current_trade and self.current_trade.result == "PENDING":
            self._set_kill_switch(
                "Pending trade could not be resolved at market rollover; manual review required"
            )
            return

        # Stop polling old market
        self.poly_client.stop_odds_polling()
        if self._odds_task:
            self._odds_task.cancel()
            self._odds_task = None

        # Start price: set chainlink_adj immediately for fast entry readiness,
        # then scrape exact PTB ~3s later (page needs time to show new window).
        self.market_start_price = None
        self._market_start_official = False
        self._market_start_source = "none"
        self._ptb_scrape_done = False

        self.current_market = await self.poly_client.find_market(start_timestamp)

        if self.current_market:
            if (
                self.current_market.price_to_beat is not None
                and float(self.current_market.price_to_beat) > 0.0
            ):
                self.market_start_price = float(self.current_market.price_to_beat)
                self._market_start_official = True
                self._market_start_source = "ptb_api"
                self._ptb_scrape_done = True
            elif (
                self.price_feed.calibrator is not None
                and self.price_feed.calibrator.is_calibrated
                and self.price_feed.current_price is not None
            ):
                # Immediate fallback — scrape will correct in ~3s
                self.market_start_price = self.price_feed.adjusted_price
                self._market_start_official = True
                self._market_start_source = "chainlink_adj"
            start_str = (
                f"${self.market_start_price:,.2f} ({self._market_start_source})"
                if self.market_start_price else "awaiting PTB"
            )
            logger.info(
                f"New market: {self.current_market.slug} | "
                f"UP={self.current_market.up_price:.3f} DOWN={self.current_market.down_price:.3f} | "
                f"BTC start={start_str}"
            )
            # Start background odds polling for this market
            if self.current_market.up_token_id and self.current_market.down_token_id:
                self._odds_task = asyncio.create_task(
                    self.poly_client.start_odds_polling(self.current_market, interval=1.0)
                )
                if not await self._reconcile_exchange_for_current_market("rollover"):
                    return
        else:
            logger.warning(f"Market not found for ts={start_timestamp}, creating stub")
            from polymarket_client import MarketInfo, market_slug_for_timestamp
            self.current_market = MarketInfo(
                condition_id="", question="",
                slug=market_slug_for_timestamp(start_timestamp),
                start_timestamp=start_timestamp,
                end_timestamp=start_timestamp + config.polymarket.interval_seconds,
                up_token_id="", down_token_id="",
                up_price=0.5, down_price=0.5, active=True,
            )

        self.current_trade = None
        self.current_trade_window_start = None
        self.current_trade_signal_confidence = None
        self.current_trade_signal_reason = None
        self.current_trade_entry_source = None
        self._trade_locked_window_start = None
        self._early_exit_opposite_hits.clear()
        self._early_exit_peak_roi.clear()
        self._persist_runtime_state()

    async def _maybe_sync_market_start_price(self, *, now_ts: float, seconds_elapsed: float):
        """Scrape exact PTB ~3s after window start, correct chainlink_adj estimate."""
        if self.current_market is None:
            return

        # --- Phase 1: if no price at all, set chainlink_adj immediately ---
        if not self._market_start_official or self.market_start_price is None:
            if (
                self.price_feed.calibrator is not None
                and self.price_feed.calibrator.is_calibrated
                and self.price_feed.current_price is not None
            ):
                self.market_start_price = self.price_feed.adjusted_price
                self._market_start_official = True
                self._market_start_source = "chainlink_adj"
                logger.info(
                    "Market start set from calibrated Binance: %s | $%.2f",
                    self.current_market.slug, self.market_start_price,
                )
            elif seconds_elapsed > 5.0:
                logger.warning(
                    "No start price for %s (elapsed=%.0fs)",
                    self.current_market.slug, seconds_elapsed,
                )
            return

        # --- Phase 2: scrape exact PTB + Current price at ~3s (one-shot) ---
        if self._ptb_scrape_done:
            return
        if seconds_elapsed < 3.0:
            return
        self._ptb_scrape_done = True

        scraped_ptb, scraped_current = await self.poly_client.scrape_prices(
            self.current_market.slug
        )

        # --- Calibrate offset using Polymarket Current price ---
        if scraped_current is not None and scraped_current > 0:
            binance_now = self.price_feed.current_price
            if binance_now is not None and binance_now > 0:
                new_offset = binance_now - scraped_current
                old_offset = self.price_feed.calibrator.offset if self.price_feed.calibrator else 0
                if self.price_feed.calibrator is not None:
                    self.price_feed.calibrator.offset = new_offset
                    self.price_feed.calibrator.chainlink_price = scraped_current
                    self.price_feed.calibrator.binance_at_update = binance_now
                logger.info(
                    "Calibration updated from Polymarket scrape: "
                    "poly_current=$%.2f binance=$%.2f new_offset=$%.2f (was $%.2f)",
                    scraped_current, binance_now, new_offset, old_offset,
                )

        if scraped_ptb is None or scraped_ptb <= 0:
            logger.warning("PTB scrape returned None for %s, keeping %s ($%.2f)",
                           self.current_market.slug, self._market_start_source,
                           self.market_start_price or 0)
            return

        prev = self.market_start_price
        prev_src = self._market_start_source
        self.market_start_price = scraped_ptb
        self._market_start_source = "ptb_scrape"
        delta = abs(scraped_ptb - prev) if prev else 0
        logger.info(
            "Market start corrected by scrape: %s | $%.2f -> $%.2f (delta=$%.2f, was %s)",
            self.current_market.slug, prev or 0, scraped_ptb, delta, prev_src,
        )

    async def _polymarket_price_sync_loop(self):
        """Continuously extract Polymarket's 'Current price' from Playwright page
        every ~5s (page reload + extract). Keeps the Binance-Chainlink calibration
        offset accurate, replacing unreliable Chainlink RPC."""
        _last_log = 0.0
        _consecutive_none = 0
        # Wait for initial scrape to load the Playwright page
        await asyncio.sleep(10.0)
        while self._running:
            try:
                poly_price = await self.poly_client.extract_current_price()
                if poly_price is not None and poly_price > 0:
                    _consecutive_none = 0
                    binance_now = self.price_feed.current_price
                    if binance_now is not None and binance_now > 0 and self.price_feed.calibrator is not None:
                        new_offset = binance_now - poly_price
                        old_offset = self.price_feed.calibrator.offset
                        self.price_feed.calibrator.offset = new_offset
                        self.price_feed.calibrator.chainlink_price = poly_price
                        self.price_feed.calibrator.binance_at_update = binance_now
                        self.price_feed.calibrator.chainlink_updated_at = time.time()
                        self.price_feed.calibrator.polymarket_sync_active = True

                        # Log offset changes > $5 or every 60s
                        now = time.time()
                        offset_delta = abs(new_offset - old_offset)
                        if offset_delta > 5.0 or (now - _last_log) > 60.0:
                            logger.info(
                                "Price sync: poly=$%.2f binance=$%.2f offset=$%.2f (delta=$%.2f)",
                                poly_price, binance_now, new_offset, offset_delta,
                            )
                            _last_log = now
                else:
                    _consecutive_none += 1
                    if _consecutive_none == 30:
                        logger.warning("Price sync: no Polymarket price for 30s")
                        _consecutive_none = 0
            except Exception as e:
                logger.debug("Price sync error: %s", e)
            await asyncio.sleep(1.0)

    async def _verify_settlement_outcome(
        self, window_start: int, trade_direction: str, initial_outcome: str
    ):
        """Re-check Polymarket settlement after delay and correct DB if needed.

        The bot resolves trades at rollover using Binance prices (fast but
        sometimes wrong when Binance-Chainlink diverge).  This background task
        polls Polymarket after 15s and 30s to get the oracle-based result.
        """
        for delay in (15, 30):
            await asyncio.sleep(delay)
            try:
                poly_outcome = await self.poly_client.fetch_settlement_outcome(window_start)
                if poly_outcome not in ("UP", "DOWN"):
                    continue
                if poly_outcome == initial_outcome:
                    logger.info(
                        "Settlement verified: ws=%s outcome=%s (matches initial)",
                        window_start, poly_outcome,
                    )
                    return
                # ── MISMATCH: Polymarket oracle disagrees with our Binance-based result ──
                logger.error(
                    "SETTLEMENT CORRECTION: ws=%s Polymarket=%s but bot recorded=%s "
                    "(Binance-Chainlink divergence). Correcting DB.",
                    window_start, poly_outcome, initial_outcome,
                )
                won = 1 if trade_direction == poly_outcome else 0
                conn = self._ensure_state_conn()
                # Recalculate PnL
                trade_row = fetch_one_dict(
                    conn,
                    "SELECT stake, shares FROM live_trades WHERE window_start=? ORDER BY id DESC LIMIT 1",
                    (int(window_start),),
                )
                if trade_row:
                    stake = float(trade_row.get("stake") or 0)
                    shares = float(trade_row.get("shares") or 0)
                    if won:
                        raw_pnl = shares - stake
                        pnl = apply_fee_to_pnl(raw_pnl, stake)
                    else:
                        pnl = -stake
                    roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
                    execute_write(
                        conn,
                        """UPDATE live_trades
                           SET actual_outcome=?, won=?, pnl=?, roi_pct=?,
                               close_reason=CONCAT(COALESCE(close_reason,''), ' [corrected: was ', ?, ' now ', ?, ']')
                           WHERE window_start=? AND status='CLOSED'
                           ORDER BY id DESC LIMIT 1""",
                        (poly_outcome, won, pnl, roi_pct, initial_outcome, poly_outcome, int(window_start)),
                    )
                    conn.commit()
                    # Also sync RiskManager equity
                    old_pnl_row = fetch_one(
                        conn,
                        "SELECT pnl FROM live_trades WHERE window_start=? ORDER BY id DESC LIMIT 1",
                        (int(window_start),),
                    )
                    logger.error(
                        "DB corrected: ws=%s outcome=%s->%s pnl=$%+.2f",
                        window_start, initial_outcome, poly_outcome, pnl,
                    )
                    # Send correction notification via Telegram
                    try:
                        msg = (
                            f"[SETTLEMENT CORRECTION]\n"
                            f"window: {window_start}\n"
                            f"direction: {trade_direction}\n"
                            f"was: {initial_outcome} (Binance) -> now: {poly_outcome} (Chainlink)\n"
                            f"result: {'WIN' if won else 'LOSS'}\n"
                            f"corrected pnl: ${pnl:+.2f}"
                        )
                        self._spawn_background_task(
                            self._send_live_telegram(msg, reason="settlement_correction")
                        )
                    except Exception:
                        pass
                return
            except Exception as e:
                logger.debug("Settlement verification attempt failed: %s", e)
        logger.info(
            "Settlement verification: Polymarket API did not return outcome for ws=%s after 30s",
            window_start,
        )

    async def _resolve_previous_trade(self):
        if not self.current_trade or self.current_trade.result != "PENDING":
            return

        if self.market_start_price and self.price_feed.current_price:
            resolved_trade = self.current_trade
            resolved_market = self.current_market

            # ── PRIMARY: Check Polymarket's actual settlement outcome ──
            # Binance and Chainlink prices can diverge, so we MUST check
            # Polymarket's oracle-based settlement rather than computing ourselves.
            actual_direction = None
            window_start_ts = int(self.current_trade_window_start or 0)
            try:
                poly_outcome = await self.poly_client.fetch_settlement_outcome(window_start_ts)
                if poly_outcome in ("UP", "DOWN"):
                    actual_direction = poly_outcome
                    logger.info(
                        "Settlement outcome from Polymarket API: %s (window=%s)",
                        actual_direction, window_start_ts,
                    )
            except Exception as e:
                logger.debug("Polymarket settlement query failed: %s", e)

            # ── FALLBACK: Use Binance price comparison if API unavailable ──
            window_end_ts = float(window_start_ts + 300)
            end_price = self.price_feed.get_price_at(window_end_ts)
            if end_price is None:
                end_price = self.price_feed.current_price
            if actual_direction is None:
                went_up = end_price >= self.market_start_price
                actual_direction = "UP" if went_up else "DOWN"
                logger.warning(
                    "Settlement fallback to Binance: start=$%.2f end=$%.2f -> %s "
                    "(Chainlink may differ!)",
                    float(self.market_start_price), float(end_price), actual_direction,
                )
            won = resolved_trade.direction == actual_direction

            self.risk_mgr.resolve_trade(resolved_trade, won)
            self.recent_results.append(actual_direction)
            if len(self.recent_results) > 50:
                self.recent_results = self.recent_results[-50:]

            logger.info(
                f"Market resolved: {actual_direction} | "
                f"Trade={resolved_trade.direction} -> {'WIN' if won else 'LOSS'}"
            )
            self._schedule_settlement_exit_for_previous_window(
                trade=resolved_trade,
                market=resolved_market,
                won=bool(won),
            )
            self._upsert_live_trade_closed(
                trade=resolved_trade,
                window_start=self.current_trade_window_start,
                actual_outcome=actual_direction,
                close_reason="expiry_settlement",
            )
            if bool(getattr(config.trading, "live_telegram_notify_close", True)):
                start_price = float(self.market_start_price or 0.0)
                tg_end_price = float(end_price or self.price_feed.current_price or 0.0)
                btc_exit_price = float(end_price or self.price_feed.current_price or 0.0)
                msg = self._format_live_closed_telegram(
                    trade=resolved_trade,
                    actual_outcome=actual_direction,
                    close_reason="expiry_settlement",
                    start_price=start_price,
                    end_price=tg_end_price,
                    btc_exit_price=btc_exit_price,
                )
                self._spawn_background_task(
                    self._send_live_telegram(msg, reason="trade_close_expiry")
                )
            # Schedule delayed settlement verification — Polymarket may not have
            # settled yet at rollover time.  Re-check after 15s and 30s.
            _ws = int(window_start_ts)
            _dir = str(resolved_trade.direction)
            _initial_outcome = str(actual_direction)
            self._spawn_background_task(
                self._verify_settlement_outcome(_ws, _dir, _initial_outcome)
            )
            self.current_trade = None
            self.current_trade_window_start = None
            self.current_trade_signal_confidence = None
            self.current_trade_signal_reason = None
            self.current_trade_entry_source = None
            self._trade_locked_window_start = None
            self._early_exit_opposite_hits.clear()
            self._early_exit_peak_roi.clear()
            self._persist_runtime_state()
            await self._refresh_adaptive_balance_cap(force=True, reason="post_settlement")

    def _build_context(self, seconds_elapsed: float, seconds_remaining: float) -> Optional[MarketContext]:
        if self.price_feed.current_price is None or self.market_start_price is None:
            return None
        if self.current_market is None:
            return None

        recent = self.price_feed.get_recent_prices(
            int(config.trading.feature_lookback_seconds)
        )
        resampled_prices, resampled_ts = _resample_ticks_fixed_interval(
            recent,
            interval_sec=float(config.trading.feature_resample_seconds),
            max_points=int(config.trading.feature_max_points),
        )
        # Fallback to raw ticks if resampling produced too few points.
        if len(resampled_prices) < 10 or len(resampled_ts) < 10:
            resampled_prices = [float(t.price) for t in recent]
            resampled_ts = [float(t.timestamp) for t in recent]

        return MarketContext(
            current_binance_price=self.price_feed.adjusted_price or self.price_feed.current_price,
            market_start_price=self.market_start_price,
            recent_prices=resampled_prices,
            recent_timestamps=resampled_ts,
            poly_up_price=self.current_market.up_price,
            poly_down_price=self.current_market.down_price,
            seconds_elapsed=seconds_elapsed,
            seconds_remaining=seconds_remaining,
            poly_up_bid=(
                self.current_market.up_best_bid
                if 0.0 < self.current_market.up_best_bid < 1.0
                else None
            ),
            poly_up_ask=(
                self.current_market.up_best_ask
                if 0.0 < self.current_market.up_best_ask < 1.0
                else None
            ),
            poly_down_bid=(
                self.current_market.down_best_bid
                if 0.0 < self.current_market.down_best_bid < 1.0
                else None
            ),
            poly_down_ask=(
                self.current_market.down_best_ask
                if 0.0 < self.current_market.down_best_ask < 1.0
                else None
            ),
            recent_results=self.recent_results[-20:],
        )


async def main():
    bot = TradingBot()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: setattr(bot, '_running', False))
        except NotImplementedError:
            pass

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
