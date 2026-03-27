"""Shared market guard evaluation for data_collector, paper, and backtest."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from paper_trade_sim import _recent_move_pct


@dataclass
class GuardResult:
    passed: bool = False
    divergence_ok: bool = True
    momentum_ok: bool = True
    trend_ok: bool = True
    implied_side_ok: bool = True
    down_price_ok: bool = True
    max_price_ok: bool = True
    reason: str = ""


def evaluate_market_guards(
    direction: str,
    btc_price: float,
    start_price: float,
    up_ask: float | None,
    down_ask: float | None,
    elapsed: float,
    db_conn=None,
    window_start: int | float = 0,
    # Array-based mode (backtest): pass prices/timestamps directly
    prices: list[float] | None = None,
    timestamps: list[float] | None = None,
    # Threshold overrides (read from env if None)
    up_boundary_pct: float | None = None,
    down_boundary_pct: float | None = None,
    move_lookback_sec: float | None = None,
    min_move_pct: float | None = None,
    down_extra_momentum: float | None = None,
    trend_lookback_sec: float | None = None,
    trend_max_opposing_pct: float | None = None,
    min_side_implied: float | None = None,
    max_opposite_implied: float | None = None,
    down_min_entry_price: float | None = None,
    max_entry_price: float | None = None,
    now_ts: float | None = None,
) -> GuardResult:
    """Evaluate all market guards and return a GuardResult.

    Uses DB-based _recent_move_pct for momentum/trend (same as data_collector).
    """
    import time

    if now_ts is None:
        now_ts = time.time()

    result = GuardResult()

    if direction not in ("UP", "DOWN"):
        result.reason = "invalid_direction"
        return result

    if not start_price or start_price <= 0:
        result.reason = "no_start_price"
        return result

    # --- Read thresholds from env with defaults ---
    _up_boundary = up_boundary_pct if up_boundary_pct is not None else float(os.getenv("PAPER_MIN_BOUNDARY_DIST_PCT", "0.020"))
    _down_boundary = down_boundary_pct if down_boundary_pct is not None else float(os.getenv("PAPER_DOWN_MIN_BOUNDARY_DIST_PCT", "0.030"))
    _move_lookback = move_lookback_sec if move_lookback_sec is not None else float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
    _min_move = min_move_pct if min_move_pct is not None else float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.004"))
    _down_extra = down_extra_momentum if down_extra_momentum is not None else float(os.getenv("PAPER_DOWN_ABOVE_START_MOMENTUM_EXTRA", "0.006"))
    _trend_lookback = trend_lookback_sec if trend_lookback_sec is not None else float(os.getenv("PAPER_TREND_ALIGN_LOOKBACK_SEC", "75"))
    _trend_thr = trend_max_opposing_pct if trend_max_opposing_pct is not None else float(os.getenv("PAPER_TREND_ALIGN_MAX_OPPOSING_MOVE_PCT", "0.004"))
    _min_side = min_side_implied if min_side_implied is not None else float(os.getenv("PAPER_MIN_ENTRY_SIDE_IMPLIED", "0.22"))
    _max_opp = max_opposite_implied if max_opposite_implied is not None else float(os.getenv("PAPER_MAX_OPPOSITE_IMPLIED", "0.78"))
    _down_min_price = down_min_entry_price if down_min_entry_price is not None else float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.35"))
    _max_price = max_entry_price if max_entry_price is not None else float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.65"))

    btc_move_pct = ((btc_price - start_price) / start_price) * 100.0

    # --- Direction-against-trend hard block ---
    _down_block_pct = float(os.getenv("PAPER_DOWN_ABOVE_START_BLOCK_PCT", "0.050"))
    _up_block_pct = float(os.getenv("PAPER_UP_BELOW_START_BLOCK_PCT", "0.050"))
    if direction == "DOWN" and btc_move_pct > 0 and btc_move_pct >= _down_block_pct:
        result.reason = f"DOWN_above_start: btc_vs_start=+{btc_move_pct:.4f}% >= {_down_block_pct:.4f}%"
        return result
    if direction == "UP" and btc_move_pct < 0 and abs(btc_move_pct) >= _up_block_pct:
        result.reason = f"UP_below_start: btc_vs_start={btc_move_pct:+.4f}% <= -{_up_block_pct:.4f}%"
        return result

    # --- Opposite ask too high (market strongly disagrees with entry direction) ---
    _max_opposite = float(os.getenv("MAX_OPPOSITE_ASK", "0.65"))
    if direction == "UP" and down_ask is not None and float(down_ask) >= _max_opposite:
        result.reason = f"opposite_too_high: DOWN_ask={float(down_ask):.3f} >= {_max_opposite:.3f}"
        return result
    if direction == "DOWN" and up_ask is not None and float(up_ask) >= _max_opposite:
        result.reason = f"opposite_too_high: UP_ask={float(up_ask):.3f} >= {_max_opposite:.3f}"
        return result

    # --- Divergence (boundary distance) ---
    boundary = _down_boundary if direction == "DOWN" else _up_boundary
    result.divergence_ok = abs(btc_move_pct) >= boundary
    if not result.divergence_ok:
        result.reason = "divergence"

    # --- Strong start factor (relaxes momentum when already moving strongly) ---
    dir_start = btc_move_pct if direction == "UP" else -btc_move_pct
    factor = 1.0
    if dir_start >= 0.06:
        factor = max(0.0, 1.0 - (dir_start - 0.06) / 0.06)

    # --- Momentum (short-term, 20s default) ---
    if prices is not None and timestamps is not None:
        # Array mode (backtest)
        from backtest import _recent_move_pct as _array_move
        recent_move = _array_move(prices=prices, timestamps=timestamps, now_ts=now_ts, lookback_sec=_move_lookback)
    else:
        # DB mode (data_collector / paper / live)
        recent_move = _recent_move_pct(db_conn, window_start, now_ts, _move_lookback)
    if recent_move is not None:
        if direction == "UP" and recent_move < _min_move * factor:
            result.momentum_ok = False
            if not result.reason:
                result.reason = "momentum"

        down_thr = _min_move
        if direction == "DOWN" and btc_move_pct > 0:
            down_thr += _down_extra
        eff_down = down_thr * factor
        if direction == "DOWN" and recent_move > -eff_down:
            result.momentum_ok = False
            if not result.reason:
                result.reason = "momentum"

    # --- Trend alignment (medium-term, 75s default) ---
    if prices is not None and timestamps is not None:
        trend_move = _array_move(prices=prices, timestamps=timestamps, now_ts=now_ts, lookback_sec=_trend_lookback)
    else:
        trend_move = _recent_move_pct(db_conn, window_start, now_ts, _trend_lookback)
    eff_trend = _trend_thr / max(factor, 0.05)
    if trend_move is not None:
        if direction == "UP" and trend_move < -eff_trend:
            result.trend_ok = False
            if not result.reason:
                result.reason = "trend"
        if direction == "DOWN" and trend_move > eff_trend:
            result.trend_ok = False
            if not result.reason:
                result.reason = "trend"

    # --- Implied side (thin-book normalization) ---
    entry_price = None
    opposite_ask = None
    if up_ask is not None and down_ask is not None:
        entry_price = float(up_ask) if direction == "UP" else float(down_ask)
        opposite_ask = float(down_ask) if direction == "UP" else float(up_ask)

        # Use better of raw side_ask vs complement of opposite (thin CLOB fix)
        effective_side = max(entry_price, 1.0 - opposite_ask)
        if effective_side < _min_side:
            result.implied_side_ok = False
            if not result.reason:
                result.reason = "implied_side"

        # Opposite implied too high
        if opposite_ask > _max_opp:
            result.implied_side_ok = False
            if not result.reason:
                result.reason = "opposite_implied"

    # --- DOWN min entry price ---
    if direction == "DOWN" and entry_price is not None and entry_price < _down_min_price:
        result.down_price_ok = False
        if not result.reason:
            result.reason = "down_price"

    # --- Max entry price ---
    if entry_price is not None and entry_price > _max_price:
        result.max_price_ok = False
        if not result.reason:
            result.reason = "max_price"

    # --- Overall ---
    result.passed = (
        result.divergence_ok
        and result.momentum_ok
        and result.trend_ok
        and result.implied_side_ok
        and result.down_price_ok
        and result.max_price_ok
    )

    return result
