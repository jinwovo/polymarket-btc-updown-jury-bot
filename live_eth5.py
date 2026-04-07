"""
Live trading bot for ETH 5-minute Up/Down binary options (Polymarket).

Reads signal_cache_eth5 (written by signal_generator_eth5.py), places real
orders via PolymarketClient, and writes results to live_trades_eth5.

Architecture:
    signal_generator_eth5 --> signal_cache_eth5 --> [this script] --> live_trades_eth5
                                                         |
                                                    PolymarketClient
                                                    (place_entry_order)

Usage:
    python live_eth5.py
    python live_eth5.py --dry-run
    python live_eth5.py --stake 15 --dry-run
    python live_eth5.py --status
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from config import config
from market_config import ETH_5M, env
from db_config import (
    connect_db,
    db_label,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)
from trade_gate import apply_fee_to_pnl
from polymarket_client import PolymarketClient, MarketInfo
from telegram_notifier import send_telegram_message

# ---------------------------------------------------------------------------
# Constants from MarketDef
# ---------------------------------------------------------------------------
MARKET = ETH_5M
INTERVAL = MARKET.interval_seconds            # 300
PRICE_TABLE = MARKET.price_table              # eth_ticks
SLUG_PREFIX = MARKET.slug_prefix              # eth-updown-5m
SIGNAL_TABLE = MARKET.signal_cache_table      # signal_cache_eth5
TRADES_TABLE = MARKET.live_trades_table       # live_trades_eth5
ENV_PREFIX = MARKET.env_prefix                # ETH5_

# ---------------------------------------------------------------------------
# Logging -- bot_live_eth5.log, ASCII only (Windows cp949 safety)
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
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_live_eth5.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)

logger = logging.getLogger("live_eth5")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Entry parameters (ETH5_ prefix -> PAPER_ fallback -> defaults)
# ---------------------------------------------------------------------------
ENTRY_START_SEC = float(env(MARKET, "ENTRY_START_SEC", "60"))
ENTRY_END_SEC = float(env(MARKET, "ENTRY_END_SEC", "240"))
DOWN_ENTRY_END_SEC = float(env(MARKET, "DOWN_ENTRY_END_SEC", "200"))
MAX_ENTRY_PRICE = float(env(MARKET, "MAX_ENTRY_PRICE", "0.58"))
MIN_ENTRY_PRICE = float(env(MARKET, "DOWN_MIN_ENTRY_PRICE", "0.30"))
MAX_ODDS_SPREAD = float(env(MARKET, "MAX_ODDS_SPREAD", "0.12"))
MIN_ENTRY_SCORE = int(env(MARKET, "MIN_ENTRY_SCORE", "3"))
BB_THRESHOLD = float(env(MARKET, "BB_THRESHOLD", "0.5"))
MAX_ASK_DRIFT = float(env(MARKET, "MAX_ASK_DRIFT", "0.08"))
FIXED_STAKE = float(env(MARKET, "FIXED_STAKE", "15"))
SEED_CAPITAL = float(env(MARKET, "SEED_CAPITAL", "100"))
FIXED_SEED_PCT = float(env(MARKET, "FIXED_SEED_PCT", "0.15"))
MAX_BTC_MOVE_PCT = float(env(MARKET, "MAX_BTC_MOVE_PCT", "0.10"))
REQUIRE_MOMENTUM_AGREE = env(MARKET, "REQUIRE_MOMENTUM_AGREE", "true").lower() == "true"
REQUIRE_BB_EXTREME = env(MARKET, "REQUIRE_BB_EXTREME", "false").lower() == "true"
REQUIRE_VWAP_AGREE = env(MARKET, "REQUIRE_VWAP_AGREE", "false").lower() == "true"
REQUIRE_BTC_STILL_MOVING = env(MARKET, "REQUIRE_BTC_STILL_MOVING", "false").lower() == "true"
MIN_BET_SIZE = float(env(MARKET, "MIN_BET_SIZE", "1.00"))

# Telegram
TELEGRAM_ENABLED = env(MARKET, "TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = env(MARKET, "TELEGRAM_BOT_TOKEN", "") or os.getenv("LIVE_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env(MARKET, "TELEGRAM_CHAT_ID", "") or os.getenv("LIVE_TELEGRAM_CHAT_ID", "")


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


def _eth_slug_for_ts(start_ts: int) -> str:
    """Build the ETH 5-min market slug for a given window start timestamp."""
    return f"{SLUG_PREFIX}-{start_ts}"


def _init_tables(conn):
    """Ensure live_trades_eth5 and related tables exist."""
    init_market_schema(conn)
    # Ensure market-specific tables exist
    _create_live_trades_table(conn)
    conn.commit()


def _create_live_trades_table(conn):
    """Create the live_trades_eth5 table if it does not exist."""
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TRADES_TABLE} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        window_start BIGINT NOT NULL UNIQUE,
        window_end BIGINT NOT NULL,
        direction VARCHAR(16) NOT NULL,
        stake DOUBLE NOT NULL,
        entry_price DOUBLE NOT NULL,
        payout_multiple DOUBLE NOT NULL,
        shares DOUBLE NOT NULL,
        potential_win_pnl DOUBLE NOT NULL,
        signal_confidence DOUBLE NULL,
        signal_reason TEXT NULL,
        entry_source VARCHAR(32) NULL,
        close_reason TEXT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
        opened_at DOUBLE NOT NULL,
        closed_at DOUBLE NULL,
        actual_outcome VARCHAR(16) NULL,
        won TINYINT NULL,
        pnl DOUBLE NULL,
        roi_pct DOUBLE NULL,
        INDEX idx_live_eth5_status (status, opened_at),
        INDEX idx_live_eth5_window (window_start)
    ) ENGINE=InnoDB
    """
    try:
        execute_write(conn, sql)
    except Exception as e:
        if "Duplicate key name" not in str(e):
            raise


def _eth_price_at(conn, ts: float) -> float | None:
    """Get ETH price closest to a timestamp (within 30s)."""
    row = fetch_one(
        conn,
        f"SELECT price FROM {PRICE_TABLE} WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
        (ts - 30, ts + 30, ts),
    )
    return float(row[0]) if row else None


def _compute_window_ts(now_ts: float) -> dict:
    """Compute current window start/end and timing."""
    current_start = int(now_ts // INTERVAL) * INTERVAL
    current_end = current_start + INTERVAL
    return {
        "start": current_start,
        "end": current_end,
        "elapsed": now_ts - current_start,
        "remaining": current_end - now_ts,
    }


# ---------------------------------------------------------------------------
# Signal cache reader
# ---------------------------------------------------------------------------
def _read_signal_cache(conn) -> dict | None:
    """Read signal from signal_cache_eth5 (written by signal_generator_eth5).
    Returns None if signal is stale (>5s old) or missing."""
    row = fetch_one_dict(conn, f"SELECT * FROM {SIGNAL_TABLE} WHERE id = 1")
    if not row:
        return None
    age = time.time() - float(row.get("ts") or 0)
    if age > 5.0:
        return None
    return row


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------
def _send_telegram(text: str):
    """Best-effort Telegram notification (non-blocking)."""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        send_telegram_message(
            token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
        )
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Entry logic
# ---------------------------------------------------------------------------
def _check_entry(
    conn,
    cached: dict,
    poly_client: PolymarketClient,
    dry_run: bool,
    base_stake: float,
) -> bool:
    """Check entry conditions and place a live order if conditions are met.
    Returns True if a trade was opened."""

    direction = str(cached.get("direction", "NO_TRADE"))
    if direction not in ("UP", "DOWN"):
        return False

    now_ts = time.time()
    window_start = int(cached.get("window_start") or 0)
    if window_start <= 0:
        return False

    # Verify signal is for the CURRENT window
    current_ws = int(now_ts // INTERVAL) * INTERVAL
    if window_start != current_ws:
        return False

    window_end = window_start + INTERVAL
    seconds_elapsed = float(cached.get("seconds_elapsed") or 0)

    # Timing gate
    if seconds_elapsed < ENTRY_START_SEC or seconds_elapsed > ENTRY_END_SEC:
        return False

    # DOWN-specific late entry cutoff
    if direction == "DOWN" and seconds_elapsed > DOWN_ENTRY_END_SEC:
        logger.info("Skip DOWN late entry: elapsed=%.0fs > %.0fs",
                     seconds_elapsed, DOWN_ENTRY_END_SEC)
        return False

    # gate_allow from signal generator
    gate_allow = int(cached.get("gate_allow") or 0)
    if not gate_allow:
        logger.debug("Skip gate_allow=0 ws=%s dir=%s reason=%s",
                      window_start, direction, cached.get("gate_reason", ""))
        return False

    # guards_passed from signal generator
    if not int(cached.get("guards_passed") or 0):
        logger.debug("Skip guards_passed=0 ws=%s dir=%s", window_start, direction)
        return False

    # Ask prices from signal cache
    up_ask = _safe_prob(cached.get("up_ask"))
    down_ask = _safe_prob(cached.get("down_ask"))
    entry_price = up_ask if direction == "UP" else down_ask
    if entry_price is None:
        logger.warning("No %s ask price available ws=%s; skipping", direction, window_start)
        return False
    entry_price = float(entry_price)
    if entry_price <= 0.0 or entry_price >= 1.0:
        logger.warning("Invalid entry ask %.4f ws=%s; skipping", entry_price, window_start)
        return False

    # Price range
    if entry_price > MAX_ENTRY_PRICE:
        logger.debug("Skip expensive ws=%s: ask=%.3f > %.3f",
                      window_start, entry_price, MAX_ENTRY_PRICE)
        return False
    if direction == "DOWN" and entry_price < MIN_ENTRY_PRICE:
        logger.debug("Skip cheap DOWN ws=%s: ask=%.3f < %.3f",
                      window_start, entry_price, MIN_ENTRY_PRICE)
        return False

    # Spread filter
    if up_ask is not None and down_ask is not None:
        spread = abs(float(up_ask) - float(down_ask))
        if spread > MAX_ODDS_SPREAD:
            logger.debug("Skip wide spread ws=%s: %.3f > %.3f",
                          window_start, spread, MAX_ODDS_SPREAD)
            return False

    # Max ETH move filter (overextended = mean reversion risk)
    eth_move_raw = float(cached.get("btc_move_pct") or 0)
    eth_move = abs(eth_move_raw)
    if MAX_BTC_MOVE_PCT > 0 and eth_move > MAX_BTC_MOVE_PCT:
        logger.info("Skip overextended: eth_move=%.4f%% > %.2f%%",
                     eth_move, MAX_BTC_MOVE_PCT)
        return False

    # Momentum agreement
    if REQUIRE_MOMENTUM_AGREE:
        if direction == "UP" and eth_move_raw < -0.005:
            logger.info("Skip momentum conflict: UP but eth_move=%.4f%%", eth_move_raw)
            return False
        if direction == "DOWN" and eth_move_raw > 0.005:
            logger.info("Skip momentum conflict: DOWN but eth_move=+%.4f%%", eth_move_raw)
            return False

    # BB extreme filter
    if REQUIRE_BB_EXTREME:
        bb_pos_val = cached.get("bb_pos")
        if bb_pos_val is None:
            logger.debug("Skip no bb_pos ws=%s", window_start)
            return False
        if abs(float(bb_pos_val)) < BB_THRESHOLD:
            logger.debug("Skip BB not extreme ws=%s: bb_pos=%.3f < %.1f",
                          window_start, float(bb_pos_val), BB_THRESHOLD)
            return False

    # VWAP agree filter
    if REQUIRE_VWAP_AGREE:
        vwap_val = cached.get("vwap_agree")
        if vwap_val is None or int(vwap_val) == 0:
            logger.debug("Skip VWAP disagree ws=%s dir=%s", window_start, direction)
            return False

    # Ask drift filter
    if MAX_ASK_DRIFT > 0:
        drift_val = cached.get("ask_drift")
        if drift_val is not None and float(drift_val) > MAX_ASK_DRIFT:
            logger.debug("Skip ask drift ws=%s: %.3f > %.3f",
                          window_start, float(drift_val), MAX_ASK_DRIFT)
            return False

    # BTC still moving filter
    if REQUIRE_BTC_STILL_MOVING:
        bsm_val = cached.get("btc_still_moving")
        if bsm_val is None or int(bsm_val) == 0:
            logger.debug("Skip ETH not still moving ws=%s", window_start)
            return False

    # No duplicate trade for this window
    exists = fetch_one(
        conn,
        f"SELECT id FROM {TRADES_TABLE} WHERE window_start = ? LIMIT 1",
        (int(window_start),),
    )
    if exists:
        return False

    # Score filter
    if MIN_ENTRY_SCORE > 0:
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

        if score < MIN_ENTRY_SCORE:
            logger.info("Skip low score ws=%s: score=%d < %d (dir=%s)",
                        window_start, score, MIN_ENTRY_SCORE, direction)
            return False
        logger.info("Score OK: %d/%d (eth=%.3f%% conf=%.2f ev=%.2f prev=%s ov=%.3f accel=%s)",
                     score, MIN_ENTRY_SCORE, btc_move, conf, ev, prev, ov, accel)

    # Sizing
    stake = round(base_stake, 2)

    # Dynamic sizing via quality_score
    qs = cached.get("quality_score")
    if qs is not None:
        qs = float(qs)
        stake = round(stake * qs, 2)
        logger.debug("Dynamic sizing: quality=%.2f stake=$%.2f", qs, stake)

    if stake < max(5.0, MIN_BET_SIZE):
        logger.warning("Stake too small ws=%s: $%.2f", window_start, stake)
        return False

    # Compute trade fields
    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
    confidence = float(cached.get("avg_confidence") or 0)
    gate_ev = float(cached.get("gate_ev") or 0)
    gate_reason = str(cached.get("gate_reason") or "")
    opened_at = time.time()

    # ------------------------------------------------------------------
    # Place order (live) or simulate (dry-run)
    # ------------------------------------------------------------------
    order_result = None
    executed_price = entry_price
    executed_stake = stake
    entry_source = "dry_run" if dry_run else "live_fak"

    if dry_run:
        logger.warning(
            "[DRY-RUN] OPEN ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% conf=%.2f",
            window_start, direction, stake, entry_price,
            gate_ev * 100.0, confidence,
        )
    else:
        # Resolve market to get token IDs
        market_info = _find_eth_market(poly_client, window_start)
        if market_info is None:
            logger.warning("Cannot find ETH market for ws=%s; skipping order", window_start)
            return False

        token_id = (
            market_info.up_token_id if direction == "UP"
            else market_info.down_token_id
        )
        if not token_id:
            logger.warning("No %s token_id for ws=%s; skipping", direction, window_start)
            return False

        logger.warning(
            ">>> TRADE: %s | $%.2f @ %.4f | ev=%+.3f%% conf=%.2f | ws=%s",
            direction, stake, entry_price, gate_ev * 100.0, confidence, window_start,
        )

        try:
            order_result = asyncio.get_event_loop().run_until_complete(
                poly_client.place_entry_order(
                    token_id=token_id,
                    side=direction,
                    amount=stake,
                    reference_ask=entry_price,
                )
            )
        except Exception as e:
            logger.error("Order placement failed ws=%s: %s", window_start, e)
            return False

        if order_result is None:
            logger.error("Order result is None ws=%s -- uncertain fill, skipping DB write", window_start)
            return False

        if bool(order_result.get("uncertain_fill", False)):
            logger.error(
                "Uncertain fill ws=%s: %s -- manual review required",
                window_start, order_result.get("reason", "unknown"),
            )
            return False

        if not bool(order_result.get("filled", False)):
            logger.info(
                "Order not filled ws=%s: mode=%s status=%s reason=%s",
                window_start,
                order_result.get("mode"),
                order_result.get("status"),
                order_result.get("reason"),
            )
            return False

        # Use exchange-reported fill data if available
        _exec_notional = float(order_result.get("executed_notional") or 0.0)
        _exec_price = float(order_result.get("executed_price") or 0.0)
        _exec_size = float(order_result.get("executed_size") or 0.0)

        if _exec_size > 0.0 and 0.0 < _exec_price < 1.0:
            _exec_notional = _exec_size * _exec_price

        if _exec_notional > 0.0:
            executed_stake = _exec_notional
        if 0.0 < _exec_price < 1.0:
            executed_price = _exec_price

        # Recalculate with actual fill
        stake = executed_stake
        entry_price = executed_price
        shares = stake / entry_price
        payout_multiple = 1.0 / entry_price
        potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
        entry_source = str(order_result.get("mode") or "live_fak")

    # INSERT trade into live_trades_eth5
    execute_write(
        conn,
        f"""INSERT INTO {TRADES_TABLE}
           (window_start, window_end, direction, stake, entry_price, payout_multiple,
            shares, potential_win_pnl, signal_confidence, signal_reason,
            entry_source, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
           ON DUPLICATE KEY UPDATE
           direction=VALUES(direction),
           stake=VALUES(stake),
           entry_price=VALUES(entry_price),
           payout_multiple=VALUES(payout_multiple),
           shares=VALUES(shares),
           potential_win_pnl=VALUES(potential_win_pnl),
           signal_confidence=COALESCE(VALUES(signal_confidence), signal_confidence),
           signal_reason=COALESCE(VALUES(signal_reason), signal_reason),
           entry_source=COALESCE(VALUES(entry_source), entry_source),
           status='OPEN',
           opened_at=VALUES(opened_at),
           closed_at=NULL, actual_outcome=NULL, won=NULL, pnl=NULL, roi_pct=NULL,
           close_reason=NULL""",
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
            gate_reason,
            entry_source,
            float(opened_at),
        ),
    )
    conn.commit()

    logger.warning(
        "OPEN  ws=%s dir=%s stake=$%.2f ask=%.4f ev=%+.3f%% conf=%.2f src=%s",
        window_start, direction, stake, entry_price,
        gate_ev * 100.0, confidence, entry_source,
    )

    # Telegram notification
    _send_telegram(
        f"[ETH5 LIVE] OPEN {direction}\n"
        f"stake: ${stake:.2f} @ {entry_price:.4f}\n"
        f"ev: {gate_ev*100:.1f}% conf: {confidence:.2f}\n"
        f"window: {window_start}"
    )

    return True


# ---------------------------------------------------------------------------
# Market resolution (Gamma API via PolymarketClient)
# ---------------------------------------------------------------------------
_market_cache: dict[int, MarketInfo] = {}


def _find_eth_market(
    poly_client: PolymarketClient,
    start_timestamp: int,
) -> Optional[MarketInfo]:
    """Find an ETH 5-min market by its start timestamp.
    Queries Gamma API using the ETH slug."""
    if start_timestamp in _market_cache:
        return _market_cache[start_timestamp]

    slug = _eth_slug_for_ts(start_timestamp)
    try:
        import httpx
        # Synchronous Gamma API query (same pattern as PolymarketClient.find_market)
        gamma_url = config.polymarket.gamma_url
        resp = httpx.get(
            f"{gamma_url}/markets",
            params={"slug": slug, "closed": "false"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("Gamma API returned %s for slug=%s", resp.status_code, slug)
            return None

        markets = resp.json()
        if not markets:
            resp2 = httpx.get(
                f"{gamma_url}/events",
                params={"slug": slug},
                timeout=10.0,
            )
            if resp2.status_code == 200:
                events = resp2.json()
                if events and len(events) > 0:
                    event = events[0]
                    if "markets" in event and len(event["markets"]) > 0:
                        markets = event["markets"]

        if not markets:
            logger.debug("No ETH market found for slug=%s", slug)
            return None

        market = markets[0] if isinstance(markets, list) else markets

        tokens = market.get("tokens", [])
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []

        up_token = ""
        down_token = ""
        up_price = 0.5
        down_price = 0.5

        if tokens and len(tokens) >= 2:
            for t in tokens:
                if not isinstance(t, dict):
                    continue
                outcome = str(t.get("outcome", "")).lower()
                if outcome == "up":
                    up_token = str(t.get("token_id", t.get("tokenId", "")))
                    try:
                        up_price = float(t.get("price", 0.5))
                    except Exception:
                        pass
                elif outcome == "down":
                    down_token = str(t.get("token_id", t.get("tokenId", "")))
                    try:
                        down_price = float(t.get("price", 0.5))
                    except Exception:
                        pass
        else:
            clob_ids = market.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if clob_ids and len(clob_ids) >= 2:
                up_token = str(clob_ids[0])
                down_token = str(clob_ids[1])

        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []
        if outcome_prices and len(outcome_prices) >= 2:
            try:
                up_price = float(outcome_prices[0])
            except Exception:
                pass
            try:
                down_price = float(outcome_prices[1])
            except Exception:
                pass

        info = MarketInfo(
            condition_id=market.get("condition_id", market.get("conditionId", "")),
            question=market.get("question", ""),
            slug=slug,
            start_timestamp=start_timestamp,
            end_timestamp=start_timestamp + INTERVAL,
            up_token_id=up_token,
            down_token_id=down_token,
            up_price=up_price,
            down_price=down_price,
            active=market.get("active", True),
        )
        _market_cache[start_timestamp] = info
        return info

    except Exception as e:
        logger.warning("find_eth_market failed for ws=%s: %s", start_timestamp, e)
        return None


def _fetch_eth_settlement_outcome(start_timestamp: int) -> Optional[str]:
    """Query Gamma API for the ETH 5-min settlement outcome.
    Returns 'UP', 'DOWN', or None if not yet settled."""
    slug = _eth_slug_for_ts(start_timestamp)
    try:
        import httpx
        gamma_url = config.polymarket.gamma_url
        resp = httpx.get(
            f"{gamma_url}/markets",
            params={"slug": slug},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None

        markets = resp.json()
        if not markets:
            resp2 = httpx.get(
                f"{gamma_url}/events",
                params={"slug": slug},
                timeout=10.0,
            )
            if resp2.status_code == 200:
                events = resp2.json()
                if events and len(events) > 0:
                    event = events[0]
                    if "markets" in event and len(event["markets"]) > 0:
                        markets = event["markets"]

        if not markets:
            return None

        market = markets[0] if isinstance(markets, list) else markets

        # Check outcomePrices: [1,0] = UP won, [0,1] = DOWN won
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        if outcome_prices and len(outcome_prices) >= 2:
            try:
                p_up = float(outcome_prices[0])
                p_down = float(outcome_prices[1])
                if p_up >= 0.95 and p_down <= 0.05:
                    return "UP"
                if p_down >= 0.95 and p_up <= 0.05:
                    return "DOWN"
            except Exception:
                pass

        # Check finalPrice
        final_price = market.get("finalPrice")
        if final_price is not None:
            try:
                fp = float(final_price)
                if fp >= 0.95:
                    return "UP"
                if fp <= 0.05:
                    return "DOWN"
            except Exception:
                pass

        return None
    except Exception as e:
        logger.debug("fetch_eth_settlement_outcome failed for ws=%s: %s", start_timestamp, e)
        return None


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def _resolve_open_trades(conn) -> int:
    """Resolve open trades at window end using ETH price."""
    now_ts = time.time()

    open_rows = fetch_all_dicts(
        conn,
        f"""SELECT id, window_start, window_end, direction, stake, shares, entry_price
           FROM {TRADES_TABLE}
           WHERE status = 'OPEN'
           ORDER BY window_start ASC""",
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

        # Must wait until window has ended (+5s buffer)
        if now_ts < we + 5:
            continue

        # PRIMARY: Gamma API settlement outcome
        outcome = _fetch_eth_settlement_outcome(ws)

        # FALLBACK 1: market_windows table (if signal_generator wrote it)
        if outcome is None:
            mw_row = fetch_one(
                conn,
                "SELECT actual_outcome FROM market_windows WHERE window_start = ? AND slug LIKE ? LIMIT 1",
                (ws, f"{SLUG_PREFIX}%"),
            )
            if mw_row and mw_row[0] in ("UP", "DOWN"):
                outcome = mw_row[0]

        # FALLBACK 2: eth_ticks price at window_end vs window_start
        if outcome is None:
            eth_start = _eth_price_at(conn, float(ws))
            eth_end = _eth_price_at(conn, float(we))
            if eth_start is not None and eth_end is not None and eth_start > 0:
                if eth_end > eth_start:
                    outcome = "UP"
                elif eth_end < eth_start:
                    outcome = "DOWN"
                else:
                    outcome = "UP"  # tie goes to UP (Polymarket convention)
                logger.info("Settlement ws=%s: eth_start=$%.2f eth_end=$%.2f -> %s",
                            ws, eth_start, eth_end, outcome)
            else:
                logger.debug("No ETH price for settlement ws=%s: start=%s end=%s",
                             ws, eth_start, eth_end)
                continue

        # Compute PnL
        won = 1 if outcome == direction else 0
        if won:
            raw_pnl = shares - stake
            pnl = apply_fee_to_pnl(raw_pnl, stake)
        else:
            pnl = -stake
        roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
        closed_at = now_ts

        execute_write(
            conn,
            f"""UPDATE {TRADES_TABLE}
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
        conn.commit()
        resolved += 1

        tag = "PROFIT" if pnl > 0 else ("LOSS  " if pnl < 0 else "FLAT  ")
        logger.warning(
            "%s ws=%s dir=%s outcome=%s pnl=$%+.2f roi=%+.2f%%",
            tag, ws, direction, outcome, pnl, roi_pct,
        )

        # Telegram close notification
        _send_telegram(
            f"[ETH5 LIVE] {'WIN' if won else 'LOSS'} {direction}\n"
            f"outcome: {outcome}\n"
            f"pnl: ${pnl:+.2f} ({roi_pct:+.1f}%)\n"
            f"window: {ws}"
        )

    return resolved


# ---------------------------------------------------------------------------
# Data freshness
# ---------------------------------------------------------------------------
_last_stale_warn_ts: float = 0.0


def _check_data_freshness(conn) -> bool:
    """Return True if ETH tick data is fresh enough."""
    global _last_stale_warn_ts
    now = time.time()
    try:
        row = fetch_one(conn, f"SELECT MAX(ts) FROM {PRICE_TABLE}")
        if not row or row[0] is None:
            if now - _last_stale_warn_ts >= 120:
                logger.warning("DATA STALE: No %s data at all!", PRICE_TABLE)
                _last_stale_warn_ts = now
            return False
        age = now - float(row[0])
        if age > 120:
            if now - _last_stale_warn_ts >= 120:
                logger.warning("DATA STALE: %s data is %.0fs old", PRICE_TABLE, age)
                _last_stale_warn_ts = now
            return False
        return True
    except Exception as e:
        logger.error("Data freshness check error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Stale trade reconciliation (startup)
# ---------------------------------------------------------------------------
def _reconcile_stale_trades(conn):
    """Backfill any OPEN trades from previous runs whose windows have ended."""
    resolved = _resolve_open_trades(conn)
    if resolved > 0:
        logger.warning("Startup: resolved %d stale open trades", resolved)


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------
def show_status(conn, initial_capital: float):
    stats = fetch_one(
        conn,
        f"""SELECT
            COUNT(*),
            SUM(status='OPEN'),
            SUM(status='CLOSED'),
            SUM(won=1),
            SUM(won=0 AND status='CLOSED'),
            COALESCE(SUM(pnl), 0)
           FROM {TRADES_TABLE}""",
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
 ETH 5min LIVE TRADE STATUS
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
        f"""SELECT window_start, direction, stake, entry_price, payout_multiple,
                  potential_win_pnl, status, actual_outcome, pnl, roi_pct, entry_source
           FROM {TRADES_TABLE}
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
            src = str(r.get("entry_source") or "-")
            print(
                f"  {dt} | {status:6s} | {r['direction']:4s} | "
                f"ask={float(r['entry_price']):.4f} payx={float(r['payout_multiple']):.3f} | "
                f"potWin={float(r['potential_win_pnl']):+.2f} | pnl={pnl_s} roi={roi_s} | {src}"
            )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_loop(
    stake: float,
    interval_sec: float,
    dry_run: bool,
):
    conn = connect_db()
    _init_tables(conn)

    # Reconcile stale open trades from previous run
    _reconcile_stale_trades(conn)

    base_stake = max(1.0, float(stake))
    mode_label = "DRY-RUN" if dry_run else "*** LIVE ***"

    # Initialize PolymarketClient for order placement (live only)
    poly_client = None
    if not dry_run:
        poly_client = PolymarketClient()

    logger.warning(
        "ETH5 live trading %s: stake=$%.2f interval=%.1fs "
        "entry=%s~%ss max_ask=%.2f spread<=%.2f score>=%d",
        mode_label, base_stake, interval_sec,
        ENTRY_START_SEC, ENTRY_END_SEC,
        MAX_ENTRY_PRICE, MAX_ODDS_SPREAD, MIN_ENTRY_SCORE,
    )

    try:
        while True:
            try:
                if not _check_data_freshness(conn):
                    time.sleep(interval_sec)
                    continue

                # Resolve any open trades whose window has ended
                _resolve_open_trades(conn)

                # Read signal and check entry
                cached = _read_signal_cache(conn)
                if cached is not None:
                    _check_entry(conn, cached, poly_client, dry_run, base_stake)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.exception("Loop error (continue): %s", e)

                # Reconnect DB on persistent failures
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = connect_db()
                    _init_tables(conn)

            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("ETH5 live trading stopped by user")
    finally:
        # Final settlement pass
        try:
            _resolve_open_trades(conn)
        except Exception:
            pass

        # Close PolymarketClient
        if poly_client is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(poly_client.close())
                else:
                    loop.run_until_complete(poly_client.close())
            except Exception:
                pass

        conn.close()
        logger.warning("ETH5 live trading shutdown complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ETH 5min live trading bot")
    parser.add_argument("--stake", type=float, default=float(FIXED_STAKE),
                        help=f"Stake per trade in USD (default: {FIXED_STAKE})")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Polling interval seconds (default: 0.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without placing real orders")
    parser.add_argument("--status", action="store_true",
                        help="Show live trade status and exit")
    args = parser.parse_args()

    conn = connect_db()
    _init_tables(conn)
    conn.close()

    if args.status:
        conn = connect_db()
        show_status(conn, SEED_CAPITAL)
        conn.close()
        return

    run_loop(
        stake=float(args.stake),
        interval_sec=float(args.interval),
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
