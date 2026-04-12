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
    """Determine UP/DOWN by comparing eth_ticks price at window_end vs window_start.
    Returns None if prices not yet available."""
    start_price = _eth_price_at(conn, float(window_start))
    if start_price is None:
        return None

    # Use price at or slightly before window_end
    end_price = _eth_price_at(conn, float(window_end))
    if end_price is None:
        return None

    # Need at least one tick after window_end - 5s to consider settled
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

    # Guards and gate from signal generator
    guards_passed = int(cached.get("guards_passed") or 0)
    gate_allow = int(cached.get("gate_allow") or 0)
    if not guards_passed:
        logger.debug("Skip guards_passed=0 ws=%s dir=%s", window_start, direction)
        return False

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

    # Ask prices from signal cache
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

    # Already traded this window?
    exists = fetch_one(
        conn,
        "SELECT id FROM %s WHERE window_start = %%s AND archived_at IS NULL LIMIT 1" % TRADES_TABLE,
        (int(window_start),),
    )
    if exists:
        return False

    # Determine stake
    if sizing_mode == "fixed":
        stake = round(float(FIXED_STAKE_DEFAULT), 2)
        # Override from CLI/env
        _cli_stake = float(stake_amount)
        if _cli_stake > 0:
            stake = round(_cli_stake, 2)
    else:
        stake = round(float(stake_amount), 2)

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
        "ETH 5min paper sim started: stake=$%.2f mode=%s interval=%ds "
        "entry=%.0f-%.0fs remain>=%.0fs max_ask=%.3f score>=%d",
        stake, sizing_mode, INTERVAL,
        ENTRY_START_SEC, ENTRY_END_SEC, MIN_SECONDS_REMAINING,
        MAX_ENTRY_PRICE, MIN_ENTRY_SCORE,
    )

    try:
        while True:
            try:
                if not _check_data_fresh(conn):
                    time.sleep(poll_interval)
                    continue
                _resolve_trades(conn)
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
