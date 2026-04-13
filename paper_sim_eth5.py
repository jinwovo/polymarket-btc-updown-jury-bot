"""
Paper trading simulator for ETH 5-minute Up/Down binary options.

Reads signals from signal_cache_eth5 (written by a signal generator),
opens virtual trades in paper_trades_eth5, and settles at window end
using eth_ticks prices.

Usage:
    python paper_sim_eth5.py
    python paper_sim_eth5.py --stake 100 --sizing-mode fixed
    python paper_sim_eth5.py --status
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from config import config
from market_config import ETH_5M, env
from db_config import (
    connect_db,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)
from trade_gate import apply_fee_to_pnl
from telegram_notifier import send_telegram_message

# ---------------------------------------------------------------------------
# Logging -- bot_paper_eth5.log (ASCII only)
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_root = logging.getLogger()
_root.setLevel(logging.INFO)

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)

_fh = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_paper_eth5.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)

logger = logging.getLogger("paper_sim_eth5")

# ---------------------------------------------------------------------------
# Market constants
# ---------------------------------------------------------------------------
MARKET = ETH_5M
INTERVAL = MARKET.interval_seconds          # 300
PRICE_TABLE = MARKET.price_table             # "eth_ticks"
SIGNAL_TABLE = MARKET.signal_cache_table     # "signal_cache_eth5"
TRADES_TABLE = MARKET.paper_trades_table     # "paper_trades_eth5"
ENV_PREFIX = MARKET.env_prefix               # "ETH5_"

# ---------------------------------------------------------------------------
# Entry parameters (ETH5_ prefix, fallback to PAPER_)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: str) -> float:
    return float(env(MARKET, name, default))


def _env_int(name: str, default: str) -> int:
    return int(env(MARKET, name, default))


ENTRY_START_SEC = _env_float("ENTRY_START_SEC", "80")
ENTRY_END_SEC = _env_float("ENTRY_END_SEC", "240")
DOWN_ENTRY_END_SEC = _env_float("DOWN_ENTRY_END_SEC", "200")
MIN_SECONDS_REMAINING = _env_float("MIN_SECONDS_REMAINING", "30")
MAX_ENTRY_PRICE = _env_float("MAX_ENTRY_PRICE", "0.58")
DOWN_MIN_ENTRY_PRICE = _env_float("DOWN_MIN_ENTRY_PRICE", "0.30")
MAX_ODDS_SPREAD = _env_float("MAX_ODDS_SPREAD", "0.20")
MAX_BTC_MOVE_PCT = _env_float("MAX_BTC_MOVE_PCT", "0.10")
MIN_ENTRY_SCORE = _env_int("MIN_ENTRY_SCORE", "3")
FIXED_STAKE_DEFAULT = _env_float("FIXED_STAKE", "100")
MIN_BET = _env_float("MIN_BET", "10")
OPPOSITE_MAX_ASK = _env_float("OPPOSITE_MAX_ASK", "0.78")

# Technical entry filters
REQUIRE_VWAP_AGREE = env(MARKET, "REQUIRE_VWAP_AGREE", "true").lower() == "true"
REQUIRE_BB_EXTREME = env(MARKET, "REQUIRE_BB_EXTREME", "false").lower() == "true"
BB_THRESHOLD = _env_float("BB_THRESHOLD", "0.5")
MAX_ASK_DRIFT = _env_float("MAX_ASK_DRIFT", "999")

# Novel filters (2026-04-12)
BTC_ACTIVE_MIN_RANGE = _env_float("BTC_ACTIVE_MIN_RANGE", "0.05")
BTC_ACTIVE_LOOKBACK = _env_float("BTC_ACTIVE_LOOKBACK", "300")
CONF_SIZING_ENABLED = env(MARKET, "CONF_SIZING_ENABLED", "true").lower() == "true"
CONF_SIZING_FULL_MULT = _env_float("CONF_SIZING_FULL_MULT", "3.0")
BTC_LEADS_SEC = _env_float("BTC_LEADS_SEC", "30")
BTC_LEADS_MIN_PCT = _env_float("BTC_LEADS_MIN_PCT", "0.03")


# ---------------------------------------------------------------------------
# Telegram -- reuse BTC 5min settings
# ---------------------------------------------------------------------------
def _tg_send(text: str):
    """Send Telegram notification using BTC 5min's live telegram settings."""
    try:
        enabled = bool(getattr(config.trading, "paper_telegram_notify_open", False))
        if not enabled:
            return
        token = str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
        chat_id = str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
        if not token or not chat_id:
            return
        send_telegram_message(token=token, chat_id=chat_id, text=text)
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_prob(value) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if 0.0 < v < 1.0:
        return v
    return None


def _eth_price_at(conn, ts: float) -> float | None:
    """Get ETH price closest to ts from eth_ticks."""
    row = fetch_one(
        conn,
        "SELECT price FROM %s WHERE ts <= %%s ORDER BY ts DESC LIMIT 1" % PRICE_TABLE,
        (float(ts),),
    )
    if row and row[0] is not None:
        return float(row[0])
    row = fetch_one(
        conn,
        "SELECT price FROM %s WHERE ts >= %%s ORDER BY ts ASC LIMIT 1" % PRICE_TABLE,
        (float(ts),),
    )
    if row and row[0] is not None:
        return float(row[0])
    return None


def _btc_price_at(conn, ts: float) -> float | None:
    """Get BTC price at timestamp from btc_ticks."""
    row = fetch_one(conn, "SELECT price FROM btc_ticks WHERE ts <= %s ORDER BY ts DESC LIMIT 1", (float(ts),))
    return float(row[0]) if row and row[0] is not None else None


def _btc_range_pct(conn, ts: float, lookback: float) -> float:
    """BTC price range % over lookback seconds."""
    row = fetch_one(conn,
        "SELECT MIN(price), MAX(price) FROM btc_ticks WHERE ts >= %s AND ts <= %s",
        (ts - lookback, ts))
    if not row or row[0] is None or row[1] is None:
        return 999.0  # no data -> pass filter
    lo, hi = float(row[0]), float(row[1])
    mid = (lo + hi) / 2
    if mid <= 0:
        return 999.0
    return (hi - lo) / mid * 100.0


def _check_btc_activity(conn, now_ts: float) -> bool:
    """Return False if BTC market is dead (range < threshold)."""
    if BTC_ACTIVE_MIN_RANGE <= 0:
        return True
    rng = _btc_range_pct(conn, now_ts, BTC_ACTIVE_LOOKBACK)
    if rng < BTC_ACTIVE_MIN_RANGE:
        logger.debug("Skip dead BTC market: range=%.4f%% < %.4f%%", rng, BTC_ACTIVE_MIN_RANGE)
        return False
    return True


def _compute_confidence_mult(conn, direction: str, ws: int, entry_ts: float) -> float:
    """Compute stake multiplier based on BB+VWAP+BTC-leads agreement.
    Returns 1.0 (no boost) or CONF_SIZING_FULL_MULT (all agree)."""
    if not CONF_SIZING_ENABLED:
        return 1.0

    # BTC-leads check
    btc_start = _btc_price_at(conn, float(ws))
    btc_check = _btc_price_at(conn, float(ws) + BTC_LEADS_SEC)
    if not btc_start or not btc_check or btc_start <= 0:
        return 1.0
    btc_chg = (btc_check - btc_start) / btc_start * 100.0
    if abs(btc_chg) < BTC_LEADS_MIN_PCT:
        return 1.0
    btc_dir = "UP" if btc_chg > 0 else "DOWN"
    if btc_dir != direction:
        return 1.0

    # BB extreme check (ETH prices, 60-tick window)
    rows = fetch_one(conn,
        "SELECT price FROM %s WHERE ts <= %%s ORDER BY ts DESC LIMIT 60" % PRICE_TABLE,
        (entry_ts,))
    # Need bulk query
    from db_config import fetch_all_dicts as _fa
    bb_rows = _fa(conn,
        "SELECT price FROM %s WHERE ts <= %%s ORDER BY ts DESC LIMIT 60" % PRICE_TABLE,
        (entry_ts,))
    if len(bb_rows) < 30:
        return 1.0
    prices = [float(r["price"]) for r in bb_rows]
    bb_mean = sum(prices) / len(prices)
    bb_std = (sum((p - bb_mean) ** 2 for p in prices) / len(prices)) ** 0.5
    if bb_std <= 0.01:
        return 1.0
    bb_pos = (prices[0] - bb_mean) / (2 * bb_std)  # prices[0] is most recent
    if abs(bb_pos) <= 0.5:
        return 1.0

    # VWAP agree check
    vwap_rows = _fa(conn,
        "SELECT price, volume FROM %s WHERE ts >= %%s AND ts <= %%s ORDER BY ts" % PRICE_TABLE,
        (float(ws), entry_ts))
    if len(vwap_rows) < 5:
        return 1.0
    sum_pv = sum(float(r["price"]) * float(r.get("volume") or 0) for r in vwap_rows)
    sum_v = sum(float(r.get("volume") or 0) for r in vwap_rows)
    if sum_v <= 0:
        return 1.0
    vwap = sum_pv / sum_v
    cur_price = float(vwap_rows[-1]["price"])
    vwap_agree = (direction == "UP" and cur_price > vwap) or \
                 (direction == "DOWN" and cur_price < vwap)
    if not vwap_agree:
        return 1.0

    # All three agree!
    logger.info("Full confidence: BTC-leads(%.3f%%) + BB(%.2f) + VWAP -> %.1fx ws=%s",
                btc_chg, bb_pos, CONF_SIZING_FULL_MULT, ws)
    return CONF_SIZING_FULL_MULT


def _current_window_start(now: float) -> int:
    """Floor timestamp to 300-second boundary."""
    return int(now // INTERVAL) * INTERVAL


# ---------------------------------------------------------------------------
# Table init (uses schema from db_config._multi_market_tables, but ensure it)
# ---------------------------------------------------------------------------

def _ensure_table(conn):
    """Create paper_trades_eth5 if not exists."""
    sql = """
    CREATE TABLE IF NOT EXISTS %s (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        window_start BIGINT NOT NULL,
        window_end BIGINT,
        direction VARCHAR(16),
        stake DOUBLE,
        entry_price DOUBLE,
        payout_multiple DOUBLE,
        shares DOUBLE,
        potential_win_pnl DOUBLE,
        signal_confidence DOUBLE,
        signal_reason TEXT,
        status VARCHAR(16) DEFAULT 'OPEN',
        opened_at DOUBLE NOT NULL,
        closed_at DOUBLE,
        actual_outcome VARCHAR(16),
        won TINYINT,
        pnl DOUBLE,
        roi_pct DOUBLE,
        initial_capital DOUBLE,
        risk_fraction DOUBLE,
        close_reason TEXT,
        archived_at REAL,
        UNIQUE KEY uq_pt_eth5_ws (window_start),
        INDEX idx_pt_eth5_status (status),
        INDEX idx_pt_eth5_closed (closed_at)
    ) ENGINE=InnoDB
    """ % TRADES_TABLE
    try:
        execute_write(conn, sql)
    except Exception:
        pass
    conn.commit()


# ---------------------------------------------------------------------------
# Signal cache reader
# ---------------------------------------------------------------------------

def _read_signal(conn) -> dict | None:
    """Read signal from signal_cache_eth5 (written by signal generator).
    Returns None if stale (>5s) or missing."""
    row = fetch_one_dict(
        conn,
        "SELECT * FROM %s WHERE id = 1" % SIGNAL_TABLE,
    )
    if not row:
        return None
    age = time.time() - float(row.get("ts") or 0)
    if age > 5.0:
        return None
    return row


# ---------------------------------------------------------------------------
# Settlement: determine outcome from eth_ticks
# ---------------------------------------------------------------------------

def _settle_outcome(conn, window_start: int, window_end: int) -> str | None:
    """Determine UP/DOWN outcome for ETH 5-min window.

    Priority:
    1. Gamma API finalPrice (authoritative Chainlink settlement)
    2. CLOB odds near expiry (93%+ accurate)
    3. eth_ticks price comparison (fallback)

    Returns None if no data available yet.
    """
    # --- 1. Try Gamma API first (authoritative) ---
    try:
        import httpx
        slug = f"eth-updown-5m-{window_start}"
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
                outcome = "UP" if gamma_final >= gamma_ptb else "DOWN"
                logger.info(
                    "Settlement via Gamma: ws=%s ptb=$%.2f final=$%.2f outcome=%s",
                    window_start, gamma_ptb, gamma_final, outcome,
                )
                return outcome
    except Exception:
        pass

    # --- 2. CLOB odds near expiry (last 10s of window) ---
    _last_odds = fetch_one_dict(
        conn,
        """SELECT up_best_ask, down_best_ask FROM poly_odds
           WHERE window_start = %s AND slug LIKE 'eth-updown-5m%%'
             AND ts >= %s AND ts <= %s
           ORDER BY ts DESC LIMIT 1""",
        (window_start, float(window_end) - 10, float(window_end) + 5),
    )
    if _last_odds:
        _lo_up = float(_last_odds.get("up_best_ask") or 0.5)
        _lo_dn = float(_last_odds.get("down_best_ask") or 0.5)
        if abs(_lo_up - _lo_dn) > 0.20:  # confident enough
            outcome = "UP" if _lo_up > _lo_dn else "DOWN"
            logger.info(
                "Settlement via CLOB: ws=%s up_ask=%.3f dn_ask=%.3f outcome=%s",
                window_start, _lo_up, _lo_dn, outcome,
            )
            return outcome

    # --- 3. Fallback: eth_ticks price comparison ---
    start_price = _eth_price_at(conn, float(window_start))
    if start_price is None:
        return None

    end_price = _eth_price_at(conn, float(window_end))
    if end_price is None:
        return None

    check_row = fetch_one(
        conn,
        "SELECT ts FROM %s WHERE ts >= %%s ORDER BY ts ASC LIMIT 1" % PRICE_TABLE,
        (float(window_end) - 5,),
    )
    if not check_row:
        return None

    if end_price >= start_price:
        return "UP"
    else:
        return "DOWN"


def _backfill_unresolved_trades(conn) -> int:
    """Re-settle trades that may have wrong outcome (Gamma API now available).

    BTC 5min does this for market_windows; we do it for paper_trades_eth5 directly.
    Checks closed trades in last 30min and re-verifies via Gamma API.
    """
    now_ts = time.time()
    rows = fetch_all_dicts(
        conn,
        """SELECT id, window_start, window_end, direction, stake, shares,
                  actual_outcome, pnl
           FROM %s
           WHERE status = 'CLOSED' AND archived_at IS NULL
             AND closed_at > %%s
           ORDER BY window_start ASC
           LIMIT 20""" % TRADES_TABLE,
        (now_ts - 1800,),  # last 30min
    )
    if not rows:
        return 0

    updated = 0
    for row in rows:
        ws = int(row["window_start"])
        we = int(row.get("window_end") or (ws + INTERVAL))
        old_outcome = str(row.get("actual_outcome") or "")

        # Try Gamma API
        try:
            import httpx
            slug = f"eth-updown-5m-{ws}"
            resp = httpx.get(
                f"https://gamma-api.polymarket.com/events?slug={slug}&limit=1",
                timeout=5,
            )
            events = resp.json()
            if not events:
                continue
            meta = events[0].get("eventMetadata") or {}
            if meta.get("priceToBeat") is None or meta.get("finalPrice") is None:
                continue
            gamma_ptb = float(meta["priceToBeat"])
            gamma_final = float(meta["finalPrice"])
            new_outcome = "UP" if gamma_final >= gamma_ptb else "DOWN"
        except Exception:
            continue

        if new_outcome == old_outcome:
            continue  # already correct

        # Outcome changed - recalculate PnL
        direction = str(row["direction"])
        stake = float(row["stake"])
        shares = float(row["shares"])
        won = 1 if new_outcome == direction else 0
        if won:
            raw_pnl = shares - stake
            pnl = apply_fee_to_pnl(raw_pnl, stake)
        else:
            pnl = -stake
        roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0

        execute_write(
            conn,
            """UPDATE %s
               SET actual_outcome=%%s, won=%%s, pnl=%%s, roi_pct=%%s,
                   close_reason='expiry_settlement_corrected'
               WHERE id=%%s""" % TRADES_TABLE,
            (new_outcome, won, pnl, roi_pct, int(row["id"])),
        )
        updated += 1
        old_label = "WIN" if float(row.get("pnl") or 0) > 0 else "LOSS"
        new_label = "WIN" if pnl > 0 else "LOSS"
        logger.warning(
            "BACKFILL CORRECTED ws=%s: %s->%s (%s->%s) pnl=$%+.2f->$%+.2f",
            ws, old_outcome, new_outcome, old_label, new_label,
            float(row.get("pnl") or 0), pnl,
        )
        _tg_send(
            f"[ETH5 PAPER CORRECTED]\n"
            f"window: {ws}\n"
            f"outcome: {old_outcome} -> {new_outcome}\n"
            f"result: {old_label} -> {new_label}\n"
            f"pnl: ${float(row.get('pnl') or 0):+.2f} -> ${pnl:+.2f}\n"
            f"source: Gamma API (ptb=${gamma_ptb:,.2f} final=${gamma_final:,.2f})"
        )

    if updated:
        conn.commit()
        logger.warning("Backfilled %d ETH5 trades from Gamma API", updated)
    return updated


# ---------------------------------------------------------------------------
# Entry scoring (same as BTC paper sim)
# ---------------------------------------------------------------------------

def _compute_entry_score(cached: dict, direction: str, entry_price: float) -> int:
    """Compute entry quality score from signal_cache fields."""
    btc_move = abs(float(cached.get("btc_move_pct") or 0))
    conf = float(cached.get("avg_confidence") or 0)
    ev = float(cached.get("gate_ev") or 0)
    prev = str(cached.get("prev_outcome") or "")
    if prev not in ("UP", "DOWN"):
        prev = None
    ov = float(cached.get("odds_velocity") or 0)
    accel = (
        bool(int(cached.get("btc_accel_ok") or 0))
        if cached.get("btc_accel_ok") is not None
        else False
    )

    score = 0
    if btc_move >= 0.02:
        score += 1
    if prev == direction:
        score += 1
    if entry_price <= 0.45:
        score += 1
    if ev >= 0.20:
        score += 1
    if conf >= 0.7:
        score += 1
    if ov >= 0.02:
        score += 1
    if accel:
        score += 1
    return score


# ---------------------------------------------------------------------------
# Open trade
# ---------------------------------------------------------------------------

def _open_trade(conn, cached: dict, stake_amount: float, sizing_mode: str) -> bool:
    """Attempt to open a paper trade from signal_cache_eth5."""
    now_ts = time.time()

    direction = str(cached.get("direction", "NO_TRADE"))
    if direction not in ("UP", "DOWN"):
        return False

    window_start = int(cached.get("window_start") or 0)
    if window_start <= 0:
        return False

    # Verify signal is for CURRENT window
    current_ws = _current_window_start(now_ts)
    if window_start != current_ws:
        return False

    window_end = window_start + INTERVAL
    elapsed = float(cached.get("seconds_elapsed") or 0)
    remaining = float(cached.get("seconds_remaining") or 0)

    # Timing filters
    if elapsed < ENTRY_START_SEC or elapsed > ENTRY_END_SEC:
        return False
    if remaining < MIN_SECONDS_REMAINING:
        return False
    if direction == "DOWN" and elapsed > DOWN_ENTRY_END_SEC:
        return False

    # Gate from signal generator (guards skipped -- too strict for ETH, matches paper_replay)
    gate_allow = int(cached.get("gate_allow") or 0)

    # Gate lock: hold gate_allow=1 for 5s (consumer-side backup)
    if not hasattr(_open_trade, '_gate_lock'):
        _open_trade._gate_lock = {}
    _glock = _open_trade._gate_lock
    _gate_dir = str(cached.get("direction") or "")
    if gate_allow and _gate_dir in ("UP", "DOWN"):
        _glock["ws"] = window_start
        _glock["ts"] = now_ts
        _glock["dir"] = _gate_dir
        _glock["ev"] = float(cached.get("gate_ev") or 0)
        _glock["reason"] = str(cached.get("gate_reason") or "")
    elif (not gate_allow
          and _glock.get("ws") == window_start
          and (now_ts - _glock.get("ts", 0)) < 5.0
          and _glock.get("dir") == direction):
        gate_allow = 1

    if not gate_allow:
        gate_reason = str(cached.get("gate_reason") or "")
        logger.debug("Skip gate_allow=0 ws=%s: %s", window_start, gate_reason)
        return False

    # Ask prices from CURRENT signal_cache (real market price at read time)
    # NOT snapshot — if ask spiked past max, we correctly skip (can't buy at old price)
    up_ask = _safe_prob(cached.get("up_ask"))
    down_ask = _safe_prob(cached.get("down_ask"))
    entry_price = up_ask if direction == "UP" else down_ask
    if entry_price is None or entry_price <= 0.0 or entry_price >= 1.0:
        logger.warning("No valid %s ask price; skip", direction)
        return False

    # Price range filters
    if entry_price > MAX_ENTRY_PRICE:
        logger.debug("Skip expensive ask=%.3f > %.3f ws=%s", entry_price, MAX_ENTRY_PRICE, window_start)
        return False
    if direction == "DOWN" and entry_price < DOWN_MIN_ENTRY_PRICE:
        logger.debug("Skip cheap DOWN ask=%.3f < %.3f ws=%s", entry_price, DOWN_MIN_ENTRY_PRICE, window_start)
        return False

    # Opposite ask guard
    opp_ask = down_ask if direction == "UP" else up_ask
    if opp_ask is not None and opp_ask >= OPPOSITE_MAX_ASK:
        logger.debug("Skip high opposite ask=%.3f >= %.3f ws=%s", opp_ask, OPPOSITE_MAX_ASK, window_start)
        return False

    # Spread filter
    if up_ask is not None and down_ask is not None:
        spread = abs(float(up_ask) - float(down_ask))
        if spread > MAX_ODDS_SPREAD:
            logger.debug("Skip wide spread=%.3f > %.3f ws=%s", spread, MAX_ODDS_SPREAD, window_start)
            return False

    # Momentum agreement
    btc_move = float(cached.get("btc_move_pct") or 0)
    if direction == "UP" and btc_move < -0.005:
        logger.debug("Skip momentum conflict: UP but move=%.4f%% ws=%s", btc_move, window_start)
        return False
    if direction == "DOWN" and btc_move > 0.005:
        logger.debug("Skip momentum conflict: DOWN but move=+%.4f%% ws=%s", btc_move, window_start)
        return False

    # Max BTC move filter
    if MAX_BTC_MOVE_PCT > 0 and abs(btc_move) > MAX_BTC_MOVE_PCT:
        logger.debug("Skip overextended btc_move=%.4f%% > %.2f%% ws=%s", abs(btc_move), MAX_BTC_MOVE_PCT, window_start)
        return False

    # Score filter
    if MIN_ENTRY_SCORE > 0:
        score = _compute_entry_score(cached, direction, entry_price)
        if score < MIN_ENTRY_SCORE:
            logger.info("Skip low score ws=%s: score=%d < %d dir=%s", window_start, score, MIN_ENTRY_SCORE, direction)
            return False
        logger.info("Score OK: %d/%d dir=%s ask=%.3f ws=%s", score, MIN_ENTRY_SCORE, direction, entry_price, window_start)

    # Technical filters from signal_cache
    if REQUIRE_VWAP_AGREE:
        vwap_ok = int(cached.get("vwap_agree") or 0)
        if not vwap_ok:
            logger.debug("Skip VWAP disagree ws=%s dir=%s", window_start, direction)
            return False
    if REQUIRE_BB_EXTREME:
        bb = float(cached.get("bb_pos") or 0)
        if abs(bb) < BB_THRESHOLD:
            logger.debug("Skip BB not extreme: %.2f < %.1f ws=%s", abs(bb), BB_THRESHOLD, window_start)
            return False
    if MAX_ASK_DRIFT < 900:
        drift = float(cached.get("ask_drift") or 0)
        if drift > MAX_ASK_DRIFT:
            logger.debug("Skip ask drift=%.3f > %.3f ws=%s", drift, MAX_ASK_DRIFT, window_start)
            return False

    # BTC activity filter: skip dead markets
    if not _check_btc_activity(conn, now_ts):
        return False

    # Already traded this window?
    exists = fetch_one(
        conn,
        "SELECT id FROM %s WHERE window_start = %%s AND archived_at IS NULL LIMIT 1" % TRADES_TABLE,
        (int(window_start),),
    )
    if exists:
        return False

    # Determine stake: always use env FIXED_STAKE for per-trade amount
    # stake_amount param = seed capital (for equity tracking), NOT per-trade
    stake = round(float(FIXED_STAKE_DEFAULT), 2)

    # Confidence sizing: multiply when BB+VWAP+BTC-leads all agree
    conf_mult = _compute_confidence_mult(conn, direction, window_start, now_ts)
    if conf_mult > 1.0:
        stake = round(stake * conf_mult, 2)

    if stake < MIN_BET:
        logger.warning("Stake too small: $%.2f < $%.2f", stake, MIN_BET)
        return False

    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
    confidence = float(cached.get("avg_confidence") or 0)
    gate_ev = float(cached.get("gate_ev") or 0)
    gate_reason = str(cached.get("gate_reason") or "")
    opened_at = time.time()

    execute_write(
        conn,
        """INSERT INTO %s
           (window_start, window_end, direction, stake, entry_price,
            payout_multiple, shares, potential_win_pnl,
            signal_confidence, signal_reason,
            initial_capital, risk_fraction,
            status, opened_at)
           VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, 'OPEN', %%s)"""
        % TRADES_TABLE,
        (
            int(window_start),
            int(window_end),
            direction,
            float(stake),
            float(entry_price),
            float(payout_multiple),
            float(shares),
            float(potential_win_pnl),
            float(confidence),
            gate_reason[:500] if gate_reason else "",
            float(stake_amount),
            0.0,
            float(opened_at),
        ),
    )
    conn.commit()

    logger.warning(
        "OPEN ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% conf=%.3f",
        window_start, direction, stake, entry_price,
        gate_ev * 100.0, confidence,
    )

    # Detailed Telegram (same format as BTC 5min)
    _eth_start = _eth_price_at(conn, float(window_start))
    _eth_now = _eth_price_at(conn, opened_at)
    _reason_text = str(gate_reason or "").strip().replace("\n", " ")
    if len(_reason_text) > 260:
        _reason_text = f"{_reason_text[:257]}..."
    _now_utc = datetime.now(timezone.utc).isoformat()
    _tg_send(
        f"[ETH5 PAPER OPEN]\n"
        f"time(UTC): {_now_utc}\n"
        f"side: {direction}\n"
        f"slug: eth-updown-5m\n"
        f"window_start: {window_start}\n"
        f"stake: ${float(stake):,.2f}\n"
        f"entry odds: {float(entry_price):.3f}\n"
        f"Polymarket ask (UP/DOWN): "
        f"{f'{float(up_ask):.3f}' if up_ask else '--'} / "
        f"{f'{float(down_ask):.3f}' if down_ask else '--'}\n"
        f"5m start price: {f'${float(_eth_start):,.2f}' if _eth_start else '--'}\n"
        f"current ETH: {f'${float(_eth_now):,.2f}' if _eth_now else '--'}\n"
        f"to-win total: ${float(shares):,.2f}\n"
        f"expected pnl: ${float(potential_win_pnl):,.2f}\n"
        f"confidence: {float(confidence):.3f}\n"
        f"reason: {_reason_text or '--'}"
    )
    return True


# ---------------------------------------------------------------------------
# Resolve (settle) open trades
# ---------------------------------------------------------------------------

def _resolve_trades(conn) -> int:
    """Settle open trades whose windows have ended."""
    now_ts = time.time()

    open_rows = fetch_all_dicts(
        conn,
        """SELECT id, window_start, window_end, direction, stake, shares, entry_price
           FROM %s
           WHERE status = 'OPEN' AND archived_at IS NULL
           ORDER BY window_start ASC""" % TRADES_TABLE,
    )
    if not open_rows:
        return 0

    resolved = 0
    for row in open_rows:
        trade_id = int(row["id"])
        ws = int(row["window_start"])
        we = int(row.get("window_end") or (ws + INTERVAL))
        direction = str(row["direction"])
        stake = float(row["stake"])
        shares = float(row["shares"])

        # Not yet expired
        if now_ts < we + 5:
            continue

        outcome = _settle_outcome(conn, ws, we)
        if outcome is None:
            # No price data yet, skip
            logger.debug("No settlement data yet for ws=%s", ws)
            continue

        won = 1 if outcome == direction else 0
        if won:
            raw_pnl = shares - stake
            pnl = apply_fee_to_pnl(raw_pnl, stake)
        else:
            pnl = -stake
        roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0

        execute_write(
            conn,
            """UPDATE %s
               SET status='CLOSED',
                   closed_at=%%s,
                   actual_outcome=%%s,
                   won=%%s,
                   pnl=%%s,
                   roi_pct=%%s,
                   close_reason='expiry_settlement'
               WHERE id=%%s""" % TRADES_TABLE,
            (now_ts, outcome, won, pnl, roi_pct, trade_id),
        )
        resolved += 1

        label = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
        logger.warning(
            "%s ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%%",
            label, ws, direction, outcome, pnl, roi_pct,
        )

        # Detailed Telegram (same format as BTC 5min)
        _eth_start = _eth_price_at(conn, float(ws))
        _eth_end = _eth_price_at(conn, float(we))
        _eth_exit = _eth_price_at(conn, now_ts)
        _entry_price = float(row.get("entry_price") or 0)
        _settle_price = 1.0 if outcome == direction else 0.0
        _now_utc = datetime.now(timezone.utc).isoformat()
        _tg_send(
            f"[ETH5 PAPER CLOSE:SETTLEMENT] {label}\n"
            f"time(UTC): {_now_utc}\n"
            f"side: {direction}\n"
            f"slug: eth-updown-5m\n"
            f"window_start: {ws}\n"
            f"stake: ${float(stake):,.2f}\n"
            f"entry odds: {float(_entry_price):.3f}\n"
            f"settlement odds: {float(_settle_price):.3f}\n"
            f"5m start/end(ETH): "
            f"{f'${float(_eth_start):,.2f}' if _eth_start else '--'} / "
            f"{f'${float(_eth_end):,.2f}' if _eth_end else '--'}\n"
            f"ETH at exit: {f'${float(_eth_exit):,.2f}' if _eth_exit else '--'}\n"
            f"outcome: {str(outcome or '--').upper()}\n"
            f"realized pnl: ${float(pnl):,.2f} ({float(roi_pct):+.2f}%)\n"
            f"reason: expiry_settlement"
        )

    if resolved:
        conn.commit()
    return resolved


# ---------------------------------------------------------------------------
# Data freshness check
# ---------------------------------------------------------------------------
_DATA_MAX_AGE = float(os.getenv("ETH5_DATA_MAX_AGE_SEC", "120"))
_last_stale_warn: float = 0.0


def _check_data_fresh(conn) -> bool:
    global _last_stale_warn
    now = time.time()
    row = fetch_one(
        conn,
        "SELECT MAX(ts) FROM %s" % PRICE_TABLE,
    )
    if not row or row[0] is None:
        if now - _last_stale_warn > 120:
            logger.warning("No %s data at all", PRICE_TABLE)
            _last_stale_warn = now
        return False
    age = now - float(row[0])
    if age > _DATA_MAX_AGE:
        if now - _last_stale_warn > 120:
            logger.warning("%s data is %.0fs old (max %.0fs)", PRICE_TABLE, age, _DATA_MAX_AGE)
            _last_stale_warn = now
        return False
    return True


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def show_status(conn):
    stats = fetch_one(
        conn,
        """SELECT
               COUNT(*) AS total,
               SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
               SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl), 0)
           FROM %s
           WHERE archived_at IS NULL""" % TRADES_TABLE,
    )
    total = int(stats[0] or 0)
    open_cnt = int(stats[1] or 0)
    closed_cnt = int(stats[2] or 0)
    wins = int(stats[3] or 0)
    losses = int(stats[4] or 0)
    total_pnl = float(stats[5] or 0.0)
    win_rate = (wins / closed_cnt * 100.0) if closed_cnt > 0 else 0.0

    print("=" * 60)
    print(" ETH 5min Paper Trade Status")
    print("=" * 60)
    print(f" Total:       {total}")
    print(f" Open:        {open_cnt}")
    print(f" Closed:      {closed_cnt}")
    print(f" Wins/Losses: {wins}/{losses}  (WR={win_rate:.1f}%)")
    print(f" Total PnL:   ${total_pnl:+.2f}")
    print("=" * 60)

    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, direction, stake, entry_price,
                  status, actual_outcome, pnl, roi_pct
           FROM %s
           WHERE archived_at IS NULL
           ORDER BY window_start DESC
           LIMIT 12""" % TRADES_TABLE,
    )
    if rows:
        print("Recent trades:")
        for r in rows:
            ws = int(r["window_start"])
            dt = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            status = str(r["status"])
            pnl = r.get("pnl")
            pnl_s = f"${float(pnl):+.2f}" if pnl is not None else "-"
            roi = r.get("roi_pct")
            roi_s = f"{float(roi):+.2f}%" if roi is not None else "-"
            print(
                f"  {dt} | {status:6s} | {r['direction']:4s} | "
                f"ask={float(r['entry_price']):.3f} | "
                f"pnl={pnl_s} roi={roi_s}"
            )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop(stake: float, sizing_mode: str):
    conn = connect_db()
    init_market_schema(conn)
    _ensure_table(conn)

    poll_interval = 0.1

    logger.warning(
        "ETH 5min paper sim started: seed=$%.2f per_trade=$%.2f mode=%s interval=%ds "
        "entry=%.0f-%.0fs remain>=%.0fs max_ask=%.3f score>=%d vwap=%s bb=%s",
        stake, FIXED_STAKE_DEFAULT, sizing_mode, INTERVAL,
        ENTRY_START_SEC, ENTRY_END_SEC, MIN_SECONDS_REMAINING,
        MAX_ENTRY_PRICE, MIN_ENTRY_SCORE, REQUIRE_VWAP_AGREE, REQUIRE_BB_EXTREME,
    )

    try:
        while True:
            try:
                if not _check_data_fresh(conn):
                    time.sleep(poll_interval)
                    continue
                _resolve_trades(conn)
                # Backfill every 60s (Gamma API rate limit)
                if not hasattr(run_loop, '_last_backfill'):
                    run_loop._last_backfill = 0.0
                _now = time.time()
                if _now - run_loop._last_backfill >= 60.0:
                    run_loop._last_backfill = _now
                    _backfill_unresolved_trades(conn)
                cached = _read_signal(conn)
                if cached:
                    _open_trade(conn, cached, stake, sizing_mode)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.exception("Loop error: %s", e)
                # Reconnect on DB errors
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    try:
                        conn = connect_db()
                    except Exception as ce:
                        logger.error("DB reconnect failed: %s", ce)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.warning("ETH 5min paper sim stopped by user")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ETH 5min paper trading simulator")
    parser.add_argument(
        "--stake", type=float, default=100.0,
        help="Paper stake per trade in USD (default: 100)",
    )
    parser.add_argument(
        "--sizing-mode", type=str, default="fixed",
        choices=["fixed"],
        help="Sizing mode (default: fixed)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show paper trade status and exit",
    )
    args = parser.parse_args()

    if args.status:
        conn = connect_db()
        init_market_schema(conn)
        _ensure_table(conn)
        show_status(conn)
        conn.close()
        return

    run_loop(
        stake=float(args.stake),
        sizing_mode=str(args.sizing_mode),
    )


if __name__ == "__main__":
    main()
