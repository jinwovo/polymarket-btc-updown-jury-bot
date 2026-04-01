"""
Live paper-trading simulator for BTC Up/Down 5m signals.

What it does:
- Watches real-time signal from dashboard_server.build_snapshot()
- On actionable signal, enters one virtual trade per 5m window
- Uses REAL current orderbook ask price (UP/DOWN) as entry
- Calculates pay-to-win metrics for a virtual stake (default $1000)
- Resolves trade when market_windows.actual_outcome is available

Usage:
    python paper_trade_sim.py
    python paper_trade_sim.py --stake 1000 --interval 2
    python paper_trade_sim.py --status
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from config import config
from dashboard_server import build_snapshot
from db_config import (
    connect_db,
    db_label,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
)
from entry_parity import (
    ParityAdaptiveConfig,
    ParityAdaptiveState,
    compute_parity_thresholds,
)
from exit_policy import ExitPolicyConfig, ExitPolicyInput, evaluate_exit_policy
from judges import Jury, MarketContext
from telegram_notifier import send_telegram_message
from trade_gate import apply_fee_to_pnl, evaluate_entry_gate

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Console: WARNING+ only (trades, errors)
_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)
# File: all INFO+
_fh = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_paper.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)
logger = logging.getLogger("paper_sim")
_PAPER_TELEGRAM_WARNED_NOT_READY = False


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------- Data Collector Health Check ---------------
_DATA_STALE_WARN_INTERVAL = 120  # seconds between repeated warnings
_last_stale_warn_ts: float = 0.0
_data_collector_pid: int | None = None

PAPER_AUTO_START_COLLECTOR = os.getenv("PAPER_AUTO_START_COLLECTOR", "true").lower() == "true"
PAPER_DATA_MAX_AGE_SEC = float(os.getenv("PAPER_DATA_MAX_AGE_SEC", "120"))


def _check_data_freshness(conn) -> bool:
    """Return True if data is fresh enough to trade. Warn loudly + auto-start collector if stale."""
    global _last_stale_warn_ts, _data_collector_pid
    now = time.time()
    try:
        row = fetch_one(conn, "SELECT MAX(ts) FROM btc_ticks")
        if not row or row[0] is None:
            _warn_stale(now, "No btc_ticks data at all!")
            _maybe_start_collector()
            return False
        latest = float(row[0])
        age = now - latest
        if age > PAPER_DATA_MAX_AGE_SEC:
            _warn_stale(now, f"btc_ticks data is {age:.0f}s old (max {PAPER_DATA_MAX_AGE_SEC:.0f}s)")
            _maybe_start_collector()
            return False
        return True
    except Exception as e:
        logger.error("Data freshness check error: %s", e)
        return False


def _warn_stale(now: float, msg: str):
    global _last_stale_warn_ts
    if now - _last_stale_warn_ts >= _DATA_STALE_WARN_INTERVAL:
        logger.warning("DATA STALE: %s -- no trading possible until data_collector feeds fresh data!", msg)
        _last_stale_warn_ts = now


def _maybe_start_collector():
    """Auto-start data_collector.py if not already running."""
    global _data_collector_pid
    if not PAPER_AUTO_START_COLLECTOR:
        return
    # Check if our previously launched collector is still alive
    if _data_collector_pid is not None:
        try:
            os.kill(_data_collector_pid, 0)  # signal 0 = check existence
            return  # still running
        except OSError:
            _data_collector_pid = None  # dead, try again

    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_collector.py")
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _data_collector_pid = proc.pid
        logger.warning("Auto-started data_collector.py (PID %d)", proc.pid)
    except Exception as e:
        logger.error("Failed to auto-start data_collector: %s", e)


def _paper_telegram_ready() -> tuple[bool, str, str]:
    global _PAPER_TELEGRAM_WARNED_NOT_READY
    enabled = bool(getattr(config.trading, "paper_telegram_notify_open", False))
    token = str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
    chat_id = str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
    ready = bool(enabled and token and chat_id)
    if enabled and not ready and not _PAPER_TELEGRAM_WARNED_NOT_READY:
        _PAPER_TELEGRAM_WARNED_NOT_READY = True
        logger.warning(
            "Paper Telegram notify enabled, but live Telegram token/chat_id is missing."
        )
    if ready:
        _PAPER_TELEGRAM_WARNED_NOT_READY = False
    return ready, token, chat_id


def _format_paper_open_telegram(
    *,
    window_slug: str,
    window_start: int,
    direction: str,
    stake: float,
    entry_price: float,
    up_ask: float | None,
    down_ask: float | None,
    btc_start: float | None,
    btc_now: float | None,
    confidence: float,
    reason: str,
) -> str:
    to_win_total = (stake / entry_price) if (stake > 0.0 and 0.0 < entry_price < 1.0) else 0.0
    expected_pnl = max(0.0, to_win_total - stake)
    now_utc = datetime.now(timezone.utc).isoformat()
    reason_text = str(reason or "").strip().replace("\n", " ")
    if len(reason_text) > 260:
        reason_text = f"{reason_text[:257]}..."
    return (
        "[PAPER OPEN]\n"
        f"time(UTC): {now_utc}\n"
        f"side: {direction}\n"
        f"slug: {window_slug}\n"
        f"window_start: {window_start}\n"
        f"stake: ${float(stake):,.2f}\n"
        f"entry odds: {float(entry_price):.3f}\n"
        f"Polymarket ask (UP/DOWN): "
        f"{f'{float(up_ask):.3f}' if up_ask is not None else '--'} / "
        f"{f'{float(down_ask):.3f}' if down_ask is not None else '--'}\n"
        f"5m start price: {f'{float(btc_start):,.2f}' if btc_start is not None else '--'}\n"
        f"current BTC: {f'{float(btc_now):,.2f}' if btc_now is not None else '--'}\n"
        f"to-win total: ${to_win_total:,.2f}\n"
        f"expected pnl: ${expected_pnl:,.2f}\n"
        f"confidence: {float(confidence):.3f}\n"
        f"reason: {reason_text or '--'}"
    )


def _send_paper_open_telegram(
    *,
    window_slug: str,
    window_start: int,
    direction: str,
    stake: float,
    entry_price: float,
    up_ask: float | None,
    down_ask: float | None,
    btc_start: float | None,
    btc_now: float | None,
    confidence: float,
    reason: str,
):
    ready, token, chat_id = _paper_telegram_ready()
    if not ready:
        return
    try:
        text = _format_paper_open_telegram(
            window_slug=window_slug,
            window_start=window_start,
            direction=direction,
            stake=stake,
            entry_price=entry_price,
            up_ask=up_ask,
            down_ask=down_ask,
            btc_start=btc_start,
            btc_now=btc_now,
            confidence=confidence,
            reason=reason,
        )
        result = send_telegram_message(
            token=token,
            chat_id=chat_id,
            text=text,
            timeout=8.0,
            auto_resolve_chat=False,
        )
        if not bool(result.get("ok")):
            logger.warning(
                "Paper Telegram send failed: %s",
                result.get("error") or "unknown",
            )
    except Exception as e:
        logger.warning("Paper Telegram send exception: %s", e)


def _format_paper_close_telegram(
    *,
    close_type: str,
    window_slug: str,
    window_start: int,
    direction: str,
    stake: float,
    entry_price: float,
    btc_start: float | None,
    btc_end: float | None,
    btc_exit: float | None,
    outcome: str | None,
    up_ask: float | None,
    down_ask: float | None,
    exit_price: float | None,
    pnl: float,
    roi_pct: float,
    reason: str,
) -> str:
    now_utc = datetime.now(timezone.utc).isoformat()
    status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
    reason_text = str(reason or "").strip().replace("\n", " ")
    if len(reason_text) > 320:
        reason_text = f"{reason_text[:317]}..."
    settle_price = 1.0 if outcome and str(outcome).upper() == str(direction).upper() else 0.0
    return (
        f"[PAPER CLOSE:{close_type.upper()}] {status}\n"
        f"time(UTC): {now_utc}\n"
        f"side: {direction}\n"
        f"slug: {window_slug}\n"
        f"window_start: {window_start}\n"
        f"stake: ${float(stake):,.2f}\n"
        f"entry odds: {float(entry_price):.3f}\n"
        f"exit odds: {f'{float(exit_price):.3f}' if exit_price is not None else '--'}\n"
        f"settlement odds: {f'{float(settle_price):.3f}' if outcome else '--'}\n"
        f"Polymarket ask(close UP/DOWN): "
        f"{f'{float(up_ask):.3f}' if up_ask is not None else '--'} / "
        f"{f'{float(down_ask):.3f}' if down_ask is not None else '--'}\n"
        f"5m start/end(Binance): "
        f"{f'{float(btc_start):,.2f}' if btc_start is not None else '--'} / "
        f"{f'{float(btc_end):,.2f}' if btc_end is not None else '--'}\n"
        f"BTC at exit: {f'{float(btc_exit):,.2f}' if btc_exit is not None else '--'}\n"
        f"outcome: {str(outcome or '--').upper()}\n"
        f"realized pnl: ${float(pnl):,.2f} ({float(roi_pct):+.2f}%)\n"
        f"reason: {reason_text or '--'}"
    )


def _send_paper_close_telegram(
    *,
    close_type: str,
    window_slug: str,
    window_start: int,
    direction: str,
    stake: float,
    entry_price: float,
    btc_start: float | None,
    btc_end: float | None,
    btc_exit: float | None,
    outcome: str | None,
    up_ask: float | None,
    down_ask: float | None,
    exit_price: float | None,
    pnl: float,
    roi_pct: float,
    reason: str,
):
    ready, token, chat_id = _paper_telegram_ready()
    if not ready:
        return
    try:
        text = _format_paper_close_telegram(
            close_type=close_type,
            window_slug=window_slug,
            window_start=window_start,
            direction=direction,
            stake=stake,
            entry_price=entry_price,
            btc_start=btc_start,
            btc_end=btc_end,
            btc_exit=btc_exit,
            outcome=outcome,
            up_ask=up_ask,
            down_ask=down_ask,
            exit_price=exit_price,
            pnl=pnl,
            roi_pct=roi_pct,
            reason=reason,
        )
        result = send_telegram_message(
            token=token,
            chat_id=chat_id,
            text=text,
            timeout=8.0,
            auto_resolve_chat=False,
        )
        if not bool(result.get("ok")):
            logger.warning(
                "Paper Telegram close send failed: %s",
                result.get("error") or "unknown",
            )
    except Exception as e:
        logger.warning("Paper Telegram close send exception: %s", e)


PAPER_RISK_FRACTION = float(os.getenv("PAPER_RISK_FRACTION", "0.20"))
PAPER_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.020"))
PAPER_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.50"))
PAPER_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.22"))
PAPER_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.52"))
PAPER_MIN_BET = float(os.getenv("PAPER_MIN_BET", "25"))
PAPER_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
PAPER_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
PAPER_DOWN_ENTRY_END_SEC = float(os.getenv("PAPER_DOWN_ENTRY_END_SEC", "160"))
PAPER_DOWN_MIN_ENTRY_PRICE = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.42"))
PAPER_MIN_SECONDS_REMAINING = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
PAPER_MIN_TICK_SAMPLES = int(os.getenv("PAPER_MIN_TICK_SAMPLES", "100"))
PAPER_MIN_ODDS_SAMPLES = int(os.getenv("PAPER_MIN_ODDS_SAMPLES", "16"))
PAPER_RECENT_MOVE_LOOKBACK_SEC = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
PAPER_MIN_RECENT_MOVE_PCT = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.006"))
PAPER_MIN_BOUNDARY_DIST_PCT = float(os.getenv("PAPER_MIN_BOUNDARY_DIST_PCT", "0.040"))
PAPER_DOWN_MIN_BOUNDARY_DIST_PCT = float(os.getenv("PAPER_DOWN_MIN_BOUNDARY_DIST_PCT", "0.050"))
# Dynamic minimum trade gap:
# - base gap is permissive enough to allow a strong signal in the next 5m window
# - adaptive gap expands toward target gap when performance deteriorates
PAPER_BASE_TRADE_GAP_SEC = float(os.getenv("PAPER_BASE_TRADE_GAP_SEC", "120"))
PAPER_TARGET_TRADE_GAP_SEC = float(os.getenv("PAPER_TARGET_TRADE_GAP_SEC", "600"))
PAPER_MAX_DRAWDOWN_STOP_PCT = float(os.getenv("PAPER_MAX_DRAWDOWN_STOP_PCT", "0.20"))
PAPER_RECENT_PERF_WINDOW = int(os.getenv("PAPER_RECENT_PERF_WINDOW", "8"))
PAPER_MIN_RECENT_WIN_RATE = float(os.getenv("PAPER_MIN_RECENT_WIN_RATE", "0.55"))
PAPER_REQUIRE_UNANIMOUS = os.getenv("PAPER_REQUIRE_UNANIMOUS", "false").lower() == "true"
PAPER_HIGH_QUALITY_EV = float(os.getenv("PAPER_HIGH_QUALITY_EV", "0.12"))
PAPER_HIGH_QUALITY_CONF = float(os.getenv("PAPER_HIGH_QUALITY_CONF", "0.50"))
PAPER_SIZING_MODE = str(os.getenv("PAPER_SIZING_MODE", "adaptive")).strip().lower()
PAPER_PROFIT_MODE = str(os.getenv("PAPER_PROFIT_MODE", "aggressive")).strip().lower()
PAPER_AGGRESSIVE_ENTRY_RELAX = float(os.getenv("PAPER_AGGRESSIVE_ENTRY_RELAX", "0.20"))
PAPER_AGGRESSIVE_GAP_MULT = float(os.getenv("PAPER_AGGRESSIVE_GAP_MULT", "0.65"))
PAPER_AGGRESSIVE_MAX_BET_FRAC = float(os.getenv("PAPER_AGGRESSIVE_MAX_BET_FRAC", "0.12"))
PAPER_AGGRESSIVE_KELLY_FRAC = float(os.getenv("PAPER_AGGRESSIVE_KELLY_FRAC", "0.50"))
PAPER_AGGRESSIVE_LOSS_DEBOOST = float(os.getenv("PAPER_AGGRESSIVE_LOSS_DEBOOST", "0.82"))
# Anti-freeze controls: gradually relax strictness when no entry happens for long.
PAPER_STALE_RELAX_START_SEC = float(os.getenv("PAPER_STALE_RELAX_START_SEC", "2400"))
PAPER_STALE_RELAX_FULL_SEC = float(os.getenv("PAPER_STALE_RELAX_FULL_SEC", "9000"))
PAPER_STALE_RELAX_MAX = float(os.getenv("PAPER_STALE_RELAX_MAX", "0.50"))
PAPER_STRICTNESS_UNANIMOUS_AT = float(os.getenv("PAPER_STRICTNESS_UNANIMOUS_AT", "0.90"))
PAPER_ADAPTIVE_MAX_ASK_FLOOR = float(os.getenv("PAPER_ADAPTIVE_MAX_ASK_FLOOR", "0.47"))
PAPER_PERF_PAUSE_SEC = float(os.getenv("PAPER_PERF_PAUSE_SEC", "1800"))

# Lag probability edge: model_prob must exceed normalized market prob by this margin.
# Matched to backtest's live_min_lag_prob_edge for entry quality.
PAPER_MIN_LAG_PROB_EDGE = float(os.getenv("PAPER_MIN_LAG_PROB_EDGE", "0.020"))

# Direction consistency filter (market-implied probability alignment).
PAPER_MAX_OPPOSITE_IMPLIED = float(os.getenv("PAPER_MAX_OPPOSITE_IMPLIED", "0.62"))
PAPER_MIN_ENTRY_SIDE_IMPLIED = float(os.getenv("PAPER_MIN_ENTRY_SIDE_IMPLIED", "0.38"))
# If opposite ask is materially higher than selected side ask, skip unless model/confidence are very strong.
PAPER_MAX_CONTRA_GAP = float(os.getenv("PAPER_MAX_CONTRA_GAP", "0.50"))
PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB", "0.66"))
PAPER_CONTRA_OVERRIDE_MIN_CONF = float(os.getenv("PAPER_CONTRA_OVERRIDE_MIN_CONF", "0.75"))
# Additional trend alignment to avoid entering on short-lived flips near local extrema.
PAPER_TREND_ALIGN_LOOKBACK_SEC = float(os.getenv("PAPER_TREND_ALIGN_LOOKBACK_SEC", "75"))
PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT = float(os.getenv("PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT", "0.004"))

# Macro trend filter: block counter-trend entries when BTC has a clear
# multi-window directional trend.  Lookback crosses window boundaries.
PAPER_MACRO_TREND_LOOKBACK_SEC = float(os.getenv("PAPER_MACRO_TREND_LOOKBACK_SEC", "900"))
PAPER_MACRO_TREND_BLOCK_PCT = float(os.getenv("PAPER_MACRO_TREND_BLOCK_PCT", "0.040"))
PAPER_MACRO_TREND_EXTRA_EV = float(os.getenv("PAPER_MACRO_TREND_EXTRA_EV", "0.04"))

# DOWN-side hardening when BTC is above the window start.
PAPER_DOWN_ABOVE_START_BLOCK_PCT = float(os.getenv("PAPER_DOWN_ABOVE_START_BLOCK_PCT", "0.050"))
PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA = float(os.getenv("PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA", "0.006"))
PAPER_DOWN_ABOVE_START_EV_PENALTY = float(os.getenv("PAPER_DOWN_ABOVE_START_EV_PENALTY", "0.020"))

# Early exit rules for open paper positions.
PAPER_ENABLE_EARLY_EXIT = os.getenv("PAPER_ENABLE_EARLY_EXIT", "true").lower() == "true"
PAPER_EARLY_EXIT_MIN_ELAPSED_SEC = float(os.getenv("PAPER_EARLY_EXIT_MIN_ELAPSED_SEC", "25"))
PAPER_EARLY_EXIT_OPPOSITE_ASK = float(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_ASK", "0.78"))
PAPER_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT", "-20.0")
)
PAPER_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS = int(os.getenv("PAPER_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS", "3"))
PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT", "-85.0"))
PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC", "35"))
PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF", "0.75")
)
PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC", "20")
)
PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF", "0.60")
)
PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT", "15")
)
PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE = (
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE", "true").lower() == "true"
)
PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT", "0.090")
)
PAPER_EARLY_EXIT_MAX_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_MAX_HOLD_SEC", "220"))
PAPER_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC = float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC", "20"))
PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT", "-8.0"))
# Trailing stop: exit when ROI drops by this many % from peak ROI.
# Set to 999 via env to effectively disable (binary market hold-to-expiry strategy).
PAPER_EARLY_EXIT_TRAILING_STOP_DROP_PCT = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_DROP_PCT", "999"))
PAPER_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT", "10.0"))
PAPER_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC", "20"))
# Profit take: exit immediately when ROI exceeds threshold.
PAPER_EARLY_EXIT_PROFIT_TAKE_ROI_PCT = float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_ROI_PCT", "65.0"))
PAPER_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC = float(os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC", "20"))
PAPER_TIME_WEIGHTED_EXIT = os.getenv("PAPER_TIME_WEIGHTED_EXIT", "true").lower() == "true"
PAPER_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA", "0.10")
)
PAPER_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT", "18.0")
)
PAPER_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT", "12.0")
)
PAPER_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT", "14.0")
)
PAPER_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT", "18.0")
)
PAPER_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT", "25.0")
)
PAPER_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT = float(
    os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT", "0.90")
)
PAPER_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT", "0.020")
)
PAPER_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC", "60")
)
PAPER_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT", "-8.0")
)
PAPER_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC", "135")
)
PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT", "1.35")
)
PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT", "0.060")
)
PAPER_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC", "140")
)
PAPER_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT", "95")
)
PAPER_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC", "120")
)
PAPER_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT", "90")
)
PAPER_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC", "115")
)
PAPER_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT = float(
    os.getenv("PAPER_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT", "110")
)
PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_OPPOSITE_ASK = float(
    os.getenv("PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_OPPOSITE_ASK", "0.05")
)
PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_MIN_HOLD_SEC = float(
    os.getenv("PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_MIN_HOLD_SEC", "10")
)

# In-memory debounce for noisy opposite-probability spikes.
_EARLY_EXIT_OPPOSITE_HITS: dict[int, int] = {}
# Track peak ROI per trade for trailing stop.
_PEAK_ROI_PER_TRADE: dict[int, float] = {}
# Smart exit: track last jury re-evaluation time per trade.
_SMART_EXIT_LAST_CHECK: dict[int, float] = {}


def _paper_exit_policy_config() -> ExitPolicyConfig:
    return ExitPolicyConfig(
        enabled=bool(PAPER_ENABLE_EARLY_EXIT),
        min_elapsed_sec=float(PAPER_EARLY_EXIT_MIN_ELAPSED_SEC),
        opposite_ask=float(PAPER_EARLY_EXIT_OPPOSITE_ASK),
        opposite_min_loss_roi_pct=float(PAPER_EARLY_EXIT_OPPOSITE_MIN_LOSS_ROI_PCT),
        opposite_confirm_polls=int(PAPER_EARLY_EXIT_OPPOSITE_CONFIRM_POLLS),
        stop_loss_roi_pct=float(PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT),
        stop_loss_min_hold_sec=float(PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC),
        stop_loss_high_conf_cutoff=float(PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF),
        stop_loss_high_conf_min_hold_sec=float(PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC),
        stop_loss_low_conf_cutoff=float(PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF),
        stop_loss_low_conf_relax_pct=float(PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT),
        stop_loss_require_btc_adverse=bool(PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE),
        stop_loss_btc_adverse_pct=float(PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT),
        max_hold_sec=float(PAPER_EARLY_EXIT_MAX_HOLD_SEC),
        timestop_max_remain_sec=float(PAPER_EARLY_EXIT_TIMESTOP_MAX_REMAIN_SEC),
        timestop_max_roi_pct=float(PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT),
        trailing_stop_drop_pct=float(PAPER_EARLY_EXIT_TRAILING_STOP_DROP_PCT),
        trailing_stop_min_peak_pct=float(PAPER_EARLY_EXIT_TRAILING_STOP_MIN_PEAK_PCT),
        trailing_stop_min_hold_sec=float(PAPER_EARLY_EXIT_TRAILING_STOP_MIN_HOLD_SEC),
        profit_take_roi_pct=float(PAPER_EARLY_EXIT_PROFIT_TAKE_ROI_PCT),
        profit_take_min_hold_sec=float(PAPER_EARLY_EXIT_PROFIT_TAKE_MIN_HOLD_SEC),
        time_weight_enabled=bool(PAPER_TIME_WEIGHTED_EXIT),
        early_opposite_ask_extra=float(PAPER_EARLY_EXIT_EARLY_OPPOSITE_ASK_EXTRA),
        early_opposite_loss_extra_pct=float(PAPER_EARLY_EXIT_EARLY_OPPOSITE_LOSS_EXTRA_PCT),
        early_stop_loss_extra_pct=float(PAPER_EARLY_EXIT_EARLY_STOP_LOSS_EXTRA_PCT),
        early_trailing_drop_extra_pct=float(PAPER_EARLY_EXIT_EARLY_TRAILING_DROP_EXTRA_PCT),
        early_trailing_peak_extra_pct=float(PAPER_EARLY_EXIT_EARLY_TRAILING_PEAK_EXTRA_PCT),
        early_profit_take_extra_pct=float(PAPER_EARLY_EXIT_EARLY_PROFIT_TAKE_EXTRA_PCT),
        strong_favor_sigma_mult=float(PAPER_EARLY_EXIT_STRONG_FAVOR_SIGMA_MULT),
        strong_favor_min_move_pct=float(PAPER_EARLY_EXIT_STRONG_FAVOR_MIN_MOVE_PCT),
        favor_hold_min_remaining_sec=float(PAPER_EARLY_EXIT_FAVOR_HOLD_MIN_REMAINING_SEC),
        favor_hold_break_even_floor_roi_pct=float(PAPER_EARLY_EXIT_FAVOR_HOLD_BREAK_EVEN_FLOOR_ROI_PCT),
        opposite_late_only_remaining_sec=float(PAPER_EARLY_EXIT_OPPOSITE_LATE_ONLY_REMAINING_SEC),
        opposite_severe_adverse_sigma_mult=float(PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_SIGMA_MULT),
        opposite_severe_adverse_min_move_pct=float(PAPER_EARLY_EXIT_OPPOSITE_SEVERE_ADVERSE_MIN_MOVE_PCT),
        trailing_late_only_remaining_sec=float(PAPER_EARLY_EXIT_TRAILING_LATE_ONLY_REMAINING_SEC),
        trailing_force_peak_pct=float(PAPER_EARLY_EXIT_TRAILING_FORCE_PEAK_PCT),
        break_even_late_only_remaining_sec=float(PAPER_EARLY_EXIT_BREAK_EVEN_LATE_ONLY_REMAINING_SEC),
        break_even_force_peak_pct=float(PAPER_EARLY_EXIT_BREAK_EVEN_FORCE_PEAK_PCT),
        profit_take_late_only_remaining_sec=float(PAPER_EARLY_EXIT_PROFIT_TAKE_LATE_ONLY_REMAINING_SEC),
        profit_take_force_roi_pct=float(PAPER_EARLY_EXIT_PROFIT_TAKE_FORCE_ROI_PCT),
        near_certain_win_opposite_ask=float(PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_OPPOSITE_ASK),
        near_certain_win_min_hold_sec=float(PAPER_EARLY_EXIT_NEAR_CERTAIN_WIN_MIN_HOLD_SEC),
    )


def init_paper_table(conn):
    sql = """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        window_start BIGINT NOT NULL UNIQUE,
        window_end BIGINT NOT NULL,
        direction VARCHAR(16) NOT NULL,
        stake DOUBLE NOT NULL,
        entry_price DOUBLE NOT NULL,
        payout_multiple DOUBLE NOT NULL,
        shares DOUBLE NOT NULL,
        potential_win_pnl DOUBLE NOT NULL,
        signal_confidence DOUBLE NOT NULL,
        signal_reason TEXT NULL,
        close_reason TEXT NULL,
        initial_capital DOUBLE NULL,
        risk_fraction DOUBLE NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
        opened_at DOUBLE NOT NULL,
        closed_at DOUBLE NULL,
        actual_outcome VARCHAR(16) NULL,
        won TINYINT NULL,
        pnl DOUBLE NULL,
        roi_pct DOUBLE NULL,
        INDEX idx_paper_status (status),
        INDEX idx_paper_closed (closed_at)
    ) ENGINE=InnoDB
    """
    execute_write(conn, sql)
    # Lightweight schema migration for existing deployments.
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN initial_capital REAL")
    except Exception:
        pass
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN risk_fraction REAL")
    except Exception:
        pass
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN close_reason TEXT")
    except Exception:
        pass
    try:
        execute_write(conn, "ALTER TABLE paper_trades ADD COLUMN archived_at REAL")
    except Exception:
        pass
    conn.commit()


def _equity_snapshot(conn, initial_capital: float) -> tuple[float, float]:
    closed_pnl_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status='CLOSED' AND archived_at IS NULL",
    )
    open_notional_row = fetch_one(
        conn,
        "SELECT COALESCE(SUM(stake), 0) FROM paper_trades WHERE status='OPEN' AND archived_at IS NULL",
    )
    closed_pnl = float(closed_pnl_row[0] or 0.0) if closed_pnl_row else 0.0
    open_notional = float(open_notional_row[0] or 0.0) if open_notional_row else 0.0
    realized_equity = initial_capital + closed_pnl
    available_equity = max(0.0, realized_equity - open_notional)
    return realized_equity, available_equity


def _compute_bet_size(
    available_equity: float,
    initial_capital: float,
    expected_roi: float,
    risk_fraction: float,
    entry_price: float | None = None,
    model_prob: float | None = None,
    confidence: float | None = None,
    seconds_elapsed: float | None = None,
    max_edge: float | None = None,
) -> float:
    """Adaptive bet sizing: 5-15% of equity based on conviction.
    Matched to RiskManager.compute_bet_size -- uses max_edge for conviction,
    caps at 20% of equity to prevent runaway bet sizes."""
    if available_equity <= 0:
        return 0.0

    # Use max_edge (jury edge) for conviction, matching backtest's risk_mgr
    edge = _clamp(float(max_edge or expected_roi), 0.0, 0.30)
    conf = _clamp(float(confidence or 0.5), 0.0, 1.0)
    conviction = edge * conf
    # Normalize: 0.02 = weak, 0.12+ = very strong
    conv_norm = _clamp((conviction - 0.02) / 0.10, 0.0, 1.0)

    # Lerp 5%-15% of equity
    BET_PCT_MIN = 0.05
    BET_PCT_MAX = 0.15
    bet_pct = BET_PCT_MIN + conv_norm * (BET_PCT_MAX - BET_PCT_MIN)
    sized = available_equity * bet_pct

    # Time-graduated sizing: closer to expiry = more certain = bigger bet
    # 150s: 1.0x, 200s: 1.25x, 240s: 1.5x
    if seconds_elapsed is not None:
        _time_mult = 1.0 + _clamp((float(seconds_elapsed) - 150) / 180, 0.0, 0.5)
        sized *= _time_mult
    # Cap at 20% of equity (matched to backtest ceiling)
    max_bet = available_equity * 0.20
    return round(max(PAPER_MIN_BET, min(sized, max_bet)), 2)


def _recent_risk_state(conn) -> tuple[int, float]:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl
           FROM paper_trades
           WHERE status='CLOSED' AND archived_at IS NULL
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

    losses = sum(1 for row in rows if float(row.get("pnl") or 0.0) < 0.0)
    loss_rate = losses / float(len(rows))
    return loss_streak, loss_rate


def _recent_performance(conn, limit: int) -> tuple[int, float, float]:
    lim = max(1, int(limit))
    rows = fetch_all_dicts(
        conn,
        """SELECT won, pnl
           FROM paper_trades
           WHERE status='CLOSED' AND archived_at IS NULL
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


def _last_opened_at(conn) -> float:
    row = fetch_one(conn, "SELECT MAX(opened_at) FROM paper_trades WHERE archived_at IS NULL")
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except Exception:
        return 0.0


def _last_closed_at(conn) -> float:
    row = fetch_one(
        conn,
        """SELECT MAX(
               CASE
                 WHEN closed_at IS NOT NULL THEN closed_at
                 ELSE window_end
               END
             )
           FROM paper_trades
           WHERE status='CLOSED' AND archived_at IS NULL""",
    )
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except Exception:
        return 0.0


def _stale_relax_factor(last_opened_at: float, now_ts: float) -> float:
    if last_opened_at <= 0:
        return 0.0
    idle_sec = max(0.0, float(now_ts) - float(last_opened_at))
    start_sec = max(0.0, PAPER_STALE_RELAX_START_SEC)
    full_sec = max(start_sec + 1.0, PAPER_STALE_RELAX_FULL_SEC)
    if idle_sec <= start_sec:
        return 0.0
    span = full_sec - start_sec
    return _clamp((idle_sec - start_sec) / span, 0.0, 1.0)


def _equity_drawdown_pct(conn, initial_capital: float) -> float:
    rows = fetch_all_dicts(
        conn,
        """SELECT pnl
           FROM paper_trades
           WHERE status='CLOSED' AND archived_at IS NULL
           ORDER BY
             CASE
               WHEN closed_at IS NOT NULL THEN closed_at
               ELSE window_end
             END ASC,
             id ASC""",
    )
    if not rows:
        return 0.0

    equity = initial_capital
    peak = initial_capital
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


def _window_sample_counts(conn, window_start: int, now_ts: float) -> tuple[int, int]:
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


def _recent_move_pct(conn, window_start: int, now_ts: float, lookback_sec: float) -> float | None:
    lo_ts = max(float(window_start), float(now_ts) - max(1.0, float(lookback_sec)))
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


def _macro_trend_pct(conn, now_ts: float, lookback_sec: float = 900.0) -> float | None:
    """BTC move % over the last *lookback_sec* seconds, crossing window boundaries.

    Unlike _recent_move_pct which is clamped to the current window,
    this looks at absolute BTC price history to capture the macro trend.
    """
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


def _recent_price_series(conn, now_ts: float, lookback_sec: float = 600.0) -> tuple[list[float], list[float]]:
    lo_ts = max(0.0, float(now_ts) - max(30.0, float(lookback_sec)))
    rows = fetch_all_dicts(
        conn,
        """SELECT ts, price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (lo_ts, float(now_ts)),
    )
    ts_list: list[float] = []
    prices: list[float] = []
    for r in rows:
        try:
            ts_val = float(r.get("ts"))
            px_val = float(r.get("price"))
            if px_val > 0.0:
                ts_list.append(ts_val)
                prices.append(px_val)
        except Exception:
            continue
    return ts_list, prices


def _safe_prob(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if 0.0 < v < 1.0:
        return v
    return None


def _latest_odds_for_window(conn, window_start: int) -> dict | None:
    return fetch_one_dict(
        conn,
        """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
           FROM poly_odds
           WHERE window_start = ?
           ORDER BY ts DESC
           LIMIT 1""",
        (int(window_start),),
    )


def _mark_to_market(
    *,
    direction: str,
    stake: float,
    shares: float,
    odds_row: dict | None,
) -> tuple[float, float, float, float]:
    """
    Returns:
    - exit_price
    - current_value
    - pnl_after_fee
    - roi_pct
    """
    exit_price = None
    if odds_row:
        if direction == "UP":
            exit_price = _safe_prob(odds_row.get("up_best_bid")) or _safe_prob(odds_row.get("up_mid"))
        else:
            exit_price = _safe_prob(odds_row.get("down_best_bid")) or _safe_prob(odds_row.get("down_mid"))
    if exit_price is None:
        exit_price = 0.5

    current_value = float(shares * exit_price)
    raw_pnl = float(current_value - stake)
    pnl = float(apply_fee_to_pnl(raw_pnl, stake))
    roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
    return float(exit_price), float(current_value), float(pnl), float(roi_pct)


def _close_trade_early(
    conn,
    *,
    trade_id: int,
    window_start: int,
    direction: str,
    stake: float,
    shares: float,
    entry_price: float,
    reason: str,
    odds_row: dict | None,
) -> bool:
    exit_price, _value, pnl, roi_pct = _mark_to_market(
        direction=direction,
        stake=stake,
        shares=shares,
        odds_row=odds_row,
    )
    won = 1 if pnl > 0 else 0
    closed_at = time.time()
    execute_write(
        conn,
        """UPDATE paper_trades
           SET status='CLOSED',
               closed_at=?,
               actual_outcome=NULL,
               won=?,
               pnl=?,
               roi_pct=?,
               close_reason=?
           WHERE id=?""",
        (closed_at, won, pnl, roi_pct, str(reason), int(trade_id)),
    )
    logger.warning(
        "EARLY ws=%s id=%s dir=%s reason=%s exit_px=%.3f pnl=$%+.2f roi=%+.2f%%",
        window_start,
        trade_id,
        direction,
        reason,
        exit_price,
        pnl,
        roi_pct,
    )
    # Get current BTC price at the moment of exit
    btc_exit_price = _price_at_or_near(conn, closed_at, prefer_before=True)
    if btc_exit_price is not None:
        logger.warning(
            "  BTC at exit: $%.2f", btc_exit_price,
        )
    window_row = fetch_one_dict(
        conn,
        """SELECT slug, btc_start_price, btc_end_price, actual_outcome
           FROM market_windows
           WHERE window_start = ?
           LIMIT 1""",
        (int(window_start),),
    )
    up_ask = _safe_prob(odds_row.get("up_best_ask")) or _safe_prob(odds_row.get("up_mid")) if odds_row else None
    down_ask = _safe_prob(odds_row.get("down_best_ask")) or _safe_prob(odds_row.get("down_mid")) if odds_row else None
    _send_paper_close_telegram(
        close_type="early_exit",
        window_slug=str((window_row or {}).get("slug") or f"window-{window_start}"),
        window_start=int(window_start),
        direction=str(direction),
        stake=float(stake),
        entry_price=float(entry_price),
        btc_start=float((window_row or {}).get("btc_start_price")) if (window_row and window_row.get("btc_start_price") is not None) else None,
        btc_end=float((window_row or {}).get("btc_end_price")) if (window_row and window_row.get("btc_end_price") is not None) else None,
        btc_exit=float(btc_exit_price) if btc_exit_price is not None else None,
        outcome=str((window_row or {}).get("actual_outcome")) if (window_row and window_row.get("actual_outcome")) else None,
        up_ask=float(up_ask) if up_ask is not None else None,
        down_ask=float(down_ask) if down_ask is not None else None,
        exit_price=float(exit_price),
        pnl=float(pnl),
        roi_pct=float(roi_pct),
        reason=str(reason),
    )
    return True


def _price_at_or_near(conn, ts: float, *, prefer_before: bool) -> float | None:
    if prefer_before:
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
    else:
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
        row = fetch_one(
            conn,
            "SELECT price FROM btc_ticks WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (float(ts),),
        )
        if row and row[0] is not None:
            return float(row[0])
    return None


def _backfill_unresolved_windows(conn) -> int:
    now_ts = time.time()
    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, window_end, btc_start_price
           FROM market_windows
           WHERE window_end < ?
             AND (actual_outcome IS NULL OR actual_outcome NOT IN ('UP', 'DOWN'))
           ORDER BY window_start ASC
           LIMIT 40""",
        (now_ts,),
    )
    if not rows:
        return 0

    updated = 0
    for row in rows:
        ws = int(row["window_start"])
        we = int(row["window_end"])

        start_price = float(row["btc_start_price"]) if row.get("btc_start_price") is not None else None
        if start_price is None:
            start_price = _price_at_or_near(conn, float(ws), prefer_before=False)

        # --- Try Gamma API first (authoritative settlement prices) ---
        gamma_ptb = None
        gamma_final = None
        gamma_outcome = None
        try:
            import httpx
            slug = f"btc-updown-5m-{ws}"
            resp = httpx.get(
                f"https://gamma-api.polymarket.com/events?slug={slug}&limit=1",
                timeout=5,
            )
            events = resp.json()
            if events:
                meta = events[0].get("eventMetadata") or {}
                if meta.get("priceToBeat") is not None and meta.get("finalPrice") is not None:
                    gamma_ptb = float(meta["priceToBeat"])
                    gamma_final = float(meta["finalPrice"])
                    gamma_outcome = "UP" if gamma_final >= gamma_ptb else "DOWN"
        except Exception:
            pass  # Fall back to btc_ticks

        if gamma_outcome is not None:
            outcome = gamma_outcome
            end_price = gamma_final
            if gamma_ptb is not None and start_price is not None:
                start_price = gamma_ptb  # Use authoritative PTB too
            logger.info(
                "Backfilled %s from Gamma API: ptb=$%.2f final=$%.2f outcome=%s",
                slug, gamma_ptb or 0, gamma_final or 0, outcome,
            )
        else:
            # Fallback: use last CLOB odds near expiry (93%+ accurate).
            # Much better than btc_ticks (Binance has $10-15 offset from Chainlink).
            _last_odds = fetch_one_dict(
                conn,
                """SELECT up_best_ask, down_best_ask FROM poly_odds
                   WHERE window_start = ? AND ts >= ? AND ts <= ?
                   ORDER BY ts DESC LIMIT 1""",
                (ws, float(we) - 10, float(we) + 5),
            )
            if _last_odds:
                _lo_up = float(_last_odds.get("up_best_ask") or 0.5)
                _lo_dn = float(_last_odds.get("down_best_ask") or 0.5)
                # Higher ask = market thinks that side wins
                outcome = "UP" if _lo_up > _lo_dn else "DOWN"
                end_price = _price_at_or_near(conn, float(we), prefer_before=True) or 0
                logger.info(
                    "Backfilled %d from CLOB odds (Gamma unavailable): up_ask=%.3f down_ask=%.3f outcome=%s",
                    ws, _lo_up, _lo_dn, outcome,
                )
            else:
                # No odds data either - skip, data_collector will backfill later
                continue

        execute_write(
            conn,
            """UPDATE market_windows
               SET btc_start_price = COALESCE(btc_start_price, ?),
                   btc_end_price = ?,
                   actual_outcome = ?
               WHERE window_start = ?""",
            (float(start_price), float(end_price), outcome, ws),
        )
        updated += 1

    if updated:
        conn.commit()
        logger.warning("Backfilled %s unresolved window outcomes from btc_ticks", updated)
    return updated


# Module-level Jury instance (same config as Live)
def _read_signal_cache(conn) -> dict | None:
    """Read shared Jury signal from signal_cache (written by data_collector).
    Returns None if signal is stale (>2s old) or missing."""
    row = fetch_one_dict(conn, "SELECT * FROM signal_cache WHERE id = 1")
    if not row:
        return None
    age = time.time() - float(row.get("ts") or 0)
    if age > 5.0:
        return None
    return row


def open_trade_if_signal(
    conn,
    initial_capital: float,
    risk_fraction: float,
    sizing_mode: str,
) -> bool:
    # -- Read shared signal from data_collector's Jury (via DB) --
    cached = _read_signal_cache(conn)
    if cached is None:
        return False

    direction = str(cached.get("direction", "NO_TRADE"))
    if direction not in ("UP", "DOWN"):
        return False

    now_ts = time.time()
    window_start = int(cached.get("window_start") or 0)
    if window_start <= 0:
        return False
    interval = int(getattr(config.polymarket, "interval_seconds", 300))
    # Verify signal is for the CURRENT window (not stale from previous)
    current_ws = int(now_ts // interval) * interval
    if window_start != current_ws:
        return False
    window_end = window_start + interval
    window_slug = f"{config.polymarket.market_slug_prefix}-{window_start}"

    seconds_elapsed = float(cached.get("seconds_elapsed") or 0)
    seconds_remaining = float(cached.get("seconds_remaining") or 0)

    if seconds_elapsed < PAPER_ENTRY_START_SEC or seconds_elapsed > PAPER_ENTRY_END_SEC:
        return False
    if seconds_remaining < PAPER_MIN_SECONDS_REMAINING:
        return False
    if direction == "DOWN" and seconds_elapsed > PAPER_DOWN_ENTRY_END_SEC:
        return False

    import json as _json
    judges_list = _json.loads(cached.get("judges_json") or "[]")
    confidence = float(cached.get("avg_confidence") or 0)

    signal = {
        "direction": direction,
        "actionable": True,
        "avg_confidence": confidence,
        "judges": judges_list,
    }
    window = {
        "window_start": window_start,
        "window_end": window_end,
        "slug": window_slug,
        "seconds_elapsed": seconds_elapsed,
        "up_token_id": None,
        "down_token_id": None,
    }
    win_row = fetch_one_dict(
        conn,
        "SELECT up_token_id, down_token_id FROM market_windows WHERE window_start = ?",
        (window_start,),
    )
    if win_row:
        window["up_token_id"] = win_row.get("up_token_id")
        window["down_token_id"] = win_row.get("down_token_id")

    market = {
        "btc_price": float(cached.get("btc_price") or 0),
        "btc_start_price": float(cached.get("start_price") or 0),
        "up_ask": float(cached.get("up_ask") or 0) if cached.get("up_ask") else None,
        "down_ask": float(cached.get("down_ask") or 0) if cached.get("down_ask") else None,
    }

    tick_samples, odds_samples = _window_sample_counts(conn, int(window_start), now_ts)
    if tick_samples < PAPER_MIN_TICK_SAMPLES or odds_samples < PAPER_MIN_ODDS_SAMPLES:
        logger.info(
            "Skip low sample window ws=%s: ticks=%s/%s odds=%s/%s elapsed=%.1fs",
            window_start,
            tick_samples,
            PAPER_MIN_TICK_SAMPLES,
            odds_samples,
            PAPER_MIN_ODDS_SAMPLES,
            seconds_elapsed,
        )
        return False

    exists = fetch_one(
        conn,
        "SELECT id FROM paper_trades WHERE window_start = ? AND archived_at IS NULL LIMIT 1",
        (int(window_start),),
    )
    if exists:
        return False

    # Ask prices come from signal_cache (data_collector's CLOB snapshot).
    # No separate CLOB fetch needed -- saves 2 API calls per tick.
    up_ask_val = _safe_prob(market.get("up_ask"))
    down_ask_val = _safe_prob(market.get("down_ask"))

    entry_price = up_ask_val if direction == "UP" else down_ask_val
    if entry_price is None:
        # Last fallback to raw field conversion.
        raw_entry = market.get("up_ask") if direction == "UP" else market.get("down_ask")
        try:
            entry_price = float(raw_entry) if raw_entry is not None else None
        except Exception:
            entry_price = None
    if entry_price is None:
        logger.warning("No %s ask price available; skipping trade", direction)
        return False
    entry_price = float(entry_price)
    if entry_price <= 0.0 or entry_price >= 1.0:
        logger.warning("Invalid entry ask %.6f for %s; skipping trade", entry_price, direction)
        return False

    # DOWN-specific min entry price (cheap DOWN tokens are traps)
    if direction == "DOWN" and entry_price < PAPER_DOWN_MIN_ENTRY_PRICE:
        logger.debug("Skip cheap DOWN ws=%s: price=%.3f < %.3f", window_start, entry_price, PAPER_DOWN_MIN_ENTRY_PRICE)
        return False

    # Spread filter: only enter when market is uncertain (UP/DOWN asks close)
    _max_spread = float(os.getenv("PAPER_MAX_ODDS_SPREAD", "0.12"))
    if up_ask_val is not None and down_ask_val is not None:
        _spread = abs(float(up_ask_val) - float(down_ask_val))
        if _spread > _max_spread:
            logger.debug("Skip wide spread ws=%s: spread=%.3f > %.3f", window_start, _spread, _max_spread)
            return False

    side_implied = up_ask_val if direction == "UP" else down_ask_val
    opposite_implied = down_ask_val if direction == "UP" else up_ask_val
    # All market guards (implied-side, divergence, momentum, trend, price range)
    # are checked by data_collector -> signal_cache.guards_passed.
    # Paper does NOT re-check -- ensures backtest = paper = live parity.

    btc_now = market.get("btc_price")
    btc_start = market.get("btc_start_price")
    sec_elapsed = window.get("seconds_elapsed")
    judges = signal.get("judges") or []
    if btc_now is None or btc_start is None or sec_elapsed is None:
        logger.warning("Missing market context for entry gate; skipping trade")
        return False
    support_votes = sum(1 for j in judges if str(j.get("vote")) == direction)
    support_ratio = (support_votes / float(len(judges))) if judges else 0.0
    confidence = float(signal.get("avg_confidence") or 0.0)
    # Divergence/momentum/trend guards are checked by data_collector and
    # stored in signal_cache.guards_passed. Read result instead of re-checking
    # (eliminates timing divergence between Paper and Live).
    btc_move_from_start_pct = float(cached.get("btc_move_pct") or 0) if cached else (
        ((float(btc_now) - float(btc_start)) / float(btc_start)) * 100.0
    )
    if cached and not int(cached.get("guards_passed") or 0):
        logger.debug("Skip guards_passed=0 ws=%s dir=%s", window_start, direction)
        return False
    # Trend/macro guards handled by signal_cache.guards_passed (above)

    # Entry gate checked by data_collector -> signal_cache.gate_allow
    # Paper reads result instead of re-running (same price = same result)
    if cached:
        _gate_allow = int(cached.get("gate_allow") or 0)
        _gate_reason = str(cached.get("gate_reason") or "")
        _gate_ev = float(cached.get("gate_ev") or 0)
        if not _gate_allow:
            logger.warning("Entry gate blocked ws=%s dir=%s: %s", window_start, direction, _gate_reason)
            return False
    else:
        # No signal_cache = no trade (was fallback to local gate, caused Paper/Live mismatch)
        return False
        # Dead code below kept for reference
        recent_ts, recent_prices = _recent_price_series(conn, now_ts, lookback_sec=600.0)
        gate = evaluate_entry_gate(
            direction=direction,
            entry_price=float(entry_price),
            current_price=float(btc_now),
            start_price=float(btc_start),
            seconds_elapsed=float(sec_elapsed),
            jury_confidence=confidence,
            support_ratio=float(support_ratio),
            seconds_remaining=float(seconds_remaining),
            recent_prices=recent_prices,
            recent_timestamps=recent_ts,
            poly_up_ask=float(up_ask_val) if up_ask_val is not None else None,
            poly_down_ask=float(down_ask_val) if down_ask_val is not None else None,
        )
        if not gate.allow:
            logger.warning("Entry gate blocked ws=%s dir=%s: %s", window_start, direction, gate.reason)
            return False
        _gate_ev = gate.expected_roi
    # When reading from signal_cache, gate checks are already done by data_collector.
    # Skip all remaining gate-dependent checks (lag edge, contra gap, adaptive EV)
    # to avoid re-running with potentially different prices.
    if cached and _gate_allow:
        # Use signal_cache gate_ev for adaptive checks below
        class _GateProxy:
            allow = True
            expected_roi = _gate_ev or 0.0
            model_prob = 0.5
            reason = _gate_reason or ""
            win_prob = 0.6
            prob_floor = 0.5
            fee_rate = 0.01
            is_coinflip = False
            fair_up = 0.5
            dispersion = 0.0
            alignment = 0.0
            penalty = 0.0
            spread_cost = 0.0
            net_ev_before_penalty = _gate_ev or 0.0
            regime_pass = True
            regime_details = ""
            skip_reason = ""
        gate = _GateProxy()
        # Skip lag edge and contra gap -- already passed in data_collector
    else:
        pass  # fallback path uses real gate object

    # Lag probability edge: our model_prob must beat normalized market prob
    if not (cached and _gate_allow) and side_implied is not None and opposite_implied is not None:
        market_total = float(side_implied) + float(opposite_implied)
        if market_total > 0:
            market_dir_prob = float(side_implied) / market_total
            lag_prob_edge = float(gate.model_prob) - market_dir_prob
            if lag_prob_edge < PAPER_MIN_LAG_PROB_EDGE:
                logger.warning(
                    "Skip lag-prob-edge ws=%s dir=%s: model=%.3f market=%.3f edge=%.3f < %.3f",
                    window_start, direction, float(gate.model_prob),
                    market_dir_prob, lag_prob_edge, PAPER_MIN_LAG_PROB_EDGE,
                )
                return False

    contra_gap = None
    if not (cached and _gate_allow) and side_implied is not None and opposite_implied is not None:
        contra_gap = float(opposite_implied) - float(side_implied)
    if contra_gap is not None and contra_gap > PAPER_MAX_CONTRA_GAP:
        strong_override = (
            float(gate.model_prob) >= PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB
            and float(confidence) >= PAPER_CONTRA_OVERRIDE_MIN_CONF
        )
        if not strong_override:
            logger.warning(
                "Skip contra-dominant ws=%s dir=%s: opp-side gap=+%.3f > %.3f (p=%.3f conf=%.3f, need p>=%.3f & conf>=%.3f)",
                window_start,
                direction,
                contra_gap,
                PAPER_MAX_CONTRA_GAP,
                float(gate.model_prob),
                float(confidence),
                PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB,
                PAPER_CONTRA_OVERRIDE_MIN_CONF,
            )
            return False
    loss_streak, recent_loss_rate = _recent_risk_state(conn)
    perf_count, perf_wr, perf_pnl = _recent_performance(conn, PAPER_RECENT_PERF_WINDOW)
    realized_equity, available_equity = _equity_snapshot(conn, initial_capital)
    drawdown_pct = _equity_drawdown_pct(conn, initial_capital)

    last_opened_at = _last_opened_at(conn)
    stale_relax = _stale_relax_factor(last_opened_at=last_opened_at, now_ts=now_ts)
    parity_thresholds = compute_parity_thresholds(
        ParityAdaptiveConfig(
            min_expected_roi=float(PAPER_MIN_EXPECTED_ROI),
            min_support_ratio=float(PAPER_MIN_SUPPORT_RATIO),
            min_confidence=float(PAPER_MIN_CONFIDENCE),
            max_entry_price=float(PAPER_MAX_ENTRY_PRICE),
            adaptive_max_ask_floor=float(PAPER_ADAPTIVE_MAX_ASK_FLOOR),
            require_unanimous=bool(PAPER_REQUIRE_UNANIMOUS),
            strictness_unanimous_at=float(PAPER_STRICTNESS_UNANIMOUS_AT),
            base_trade_gap_sec=float(PAPER_BASE_TRADE_GAP_SEC),
            target_trade_gap_sec=float(PAPER_TARGET_TRADE_GAP_SEC),
            stale_relax_max=float(PAPER_STALE_RELAX_MAX),
            profit_mode=str(PAPER_PROFIT_MODE),
            aggressive_entry_relax=float(PAPER_AGGRESSIVE_ENTRY_RELAX),
            aggressive_gap_mult=float(PAPER_AGGRESSIVE_GAP_MULT),
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

    # When signal_cache gate_allow=1, data_collector already validated all entry
    # conditions. Skip ALL adaptive guards to ensure Paper=Live parity.
    if not (cached and _gate_allow):
        if direction == "DOWN" and btc_move_from_start_pct > 0.0:
            if btc_move_from_start_pct >= PAPER_DOWN_ABOVE_START_BLOCK_PCT:
                logger.warning(
                    "Skip DOWN-above-start hard block ws=%s: btc_vs_start=%+.4f%% >= %.4f%%",
                    window_start,
                    btc_move_from_start_pct,
                    PAPER_DOWN_ABOVE_START_BLOCK_PCT,
                )
                return False
            ratio = btc_move_from_start_pct / max(PAPER_DOWN_ABOVE_START_BLOCK_PCT, 1e-9)
            ev_penalty = PAPER_DOWN_ABOVE_START_EV_PENALTY * _clamp(ratio, 0.0, 1.0)
            adaptive_min_ev += ev_penalty

        since_last = (now_ts - last_opened_at) if last_opened_at > 0 else 999999.0
        high_quality_override = (
            gate.expected_roi >= PAPER_HIGH_QUALITY_EV
            and confidence >= PAPER_HIGH_QUALITY_CONF
            and support_ratio >= max(adaptive_min_support, 0.80)
        )
        if since_last < dynamic_gap and not high_quality_override:
            return False

        if gate.expected_roi < adaptive_min_ev:
            logger.warning(
                "Skip weak EV ws=%s dir=%s: net_ev=%+.3f%% < %.3f%% (loss_streak=%s, recent_loss_rate=%.0f%%)",
                window_start, direction,
                gate.expected_roi * 100.0, adaptive_min_ev * 100.0,
                loss_streak, recent_loss_rate * 100.0,
            )
            return False
        if support_ratio < adaptive_min_support:
            logger.warning(
                "Skip weak jury ws=%s dir=%s: support=%.1f%% < %.1f%%",
                window_start, direction,
                support_ratio * 100.0, adaptive_min_support * 100.0,
            )
            return False
        require_unanimous = bool(parity_thresholds.require_unanimous)
        if require_unanimous and support_ratio < 1.0:
            logger.warning(
                "Skip non-unanimous ws=%s dir=%s: strictness=%.2f(->%.2f) support=%.1f%%",
                window_start, direction, strictness, strictness_eff, support_ratio * 100.0,
            )
            return False
        if confidence < adaptive_min_conf:
            logger.warning(
                "Skip low confidence ws=%s dir=%s: conf=%.3f < %.3f",
                window_start, direction, confidence, adaptive_min_conf,
            )
            return False
        if entry_price > adaptive_max_ask:
            logger.warning(
                "Skip expensive entry ws=%s dir=%s: ask=%.3f > %.3f",
                window_start, direction, entry_price, adaptive_max_ask,
            )
            return False

        # Underdog guard
        if entry_price < 0.40:
            underdog_min_conf = adaptive_min_conf + 0.10
            underdog_min_ev = adaptive_min_ev * 2.0
            if confidence < underdog_min_conf:
                logger.warning(
                    "Skip underdog entry ws=%s dir=%s: ask=%.3f conf=%.3f < %.3f (underdog)",
                    window_start, direction, entry_price, confidence, underdog_min_conf,
                )
                return False
            if gate.expected_roi < underdog_min_ev:
                logger.warning(
                    "Skip underdog EV ws=%s dir=%s: ask=%.3f ev=%.3f%% < %.3f%% (underdog)",
                    window_start, direction, entry_price,
                    gate.expected_roi * 100, underdog_min_ev * 100,
                )
                return False

    if realized_equity <= (initial_capital * (1.0 - PAPER_MAX_DRAWDOWN_STOP_PCT)):
        logger.warning(
            "Risk stop active: equity=$%.2f below stop level $%.2f",
            realized_equity,
            initial_capital * (1.0 - PAPER_MAX_DRAWDOWN_STOP_PCT),
        )
        return False

    if perf_count >= max(4, PAPER_RECENT_PERF_WINDOW) and perf_wr < PAPER_MIN_RECENT_WIN_RATE and perf_pnl < 0.0:
        last_closed_at = _last_closed_at(conn)
        since_closed = (now_ts - last_closed_at) if last_closed_at > 0 else 999999.0
        if since_closed < PAPER_PERF_PAUSE_SEC:
            logger.warning(
                "Pause by weak recent performance: trades=%s wr=%.1f%% pnl=$%+.2f cooldown_left=%.0fs",
                perf_count,
                perf_wr * 100.0,
                perf_pnl,
                PAPER_PERF_PAUSE_SEC - since_closed,
            )
            return False

    if sizing_mode == "fixed":
        _fixed_val = float(os.getenv("PAPER_FIXED_STAKE", "0"))
        if _fixed_val > 0:
            stake = round(_fixed_val, 2)
        else:
            stake = round(initial_capital * 0.15, 2)
        # Score-based sizing: 7 signals scored, 3x when score>=5 or prev momentum
        _mega_mult = float(os.getenv("PAPER_MEGA_MULTIPLIER", "3.0"))
        _min_score = int(os.getenv("PAPER_MIN_ENTRY_SCORE", "3"))
        if _mega_mult > 1.0 and cached:
            _btc_move_abs = abs(float(cached.get("btc_move_pct") or 0))
            _confidence = float(cached.get("avg_confidence") or 0)
            _ev = float(cached.get("gate_ev") or 0)
            # Read pre-computed score signals from signal_cache (no DB queries!)
            _prev_outcome = str(cached.get("prev_outcome") or "")
            if _prev_outcome not in ("UP", "DOWN"): _prev_outcome = None
            _ov = float(cached.get("odds_velocity") or 0)
            _accel_ok = bool(int(cached.get("btc_accel_ok") or 0)) if cached.get("btc_accel_ok") is not None else False
            # Calculate score
            _score = 0
            if _btc_move_abs >= 0.02: _score += 1
            if _prev_outcome == direction: _score += 1
            if entry_price <= 0.45: _score += 1
            if _ev >= 0.20: _score += 1
            if _confidence >= 0.7: _score += 1
            if _ov >= 0.02: _score += 1
            if _accel_ok: _score += 1
            # Skip if score too low
            if _score < _min_score:
                logger.info("Skip low score ws=%s: score=%d < %d", window_start, _score, _min_score)
                return False
            # 3x when score>=6 OR prev momentum
            _mega_score = int(os.getenv("PAPER_MEGA_MIN_SCORE", "6"))
            _is_mega = (_score >= _mega_score) or (_prev_outcome == direction and _btc_move_abs >= 0.02)
            if _is_mega:
                stake = round(stake * _mega_mult, 2)
                logger.info("MEGA bet: score=%d prev=%s btc=%.3f%% -> %dx $%.2f",
                           _score, _prev_outcome, _btc_move_abs, int(_mega_mult), stake)
            else:
                logger.info("Normal bet: score=%d $%.2f", _score, stake)
    elif sizing_mode == "all_in_fixed":
        stake = round(initial_capital, 2)
    elif sizing_mode == "all_in_equity":
        stake = round(max(0.0, available_equity), 2)
    else:
        if available_equity < PAPER_MIN_BET:
            logger.warning(
                "Insufficient available equity ws=%s: available=$%.2f",
                window_start,
                available_equity,
            )
            return False

        # Compute max_edge from judges (matched to backtest's decision.max_edge)
        judges_list = signal.get("judges") or []
        _max_edge = max(
            (float(j.get("confidence") or 0.0) for j in judges_list),
            default=float(confidence),
        )
        stake = _compute_bet_size(
            available_equity=available_equity,
            initial_capital=initial_capital,
            expected_roi=gate.expected_roi,
            risk_fraction=risk_fraction,
            entry_price=entry_price,
            model_prob=gate.model_prob,
            confidence=confidence,
            max_edge=_max_edge,
            seconds_elapsed=float(sec_elapsed) if sec_elapsed is not None else None,
        )
        if loss_streak > 0:
            # Adaptive de-risking after losses.
            loss_deboost = 0.65
            if PAPER_PROFIT_MODE == "aggressive":
                loss_deboost = _clamp(PAPER_AGGRESSIVE_LOSS_DEBOOST, 0.70, 0.95)
            stake = round(stake * (loss_deboost ** min(loss_streak, 3)), 2)
    if stake < PAPER_MIN_BET:
        logger.warning("Computed bet too small ws=%s: $%.2f", window_start, stake)
        return False

    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
    opened_at = time.time()
    conf = confidence
    reason = str(signal.get("reason") or "")
    if reason:
        reason = f"{reason} | {gate.reason}"
    else:
        reason = gate.reason

    execute_write(
        conn,
        """INSERT INTO paper_trades
           (window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
            potential_win_pnl, signal_confidence, signal_reason, initial_capital, risk_fraction, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
        (
            int(window_start),
            int(window_end),
            direction,
            float(stake),
            entry_price,
            payout_multiple,
            shares,
            potential_win_pnl,
            conf,
            reason,
            float(initial_capital),
            float(risk_fraction),
            opened_at,
        ),
    )
    conn.commit()

    logger.warning(
        "OPEN  ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% eq=$%.2f avail=$%.2f strict=%.2f->%.2f stale=%.2f gap=%.0fs mode=%s",
        window_start,
        direction,
        stake,
        entry_price,
        gate.expected_roi * 100.0,
        realized_equity,
        available_equity,
        strictness,
        strictness_eff,
        stale_relax,
        dynamic_gap,
        sizing_mode,
    )
    _send_paper_open_telegram(
        window_slug=window_slug,
        window_start=int(window_start),
        direction=direction,
        stake=float(stake),
        entry_price=float(entry_price),
        up_ask=float(up_ask_val) if up_ask_val is not None else None,
        down_ask=float(down_ask_val) if down_ask_val is not None else None,
        btc_start=float(btc_start) if btc_start is not None else None,
        btc_now=float(btc_now) if btc_now is not None else None,
        confidence=float(conf),
        reason=str(reason),
    )
    return True


def resolve_open_trades(conn) -> int:
    _backfill_unresolved_windows(conn)
    now_ts = time.time()
    exit_cfg = _paper_exit_policy_config()

    open_rows = fetch_all_dicts(
        conn,
        """SELECT id, window_start, window_end, direction, stake, shares, entry_price, opened_at, signal_confidence
           FROM paper_trades
           WHERE status = 'OPEN' AND archived_at IS NULL
           ORDER BY window_start ASC""",
    )
    if not open_rows:
        return 0

    resolved = 0
    for row in open_rows:
        trade_id = int(row["id"])
        ws = int(row["window_start"])
        outcome_row = fetch_one(
            conn,
            "SELECT actual_outcome FROM market_windows WHERE window_start = ?",
            (ws,),
        )
        direction = str(row["direction"])
        stake = float(row["stake"])
        shares = float(row["shares"])
        entry_price = float(row.get("entry_price") or 0.5)
        opened_at = float(row.get("opened_at") or 0.0)
        signal_confidence = float(row.get("signal_confidence") or 0.0)
        window_end = float(row.get("window_end") or 0.0)
        window_row = fetch_one_dict(
            conn,
            """SELECT slug, btc_start_price, btc_end_price, actual_outcome
               FROM market_windows
               WHERE window_start = ?
               LIMIT 1""",
            (ws,),
        )

        # 1) Expiry settlement (binary resolution)
        outcome = outcome_row[0] if outcome_row else None
        if outcome in ("UP", "DOWN"):
            won = 1 if outcome == direction else 0
            if won:
                raw_pnl = shares - stake
                pnl = apply_fee_to_pnl(raw_pnl, stake)
            else:
                # Binary option loss: lose the stake, no additional fee
                pnl = -stake
            roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
            closed_at = now_ts

            execute_write(
                conn,
                """UPDATE paper_trades
                   SET status='CLOSED',
                       closed_at=?,
                       actual_outcome=?,
                       won=?,
                       pnl=?,
                       roi_pct=?,
                       close_reason='expiry_settlement'
                   WHERE id=?""",
                (closed_at, outcome, won, pnl, roi_pct, trade_id),
            )
            resolved += 1
            _EARLY_EXIT_OPPOSITE_HITS.pop(trade_id, None)

            if pnl > 0:
                btc_exit_px = _price_at_or_near(conn, now_ts, prefer_before=True)
                logger.warning(
                    "PROFIT ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%% btc_exit=$%.2f",
                    ws,
                    direction,
                    outcome,
                    pnl,
                    roi_pct,
                    float(btc_exit_px) if btc_exit_px else 0.0,
                )
            elif pnl < 0:
                btc_exit_px = _price_at_or_near(conn, now_ts, prefer_before=True)
                logger.warning(
                    "LOSS   ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%% btc_exit=$%.2f",
                    ws,
                    direction,
                    outcome,
                    pnl,
                    roi_pct,
                    float(btc_exit_px) if btc_exit_px else 0.0,
                )
            else:
                btc_exit_px = _price_at_or_near(conn, now_ts, prefer_before=True)
                logger.warning(
                    "FLAT   ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%% btc_exit=$%.2f",
                    ws,
                    direction,
                    outcome,
                    pnl,
                    roi_pct,
                    float(btc_exit_px) if btc_exit_px else 0.0,
                )
            odds_close_row = _latest_odds_for_window(conn, ws)
            up_ask_close = (
                (_safe_prob(odds_close_row.get("up_best_ask")) or _safe_prob(odds_close_row.get("up_mid")))
                if odds_close_row
                else None
            )
            down_ask_close = (
                (_safe_prob(odds_close_row.get("down_best_ask")) or _safe_prob(odds_close_row.get("down_mid")))
                if odds_close_row
                else None
            )
            settle_exit_price = 1.0 if int(won) == 1 else 0.0
            _send_paper_close_telegram(
                close_type="expiry_settlement",
                window_slug=str((window_row or {}).get("slug") or f"window-{ws}"),
                window_start=int(ws),
                direction=str(direction),
                stake=float(stake),
                entry_price=float(entry_price),
                btc_start=float((window_row or {}).get("btc_start_price")) if (window_row and window_row.get("btc_start_price") is not None) else None,
                btc_end=float((window_row or {}).get("btc_end_price")) if (window_row and window_row.get("btc_end_price") is not None) else None,
                btc_exit=float(btc_exit_px) if btc_exit_px is not None else None,
                outcome=str(outcome),
                up_ask=float(up_ask_close) if up_ask_close is not None else None,
                down_ask=float(down_ask_close) if down_ask_close is not None else None,
                exit_price=float(settle_exit_price),
                pnl=float(pnl),
                roi_pct=float(roi_pct),
                reason="expiry_settlement",
            )
            continue

        # 2) Early exit checks for open markets
        if not exit_cfg.enabled:
            continue
        if opened_at <= 0.0:
            continue
        hold_sec = now_ts - opened_at
        if hold_sec < exit_cfg.min_elapsed_sec:
            continue

        odds_row = _latest_odds_for_window(conn, ws)
        if not odds_row:
            continue

        # Use stored poly_odds for exit decisions (same as backtest).
        # CLOB live overlay was causing divergence: thin CLOB books show
        # extreme bid/ask (0.05/0.95) that trigger false flush exits.
        # Paper must match backtest for parity.

        _exit_px, _value, mtm_pnl, mtm_roi_pct = _mark_to_market(
            direction=direction,
            stake=stake,
            shares=shares,
            odds_row=odds_row,
        )
        up_ask = _safe_prob(odds_row.get("up_best_ask")) or _safe_prob(odds_row.get("up_mid"))
        down_ask = _safe_prob(odds_row.get("down_best_ask")) or _safe_prob(odds_row.get("down_mid"))
        opposite_ask = up_ask if direction == "DOWN" else down_ask
        remaining_sec = max(0.0, window_end - now_ts) if window_end > 0 else 0.0

        btc_move_from_entry_pct = None
        btc_adverse_ok = True
        btc_now_px = _price_at_or_near(conn, now_ts, prefer_before=True)
        if exit_cfg.stop_loss_require_btc_adverse:
            btc_entry_px = _price_at_or_near(conn, opened_at, prefer_before=True) if opened_at > 0 else None
            if btc_entry_px is not None and btc_entry_px > 0 and btc_now_px is not None and btc_now_px > 0:
                btc_move_from_entry_pct = ((float(btc_now_px) - float(btc_entry_px)) / float(btc_entry_px)) * 100.0
                adverse_thr = abs(float(exit_cfg.stop_loss_btc_adverse_pct))
                if direction == "UP":
                    btc_adverse_ok = btc_move_from_entry_pct <= -adverse_thr
                else:
                    btc_adverse_ok = btc_move_from_entry_pct >= adverse_thr
            else:
                btc_adverse_ok = False

        recent_ts, recent_prices = _recent_price_series(conn, now_ts, lookback_sec=180.0)
        current_btc_px = float(recent_prices[-1]) if recent_prices else float(btc_now_px or 0.0)
        start_btc_px = (
            float(window_row.get("btc_start_price"))
            if (window_row and window_row.get("btc_start_price") is not None)
            else float(current_btc_px or 0.0)
        )
        peak = max(float(_PEAK_ROI_PER_TRADE.get(trade_id, -999.0)), float(mtm_roi_pct))
        _PEAK_ROI_PER_TRADE[trade_id] = peak

        exit_decision = evaluate_exit_policy(
            ExitPolicyInput(
                direction=direction,
                hold_sec=float(hold_sec),
                seconds_elapsed=max(1.0, float(now_ts - ws)),
                seconds_remaining=float(remaining_sec),
                signal_confidence=float(signal_confidence),
                mtm_roi_pct=float(mtm_roi_pct),
                current_price=float(current_btc_px),
                start_price=float(start_btc_px),
                peak_roi_pct=float(peak),
                opposite_ask=float(opposite_ask) if opposite_ask is not None else None,
                recent_prices=list(recent_prices),
                recent_timestamps=list(recent_ts),
                btc_adverse_ok=bool(btc_adverse_ok),
                btc_move_from_entry_pct=(
                    float(btc_move_from_entry_pct)
                    if btc_move_from_entry_pct is not None
                    else None
                ),
                opposite_hits=int(_EARLY_EXIT_OPPOSITE_HITS.get(trade_id, 0)),
            ),
            exit_cfg,
        )
        if exit_decision.opposite_hits > 0:
            _EARLY_EXIT_OPPOSITE_HITS[trade_id] = int(exit_decision.opposite_hits)
        else:
            _EARLY_EXIT_OPPOSITE_HITS.pop(trade_id, None)
        early_reason = exit_decision.reason

        # --- Smart exit: jury re-evaluation mid-trade ---
        if (
            early_reason is None
            and bool(config.trading.smart_exit_enabled)
            and hold_sec >= float(config.trading.smart_exit_min_hold_sec)
            and float(mtm_roi_pct) >= float(config.trading.smart_exit_min_roi_pct)
        ):
            _se_interval = float(config.trading.smart_exit_interval_sec)
            _se_last = _SMART_EXIT_LAST_CHECK.get(trade_id, 0.0)
            if (now_ts - _se_last) >= _se_interval:
                _SMART_EXIT_LAST_CHECK[trade_id] = now_ts
                try:
                    _se_jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))
                    _se_prices, _se_ts = _recent_price_series(conn, now_ts, lookback_sec=600.0)
                    if len(_se_prices) >= 20 and start_btc_px > 0:
                        _se_up_ask = float(up_ask) if up_ask else 0.5
                        _se_dn_ask = float(down_ask) if down_ask else 0.5
                        _se_ctx = MarketContext(
                            current_binance_price=float(current_btc_px),
                            market_start_price=float(start_btc_px),
                            recent_prices=list(_se_prices[-600:]),
                            recent_timestamps=list(_se_ts[-600:]),
                            poly_up_price=_se_up_ask,
                            poly_down_price=_se_dn_ask,
                            seconds_elapsed=float(now_ts - ws),
                            seconds_remaining=float(remaining_sec),
                            poly_up_ask=_se_up_ask,
                            poly_down_ask=_se_dn_ask,
                        )
                        _se_decision = _se_jury.deliberate(_se_ctx)
                        if _se_decision.direction != direction and _se_decision.direction != "NO_TRADE":
                            early_reason = (
                                f"smart_exit_jury_flip(flip={_se_decision.direction}"
                                f", hold={hold_sec:.0f}s, roi={mtm_roi_pct:+.1f}%"
                                f", opp_ask={float(opposite_ask or 0):.3f})"
                            )
                        elif _se_decision.direction == "NO_TRADE" and float(mtm_roi_pct) < -30.0:
                            early_reason = (
                                f"smart_exit_no_trade(hold={hold_sec:.0f}s"
                                f", roi={mtm_roi_pct:+.1f}%"
                                f", opp_ask={float(opposite_ask or 0):.3f})"
                            )
                except Exception as _se_err:
                    logger.debug("smart_exit jury error: %s", _se_err)

        if early_reason:
            closed = _close_trade_early(
                conn,
                trade_id=trade_id,
                window_start=ws,
                direction=direction,
                stake=stake,
                shares=shares,
                entry_price=entry_price,
                reason=early_reason,
                odds_row=odds_row,
            )
            if closed:
                resolved += 1
                _EARLY_EXIT_OPPOSITE_HITS.pop(trade_id, None)
                _PEAK_ROI_PER_TRADE.pop(trade_id, None)
                _SMART_EXIT_LAST_CHECK.pop(trade_id, None)

    if resolved:
        conn.commit()
    return resolved


def show_status(conn):
    try:
        cap_row = fetch_one(
            conn,
            "SELECT initial_capital FROM paper_trades WHERE initial_capital IS NOT NULL AND archived_at IS NULL ORDER BY window_start ASC LIMIT 1",
        )
    except Exception:
        cap_row = None
    if cap_row and cap_row[0] is not None:
        initial_capital = float(cap_row[0])
    else:
        initial_capital = 1000.0

    stats = fetch_one(
        conn,
        """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_cnt,
               SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_cnt,
               SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END) AS losses,
               COALESCE(SUM(pnl), 0) AS total_pnl
           FROM paper_trades
           WHERE archived_at IS NULL""",
    )

    total = int(stats[0] or 0)
    open_cnt = int(stats[1] or 0)
    closed_cnt = int(stats[2] or 0)
    wins = int(stats[3] or 0)
    losses = int(stats[4] or 0)
    total_pnl = float(stats[5] or 0.0)
    win_rate = (wins / closed_cnt * 100.0) if closed_cnt > 0 else 0.0
    equity = initial_capital + total_pnl

    print(f"""
{'='*64}
 PAPER TRADE STATUS
{'='*64}
 DB:          {db_label()}
 Seed Cap:    ${initial_capital:.2f}
 Equity:      ${equity:+.2f}
 Total:       {total}
 Open:        {open_cnt}
 Closed:      {closed_cnt}
 Wins/Losses: {wins}/{losses}  (WR={win_rate:.1f}%)
 Total PnL:   ${total_pnl:+.2f}
{'='*64}
""")

    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, direction, stake, entry_price, payout_multiple,
                  potential_win_pnl, status, actual_outcome, pnl, roi_pct
           FROM paper_trades
           WHERE archived_at IS NULL
           ORDER BY window_start DESC
           LIMIT 12""",
    )
    if rows:
        print("Recent trades:")
        for r in rows:
            ws = int(r["window_start"])
            dt = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            status = str(r["status"])
            pnl = r["pnl"]
            pnl_s = f"${float(pnl):+.2f}" if pnl is not None else "-"
            roi = r["roi_pct"]
            roi_s = f"{float(roi):+.2f}%" if roi is not None else "-"
            print(
                f"  {dt} | {status:6s} | {r['direction']:4s} | "
                f"ask={float(r['entry_price']):.4f} payx={float(r['payout_multiple']):.3f} | "
                f"potWin={float(r['potential_win_pnl']):+.2f} | pnl={pnl_s} roi={roi_s}"
            )


def run_loop(stake: float, interval_sec: float, sizing_mode: str):
    conn = connect_db()
    init_paper_table(conn)

    initial_capital = max(50.0, float(stake))
    risk_fraction = _clamp(PAPER_RISK_FRACTION, 0.01, 1.0)
    mode = str(sizing_mode or "adaptive").strip().lower()
    if mode not in ("adaptive", "fixed", "all_in_fixed", "all_in_equity"):
        mode = "adaptive"
    logger.warning(
        "Paper simulator running: initial=$%.2f mode=%s risk_frac=%.2f base_ev=%.2f%% min_support=%.0f%% max_ask=%.2f "
        "entry=%.0f~%.0fs remain>=%.0fs samples(t/o)=%d/%d gap=%.0f~%.0fs unanim=%s(at>=%.2f) "
        "stale_relax(start=%.0fs full=%.0fs max=%.0f%% ask_floor=%.2f) perf_pause=%.0fs "
        "align(side>=%.2f,opp<=%.2f) contra_gap<=%.3f(ovr p>=%.2f conf>=%.2f) "
        "trend_align(lookback=%.0fs opp<=%.4f%%) macro_trend(lookback=%.0fs block>=%.4f%%) "
        "down_guard(block>=%.4f%%,+mom=%.4f%%,+ev=%.2f%%) "
        "profit_mode=%s(relax=%.0f%% gapx=%.2f maxBetFrac=%.2f kelly=%.2f deboost=%.2f) "
        "early_exit=%s(opp>=%.2f,sl<=%.1f%%@hold>=%.0fs highConf>=%.2f->%.0fs lowConf<=%.2f relax=%.1f%%,"
        " btcAdv=%s@%.3f%% maxHold=%.0fs ts<=%.1f%%)",
        initial_capital,
        mode,
        risk_fraction,
        PAPER_MIN_EXPECTED_ROI * 100.0,
        PAPER_MIN_SUPPORT_RATIO * 100.0,
        PAPER_MAX_ENTRY_PRICE,
        PAPER_ENTRY_START_SEC,
        PAPER_ENTRY_END_SEC,
        PAPER_MIN_SECONDS_REMAINING,
        PAPER_MIN_TICK_SAMPLES,
        PAPER_MIN_ODDS_SAMPLES,
        PAPER_BASE_TRADE_GAP_SEC,
        PAPER_TARGET_TRADE_GAP_SEC,
        PAPER_REQUIRE_UNANIMOUS,
        PAPER_STRICTNESS_UNANIMOUS_AT,
        PAPER_STALE_RELAX_START_SEC,
        PAPER_STALE_RELAX_FULL_SEC,
        PAPER_STALE_RELAX_MAX * 100.0,
        PAPER_ADAPTIVE_MAX_ASK_FLOOR,
        PAPER_PERF_PAUSE_SEC,
        PAPER_MIN_ENTRY_SIDE_IMPLIED,
        PAPER_MAX_OPPOSITE_IMPLIED,
        PAPER_MAX_CONTRA_GAP,
        PAPER_CONTRA_OVERRIDE_MIN_MODEL_PROB,
        PAPER_CONTRA_OVERRIDE_MIN_CONF,
        PAPER_TREND_ALIGN_LOOKBACK_SEC,
        PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT,
        PAPER_MACRO_TREND_LOOKBACK_SEC,
        PAPER_MACRO_TREND_BLOCK_PCT,
        PAPER_DOWN_ABOVE_START_BLOCK_PCT,
        PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA,
        PAPER_DOWN_ABOVE_START_EV_PENALTY * 100.0,
        PAPER_PROFIT_MODE,
        PAPER_AGGRESSIVE_ENTRY_RELAX * 100.0,
        PAPER_AGGRESSIVE_GAP_MULT,
        PAPER_AGGRESSIVE_MAX_BET_FRAC,
        PAPER_AGGRESSIVE_KELLY_FRAC,
        PAPER_AGGRESSIVE_LOSS_DEBOOST,
        PAPER_ENABLE_EARLY_EXIT,
        PAPER_EARLY_EXIT_OPPOSITE_ASK,
        PAPER_EARLY_EXIT_STOP_LOSS_ROI_PCT,
        PAPER_EARLY_EXIT_STOP_LOSS_MIN_HOLD_SEC,
        PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_CUTOFF,
        PAPER_EARLY_EXIT_STOP_LOSS_HIGH_CONF_MIN_HOLD_SEC,
        PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_CUTOFF,
        PAPER_EARLY_EXIT_STOP_LOSS_LOW_CONF_RELAX_PCT,
        PAPER_EARLY_EXIT_STOP_LOSS_REQUIRE_BTC_ADVERSE,
        PAPER_EARLY_EXIT_STOP_LOSS_BTC_ADVERSE_PCT,
        PAPER_EARLY_EXIT_MAX_HOLD_SEC,
        PAPER_EARLY_EXIT_TIMESTOP_MAX_ROI_PCT,
    )
    _tg_ready, _tg_token, _tg_chat = _paper_telegram_ready()
    logger.warning(
        "Paper Telegram: notify_open=%s configured=%s (token=%s chat=%s)",
        bool(getattr(config.trading, "paper_telegram_notify_open", False)),
        bool(_tg_ready),
        bool(_tg_token),
        bool(_tg_chat),
    )

    try:
        while True:
            try:
                if not _check_data_freshness(conn):
                    time.sleep(interval_sec)
                    continue
                resolve_open_trades(conn)
                open_trade_if_signal(
                    conn,
                    initial_capital=initial_capital,
                    risk_fraction=risk_fraction,
                    sizing_mode=mode,
                )
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.exception("Paper loop transient error (continue): %s", e)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("Paper simulator stopped by user")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Live paper-trading simulator")
    parser.add_argument("--stake", type=float, default=1000.0, help="Paper seed capital in USD (default: 1000)")
    parser.add_argument("--interval", type=float, default=0.1, help="Polling interval seconds (default: 0.1, max speed within rate limits)")
    parser.add_argument(
        "--sizing-mode",
        type=str,
        default=PAPER_SIZING_MODE,
        help="adaptive | fixed | all_in_fixed | all_in_equity",
    )
    parser.add_argument("--status", action="store_true", help="Show paper trade status")
    args = parser.parse_args()

    conn = connect_db()
    init_paper_table(conn)
    conn.close()

    if args.status:
        conn = connect_db()
        show_status(conn)
        conn.close()
        return

    run_loop(
        stake=float(args.stake),
        interval_sec=float(args.interval),
        sizing_mode=str(args.sizing_mode),
    )


if __name__ == "__main__":
    main()
