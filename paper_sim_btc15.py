"""
Paper trading simulator for BTC 15-minute Up/Down binary options.

Simplified version of paper_trade_sim.py that:
  - Reads from signal_cache_btc15 (written by signal_generator_btc15.py)
  - Writes to paper_trades_btc15
  - Uses 900-second intervals
  - Entry: BB extreme, VWAP agree, ask drift, btc_still_moving, dynamic sizing
  - Hold to settlement (no early exit)
  - Settlement: btc_ticks price at window_end vs window_start

Usage:
    python paper_sim_btc15.py
    python paper_sim_btc15.py --stake 100 --sizing-mode fixed
    python paper_sim_btc15.py --status
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from config import config
from market_config import BTC_15M, env
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
from telegram_notifier import send_telegram_message

# ---------------------------------------------------------------------------
# Logging -- bot_paper_btc15.log
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
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_paper_btc15.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)

logger = logging.getLogger("paper_btc15")

# ---------------------------------------------------------------------------
# Constants (from env, with BTC15_ prefix fallback to PAPER_)
# ---------------------------------------------------------------------------
INTERVAL = BTC_15M.interval_seconds  # 900

ENTRY_START_SEC = float(env(BTC_15M, "ENTRY_START_SEC", "180"))
ENTRY_END_SEC = float(env(BTC_15M, "ENTRY_END_SEC", "720"))
MAX_ENTRY_PRICE = float(env(BTC_15M, "MAX_ENTRY_PRICE", "0.58"))
MIN_ENTRY_PRICE = float(env(BTC_15M, "DOWN_MIN_ENTRY_PRICE", "0.30"))
BB_THRESHOLD = float(env(BTC_15M, "BB_THRESHOLD", "0.5"))
MAX_ASK_DRIFT = float(env(BTC_15M, "MAX_ASK_DRIFT", "0.08"))
MIN_ENTRY_SCORE = int(env(BTC_15M, "MIN_ENTRY_SCORE", "3"))
FIXED_STAKE = float(env(BTC_15M, "FIXED_STAKE", "100"))
MAX_ODDS_SPREAD = float(env(BTC_15M, "MAX_ODDS_SPREAD", "0.12"))

SIGNAL_TABLE = BTC_15M.signal_cache_table       # signal_cache_btc15
TRADES_TABLE = BTC_15M.paper_trades_table        # paper_trades_btc15
PRICE_TABLE = BTC_15M.price_table                # btc_ticks
SLUG_PREFIX = BTC_15M.slug_prefix                # btc-updown-15m


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


def _init_tables(conn):
    """Ensure paper_trades_btc15 and signal_cache_btc15 tables exist."""
    init_market_schema(conn)
    conn.commit()


def _btc_price_at(conn, ts: float) -> float | None:
    """Get BTC price closest to a timestamp (within 30s)."""
    row = fetch_one(
        conn,
        f"SELECT price FROM {PRICE_TABLE} WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
        (ts - 30, ts + 30, ts),
    )
    return float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Read signal cache
# ---------------------------------------------------------------------------
def _read_signal_cache(conn) -> dict | None:
    """Read signal from signal_cache_btc15 (written by signal_generator_btc15).
    Returns None if signal is stale (>5s old) or missing."""
    row = fetch_one_dict(conn, f"SELECT * FROM {SIGNAL_TABLE} WHERE id = 1")
    if not row:
        return None
    age = time.time() - float(row.get("ts") or 0)
    if age > 5.0:
        return None
    return row


# ---------------------------------------------------------------------------
# Entry logic
# ---------------------------------------------------------------------------
def _check_entry(conn, cached: dict, base_stake: float, sizing_mode: str) -> bool:
    """Check entry conditions and open a trade if conditions are met.
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

    # gate_allow from signal generator
    gate_allow = int(cached.get("gate_allow") or 0)
    if not gate_allow:
        logger.debug("Skip gate_allow=0 ws=%s dir=%s reason=%s",
                      window_start, direction, cached.get("gate_reason", ""))
        return False

    # Guards skipped -- too strict, matches paper_replay parity

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
        logger.debug("Skip expensive ws=%s: ask=%.3f > %.3f", window_start, entry_price, MAX_ENTRY_PRICE)
        return False
    if direction == "DOWN" and entry_price < MIN_ENTRY_PRICE:
        logger.debug("Skip cheap DOWN ws=%s: ask=%.3f < %.3f", window_start, entry_price, MIN_ENTRY_PRICE)
        return False

    # Spread filter
    if up_ask is not None and down_ask is not None:
        spread = abs(float(up_ask) - float(down_ask))
        if spread > MAX_ODDS_SPREAD:
            logger.debug("Skip wide spread ws=%s: %.3f > %.3f", window_start, spread, MAX_ODDS_SPREAD)
            return False

    # BB extreme: |bb_pos| >= threshold
    bb_pos_val = cached.get("bb_pos")
    if bb_pos_val is None:
        logger.debug("Skip no bb_pos ws=%s", window_start)
        return False
    if abs(float(bb_pos_val)) < BB_THRESHOLD:
        logger.debug("Skip BB not extreme ws=%s: bb_pos=%.3f < %.1f",
                      window_start, float(bb_pos_val), BB_THRESHOLD)
        return False

    # VWAP agree
    vwap_val = cached.get("vwap_agree")
    if vwap_val is None or int(vwap_val) == 0:
        logger.debug("Skip VWAP disagree ws=%s dir=%s", window_start, direction)
        return False

    # Ask drift
    drift_val = cached.get("ask_drift")
    if drift_val is not None and float(drift_val) > MAX_ASK_DRIFT:
        logger.debug("Skip ask drift ws=%s: %.3f > %.3f",
                      window_start, float(drift_val), MAX_ASK_DRIFT)
        return False

    # BTC still moving
    bsm_val = cached.get("btc_still_moving")
    if bsm_val is None or int(bsm_val) == 0:
        logger.debug("Skip BTC not still moving ws=%s", window_start)
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
        accel = bool(int(cached.get("btc_accel_ok") or 0)) if cached.get("btc_accel_ok") is not None else False

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
        logger.info("Score OK: %d/%d (btc=%.3f%% conf=%.2f ev=%.2f prev=%s)",
                     score, MIN_ENTRY_SCORE, btc_move, conf, ev, prev)

    # Sizing
    stake = base_stake
    if sizing_mode == "fixed":
        if FIXED_STAKE > 0:
            stake = round(FIXED_STAKE, 2)
        else:
            stake = round(base_stake, 2)

    # Dynamic sizing via quality_score
    qs = cached.get("quality_score")
    if qs is not None:
        qs = float(qs)
        stake = round(stake * qs, 2)
        logger.debug("Dynamic sizing: quality=%.2f stake=$%.2f", qs, stake)

    if stake < 5.0:
        logger.warning("Stake too small ws=%s: $%.2f", window_start, stake)
        return False

    # Compute trade fields
    shares = stake / entry_price
    payout_multiple = 1.0 / entry_price
    potential_win_pnl = apply_fee_to_pnl(shares - stake, stake)
    confidence = float(cached.get("avg_confidence") or 0)
    gate_ev = float(cached.get("gate_ev") or 0)
    opened_at = time.time()
    reason = str(cached.get("gate_reason") or "")

    # INSERT trade
    execute_write(
        conn,
        f"""INSERT INTO {TRADES_TABLE}
           (window_start, window_end, direction, stake, entry_price, payout_multiple,
            shares, potential_win_pnl, signal_confidence, signal_reason,
            initial_capital, risk_fraction, status, opened_at)
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
            confidence,
            reason,
            float(base_stake),
            0.15,
            opened_at,
        ),
    )
    conn.commit()

    logger.warning(
        "OPEN  ws=%s dir=%s stake=$%.2f ask=%.3f ev=%+.3f%% conf=%.2f bb=%.2f drift=%.3f",
        window_start, direction, stake, entry_price,
        gate_ev * 100.0, confidence,
        float(bb_pos_val) if bb_pos_val is not None else 0.0,
        float(drift_val) if drift_val is not None else 0.0,
    )
    _btc_start_open = _btc_price_at(conn, float(window_start))
    _btc_now_open = _btc_price_at(conn, opened_at)
    _now_utc_open = datetime.now(timezone.utc).isoformat()
    _reason_open = str(reason or "").strip().replace("\n", " ")
    if len(_reason_open) > 260:
        _reason_open = f"{_reason_open[:257]}..."
    _tg_send(
        f"[BTC15 PAPER OPEN]\n"
        f"time(UTC): {_now_utc_open}\n"
        f"side: {direction}\n"
        f"slug: {SLUG_PREFIX}\n"
        f"window_start: {window_start}\n"
        f"stake: ${float(stake):,.2f}\n"
        f"entry odds: {float(entry_price):.3f}\n"
        f"Polymarket ask (UP/DOWN): "
        f"{f'{float(up_ask):.3f}' if up_ask else '--'} / "
        f"{f'{float(down_ask):.3f}' if down_ask else '--'}\n"
        f"15m start price: {f'${float(_btc_start_open):,.2f}' if _btc_start_open else '--'}\n"
        f"current BTC: {f'${float(_btc_now_open):,.2f}' if _btc_now_open else '--'}\n"
        f"to-win total: ${float(shares):,.2f}\n"
        f"expected pnl: ${float(potential_win_pnl):,.2f}\n"
        f"confidence: {float(confidence):.3f}\n"
        f"reason: {_reason_open or '--'}"
    )
    return True


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def _resolve_open_trades(conn) -> int:
    """Resolve open trades at window end using BTC price."""
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
        entry_price = float(row.get("entry_price") or 0.5)

        # Must wait until window has ended
        if now_ts < we + 5:
            continue

        # Try market_windows first (Gamma API resolution)
        outcome = None
        mw_row = fetch_one(
            conn,
            "SELECT actual_outcome FROM market_windows WHERE window_start = ? AND slug LIKE ? LIMIT 1",
            (ws, f"{SLUG_PREFIX}%"),
        )
        if mw_row and mw_row[0] in ("UP", "DOWN"):
            outcome = mw_row[0]

        # Fallback: determine from btc_ticks price at window_end vs window_start
        if outcome is None:
            btc_start = _btc_price_at(conn, float(ws))
            btc_end = _btc_price_at(conn, float(we))
            if btc_start is not None and btc_end is not None and btc_start > 0:
                if btc_end > btc_start:
                    outcome = "UP"
                elif btc_end < btc_start:
                    outcome = "DOWN"
                else:
                    outcome = "UP"  # tie goes to UP (Polymarket convention)
                logger.info("Settlement ws=%s: btc_start=$%.2f btc_end=$%.2f -> %s",
                            ws, btc_start, btc_end, outcome)
            else:
                # Not enough price data yet -- skip, try again later
                logger.debug("No BTC price for settlement ws=%s: start=%s end=%s",
                             ws, btc_start, btc_end)
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

        _btc_start = _btc_price_at(conn, float(ws))
        _btc_end = _btc_price_at(conn, float(we))
        _btc_exit = _btc_price_at(conn, now_ts)
        _settle_price = 1.0 if outcome == direction else 0.0
        _now_utc = datetime.now(timezone.utc).isoformat()
        _tg_send(
            f"[BTC15 PAPER CLOSE:SETTLEMENT] {tag.strip()}\n"
            f"time(UTC): {_now_utc}\n"
            f"side: {direction}\n"
            f"slug: {SLUG_PREFIX}\n"
            f"window_start: {ws}\n"
            f"stake: ${float(stake):,.2f}\n"
            f"entry odds: {float(entry_price):.3f}\n"
            f"settlement odds: {float(_settle_price):.3f}\n"
            f"15m start/end(BTC): "
            f"{f'${float(_btc_start):,.2f}' if _btc_start else '--'} / "
            f"{f'${float(_btc_end):,.2f}' if _btc_end else '--'}\n"
            f"BTC at exit: {f'${float(_btc_exit):,.2f}' if _btc_exit else '--'}\n"
            f"outcome: {str(outcome).upper()}\n"
            f"realized pnl: ${float(pnl):,.2f} ({float(roi_pct):+.2f}%)\n"
            f"reason: expiry_settlement"
        )

    return resolved


def _backfill_unresolved_trades(conn) -> int:
    """Re-settle BTC15 trades via Gamma API (corrects wrong outcomes)."""
    now_ts = time.time()
    rows = fetch_all_dicts(
        conn,
        f"""SELECT id, window_start, window_end, direction, stake, shares,
                   actual_outcome, pnl
            FROM {TRADES_TABLE}
            WHERE status = 'CLOSED' AND archived_at IS NULL
              AND closed_at > ?
            ORDER BY window_start ASC
            LIMIT 20""",
        (now_ts - 1800,),
    )
    if not rows:
        return 0

    updated = 0
    for row in rows:
        ws = int(row["window_start"])
        old_outcome = str(row.get("actual_outcome") or "")

        try:
            import httpx
            slug = f"{SLUG_PREFIX}-{ws}"
            # Gamma API V2: /events -> /events/keyset (response: {"events":[...]})
            resp = httpx.get(
                f"https://gamma-api.polymarket.com/events/keyset?slug={slug}&limit=1",
                timeout=5,
            )
            _payload = resp.json()
            events = _payload.get("events", []) if isinstance(_payload, dict) else _payload
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
            continue

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
            f"""UPDATE {TRADES_TABLE}
                SET actual_outcome=?, won=?, pnl=?, roi_pct=?,
                    close_reason='expiry_settlement_corrected'
                WHERE id=?""",
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
            f"[BTC15 PAPER CORRECTED]\n"
            f"window: {ws}\n"
            f"outcome: {old_outcome} -> {new_outcome}\n"
            f"result: {old_label} -> {new_label}\n"
            f"pnl: ${float(row.get('pnl') or 0):+.2f} -> ${pnl:+.2f}\n"
            f"source: Gamma API (ptb=${gamma_ptb:,.2f} final=${gamma_final:,.2f})"
        )

    if updated:
        conn.commit()
        logger.warning("Backfilled %d BTC15 trades from Gamma API", updated)
    return updated


# ---------------------------------------------------------------------------
# Data freshness
# ---------------------------------------------------------------------------
_last_stale_warn_ts: float = 0.0


def _check_data_freshness(conn) -> bool:
    """Return True if BTC tick data is fresh enough."""
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
 BTC 15min PAPER TRADE STATUS
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
                  potential_win_pnl, status, actual_outcome, pnl, roi_pct
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
            print(
                f"  {dt} | {status:6s} | {r['direction']:4s} | "
                f"ask={float(r['entry_price']):.4f} payx={float(r['payout_multiple']):.3f} | "
                f"potWin={float(r['potential_win_pnl']):+.2f} | pnl={pnl_s} roi={roi_s}"
            )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_loop(stake: float, interval_sec: float, sizing_mode: str):
    conn = connect_db()
    _init_tables(conn)

    base_stake = max(5.0, float(stake))
    mode = str(sizing_mode or "fixed").strip().lower()
    if mode not in ("fixed",):
        mode = "fixed"

    logger.warning(
        "BTC15 paper sim running: stake=$%.2f mode=%s interval=%.1fs "
        "entry=%s~%ss max_ask=%.2f bb>=%.1f drift<=%.2f score>=%d",
        base_stake, mode, interval_sec,
        ENTRY_START_SEC, ENTRY_END_SEC,
        MAX_ENTRY_PRICE, BB_THRESHOLD, MAX_ASK_DRIFT, MIN_ENTRY_SCORE,
    )

    try:
        while True:
            try:
                if not _check_data_freshness(conn):
                    time.sleep(interval_sec)
                    continue

                _resolve_open_trades(conn)
                # Backfill every 60s (Gamma API rate limit)
                if not hasattr(run_loop, '_last_backfill'):
                    run_loop._last_backfill = 0.0
                _now_bf = time.time()
                if _now_bf - run_loop._last_backfill >= 60.0:
                    run_loop._last_backfill = _now_bf
                    _backfill_unresolved_trades(conn)

                cached = _read_signal_cache(conn)
                if cached is not None:
                    _check_entry(conn, cached, base_stake, mode)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.exception("Loop error (continue): %s", e)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.warning("BTC15 paper sim stopped by user")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="BTC 15min paper trading simulator")
    parser.add_argument("--stake", type=float, default=100.0,
                        help="Paper stake per trade in USD (default: 100)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Polling interval seconds (default: 0.5)")
    parser.add_argument("--sizing-mode", type=str, default="fixed",
                        help="Sizing mode: fixed (default)")
    parser.add_argument("--status", action="store_true",
                        help="Show paper trade status and exit")
    args = parser.parse_args()

    conn = connect_db()
    _init_tables(conn)
    conn.close()

    if args.status:
        conn = connect_db()
        show_status(conn, float(args.stake))
        conn.close()
        return

    run_loop(
        stake=float(args.stake),
        interval_sec=float(args.interval),
        sizing_mode=str(args.sizing_mode),
    )


if __name__ == "__main__":
    main()
