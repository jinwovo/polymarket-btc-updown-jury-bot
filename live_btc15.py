"""
Live trading bot for BTC 15-minute Up/Down binary options on Polymarket.

Simplified single-threaded async loop:
  1. Reads signal_cache_btc15 for entry signals (written by signal_generator_btc15.py)
  2. Executes FAK orders via polymarket_client.py (Rust binary + Python fallback)
  3. Records trades in live_trades_btc15 table
  4. Settles trades at window end using Polymarket API + btc_ticks fallback

Usage:
    python live_btc15.py              # dry-run mode (default)
    python live_btc15.py --no-dry-run # real money
    python live_btc15.py --dry-run    # explicit dry-run
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

from config import config
from market_config import BTC_15M, env as menv
from db_config import (
    connect_db,
    execute_write,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)
from polymarket_client import PolymarketClient, MarketInfo
from binance_ws import BinancePriceFeed
from trade_gate import apply_fee_to_pnl
from telegram_notifier import send_telegram_message

# ---------------------------------------------------------------------------
# Logging -- bot_live_btc15.log
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_root = logging.getLogger()
_root.setLevel(logging.INFO)

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)

_fh = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_live_btc15.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)

logger = logging.getLogger("live_btc15")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Constants from MarketDef
# ---------------------------------------------------------------------------
MARKET = BTC_15M
INTERVAL = MARKET.interval_seconds          # 900
SLUG_PREFIX = MARKET.slug_prefix            # "btc-updown-15m"
CACHE_TABLE = MARKET.signal_cache_table     # "signal_cache_btc15"
TRADES_TABLE = MARKET.live_trades_table     # "live_trades_btc15"
PRICE_TABLE = MARKET.price_table            # "btc_ticks"

POLL_INTERVAL = 0.5  # seconds -- poll signal_cache every 0.5s


def _env_float(name: str, default: str) -> float:
    return float(menv(MARKET, name, default))


def _env_int(name: str, default: str) -> int:
    return int(menv(MARKET, name, default))


def _env_bool(name: str, default: str) -> bool:
    return menv(MARKET, name, default).lower() == "true"


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _current_window_start(now: float) -> int:
    """Floor timestamp to 900-second boundary."""
    return int(now // INTERVAL) * INTERVAL


def _slug_for_window(ws: int) -> str:
    """Build Polymarket slug for a 15-min window."""
    return "%s-%d" % (SLUG_PREFIX, ws)


# ---------------------------------------------------------------------------
# Live Trader
# ---------------------------------------------------------------------------

class LiveBTC15Trader:
    """Live trading loop for BTC 15-min binary options."""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.db = connect_db()
        init_market_schema(self.db)

        self.poly_client = PolymarketClient()
        self.price_feed = BinancePriceFeed(enable_chainlink=True)

        # Current trade state
        self._current_ws: int = 0
        self._trade: Optional[dict] = None      # active trade info
        self._trade_ws: Optional[int] = None     # window of active trade
        self._gate_lock: Optional[dict] = None   # gate_allow lock (2s)
        self._last_quality_score: Optional[float] = None

        # Settlement verification tasks
        self._verify_tasks: list[asyncio.Task] = []

        logger.warning(
            "LiveBTC15 initialized: dry_run=%s interval=%ds slug=%s",
            self.dry_run, INTERVAL, SLUG_PREFIX,
        )

    # -----------------------------------------------------------------------
    # DB helpers
    # -----------------------------------------------------------------------

    def _ensure_db(self):
        """Reconnect DB if stale."""
        try:
            self.db.ping(reconnect=True)
        except Exception:
            try:
                self.db = connect_db()
            except Exception as e:
                logger.error("DB reconnect failed: %s", e)

    def _read_signal_cache(self) -> Optional[dict]:
        """Read latest row from signal_cache_btc15."""
        try:
            return fetch_one_dict(
                self.db,
                "SELECT * FROM %s WHERE id = 1" % CACHE_TABLE,
            )
        except Exception as e:
            logger.debug("signal_cache read failed: %s", e)
            return None

    # -----------------------------------------------------------------------
    # Market discovery (for 15-min slug)
    # -----------------------------------------------------------------------

    async def _find_market_15m(self, ws: int) -> Optional[MarketInfo]:
        """Find Polymarket market for a 15-min window."""
        slug = _slug_for_window(ws)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    "%s/markets" % config.polymarket.gamma_url,
                    params={"slug": slug, "closed": "false"},
                )
                if resp.status_code != 200:
                    return None
                markets = resp.json()
                if not markets:
                    resp2 = await http.get(
                        "%s/events" % config.polymarket.gamma_url,
                        params={"slug": slug},
                    )
                    if resp2.status_code == 200:
                        events = resp2.json()
                        if events and len(events) > 0:
                            ev = events[0]
                            if "markets" in ev and len(ev["markets"]) > 0:
                                markets = ev["markets"]
                if not markets:
                    return None

                mkt = markets[0] if isinstance(markets, list) else markets
                tokens = mkt.get("tokens", [])
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = []

                up_token = ""
                down_token = ""
                up_price = 0.5
                down_price = 0.5
                for t in (tokens if isinstance(tokens, list) else []):
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

                clob_ids = mkt.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                if not up_token and isinstance(clob_ids, list) and len(clob_ids) >= 2:
                    up_token = str(clob_ids[0])
                    down_token = str(clob_ids[1])

                return MarketInfo(
                    condition_id=mkt.get("condition_id", mkt.get("conditionId", "")),
                    question=mkt.get("question", ""),
                    slug=slug,
                    start_timestamp=ws,
                    end_timestamp=ws + INTERVAL,
                    up_token_id=up_token,
                    down_token_id=down_token,
                    up_price=up_price,
                    down_price=down_price,
                    gamma_up_price=up_price,
                    gamma_down_price=down_price,
                    active=mkt.get("active", True),
                    last_odds_update=time.time(),
                )
        except Exception as e:
            logger.warning("find_market_15m failed for slug=%s: %s", slug, e)
            return None

    # -----------------------------------------------------------------------
    # Settlement: fetch outcome from Polymarket API (15-min slug)
    # -----------------------------------------------------------------------

    async def _fetch_settlement_outcome(self, ws: int) -> Optional[str]:
        """Query Polymarket API for settlement outcome of a 15-min window.
        Returns 'UP', 'DOWN', or None if not yet settled."""
        slug = _slug_for_window(ws)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    "%s/markets" % config.polymarket.gamma_url,
                    params={"slug": slug},
                )
                if resp.status_code != 200:
                    return None
                markets = resp.json()
                if not markets:
                    resp2 = await http.get(
                        "%s/events" % config.polymarket.gamma_url,
                        params={"slug": slug},
                    )
                    if resp2.status_code == 200:
                        events = resp2.json()
                        if events and len(events) > 0:
                            ev = events[0]
                            if "markets" in ev and len(ev["markets"]) > 0:
                                markets = ev["markets"]
                if not markets:
                    return None

                mkt = markets[0] if isinstance(markets, list) else markets

                # outcomePrices: [1,0]=UP, [0,1]=DOWN
                op = mkt.get("outcomePrices", [])
                if isinstance(op, str):
                    try:
                        op = json.loads(op)
                    except Exception:
                        op = []
                if isinstance(op, list) and len(op) >= 2:
                    try:
                        up_px = float(op[0])
                        dn_px = float(op[1])
                    except Exception:
                        up_px, dn_px = 0.5, 0.5
                    if up_px >= 0.95 and dn_px <= 0.05:
                        return "UP"
                    if dn_px >= 0.95 and up_px <= 0.05:
                        return "DOWN"

                # Check tokens for winner
                tokens = mkt.get("tokens", [])
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = []
                for t in (tokens if isinstance(tokens, list) else []):
                    if not isinstance(t, dict):
                        continue
                    if t.get("winner") is True:
                        outcome = str(t.get("outcome", "")).upper()
                        if outcome in ("UP", "DOWN"):
                            return outcome

                return None
        except Exception as e:
            logger.debug("Settlement query failed for %s: %s", slug, e)
            return None

    # -----------------------------------------------------------------------
    # Entry filters (same as paper_sim_btc15 / main.py mirror mode)
    # -----------------------------------------------------------------------

    def _passes_entry_filters(self, sig: dict) -> tuple[bool, str]:
        """Apply entry filters to a signal_cache_btc15 row.
        Returns (passed, reason_if_blocked)."""

        # Daily loss limit check
        _dll = float(os.getenv("BTC15_DAILY_LOSS_LIMIT", "100"))
        if _dll > 0:
            try:
                from datetime import datetime as _dt
                _today = _dt.now().replace(hour=0, minute=0, second=0).timestamp()
                _dll_row = fetch_one(self.db, f"SELECT COALESCE(SUM(pnl), 0) FROM {TRADES_TABLE} WHERE status='CLOSED' AND closed_at >= %s", (_today,))
                _today_pnl = float(_dll_row[0]) if _dll_row else 0.0
                if _today_pnl <= -_dll:
                    now = time.time()
                    if not hasattr(self, '_dll_warned') or now - self._dll_warned > 300:
                        self._dll_warned = now
                        logger.warning("BTC15 daily loss limit reached: $%.2f <= -$%.2f", _today_pnl, _dll)
                    return False, f"daily_loss_limit({_today_pnl:.0f}<=-{_dll:.0f})"
            except Exception:
                pass

        # gate_allow must be 1
        gate_allow = int(sig.get("gate_allow") or 0)
        gate_reason = str(sig.get("gate_reason") or "")

        # Gate lock: once gate_allow=1, hold for 2s (same window)
        now = time.time()
        ws = int(sig.get("window_start") or 0)
        gate_dir = str(sig.get("direction") or "")

        if gate_allow and gate_dir in ("UP", "DOWN"):
            self._gate_lock = {
                "ts": now, "dir": gate_dir, "ws": ws,
                "ev": float(sig.get("gate_ev") or 0),
                "reason": gate_reason,
            }
        elif (self._gate_lock
              and self._gate_lock.get("ws") == ws
              and (now - self._gate_lock["ts"]) < 2.0):
            gate_allow = 1
            gate_dir = self._gate_lock["dir"]
            gate_reason = self._gate_lock.get("reason", "")

        if not gate_allow:
            return False, "gate_allow=0 (%s)" % gate_reason

        # Timing: seconds_elapsed >= ENTRY_START_SEC
        elapsed = float(sig.get("seconds_elapsed") or 0)
        entry_start = _env_float("ENTRY_START_SEC", "80")
        entry_end = _env_float("ENTRY_END_SEC", "800")
        if elapsed < entry_start:
            return False, "too_early: %.0fs < %.0fs" % (elapsed, entry_start)
        if elapsed > entry_end:
            return False, "too_late: %.0fs > %.0fs" % (elapsed, entry_end)

        direction = str(sig.get("direction") or "NO_TRADE")
        if direction not in ("UP", "DOWN"):
            return False, "no_direction"

        # Entry price range
        up_ask = float(sig.get("up_ask") or 0.5)
        dn_ask = float(sig.get("down_ask") or 0.5)
        entry_price = up_ask if direction == "UP" else dn_ask

        max_ask = _env_float("MAX_ENTRY_PRICE", "0.58")
        min_ask = _env_float("DOWN_MIN_ENTRY_PRICE", "0.35")
        if entry_price > max_ask:
            return False, "expensive: ask=%.3f > %.3f" % (entry_price, max_ask)
        if entry_price < min_ask:
            return False, "cheap: ask=%.3f < %.3f" % (entry_price, min_ask)

        # Spread filter
        max_spread = _env_float("MAX_ODDS_SPREAD", "0.20")
        spread = abs(up_ask - dn_ask)
        # Actual spread = max(up_ask, dn_ask) - min(up_ask, dn_ask)
        if max_spread > 0 and spread > max_spread:
            return False, "spread: %.3f > %.3f" % (spread, max_spread)

        # Opposite implied limit
        max_opp = _env_float("MAX_OPPOSITE_IMPLIED", "0.78")
        opp_ask = dn_ask if direction == "UP" else up_ask
        if opp_ask > max_opp:
            return False, "opp_implied: %.3f > %.3f" % (opp_ask, max_opp)

        # BB extreme
        if _env_bool("REQUIRE_BB_EXTREME", "false"):
            bb_pos = sig.get("bb_pos")
            bb_thresh = _env_float("BB_THRESHOLD", "0.5")
            if bb_pos is not None and abs(float(bb_pos)) < bb_thresh:
                return False, "bb_not_extreme: %.3f < %.1f" % (float(bb_pos), bb_thresh)

        # VWAP agree
        if _env_bool("REQUIRE_VWAP_AGREE", "false"):
            vwap_val = sig.get("vwap_agree")
            if vwap_val is not None and int(vwap_val) == 0:
                return False, "vwap_disagree"

        # Ask drift
        max_drift = _env_float("MAX_ASK_DRIFT", "0")
        if max_drift > 0:
            drift_val = sig.get("ask_drift")
            if drift_val is not None and float(drift_val) > max_drift:
                return False, "ask_drift: %.3f > %.3f" % (float(drift_val), max_drift)

        # BTC still moving
        if _env_bool("REQUIRE_BTC_STILL_MOVING", "false"):
            bsm = sig.get("btc_still_moving")
            if bsm is not None and int(bsm) == 0:
                return False, "btc_not_moving"

        # Confidence filter
        min_conf = _env_float("MIN_CONFIDENCE", "0.55")
        conf = float(sig.get("avg_confidence") or 0)
        if conf < min_conf:
            return False, "low_conf: %.3f < %.3f" % (conf, min_conf)

        return True, ""

    # -----------------------------------------------------------------------
    # Bet sizing (FIXED mode with quality_score dynamic sizing)
    # -----------------------------------------------------------------------

    def _compute_bet_size(self, sig: dict) -> float:
        """Compute bet size -- FIXED mode with optional quality_score multiplier."""
        fixed_stake = float(os.getenv("BTC15_FIXED_STAKE",
                            os.getenv("LIVE_FIXED_STAKE",
                            str(config.trading.max_bet_size))))
        if fixed_stake <= 0:
            return 0.0

        base = round(max(float(config.trading.min_bet_size), fixed_stake), 2)

        # Dynamic sizing via quality_score
        if _env_bool("DYNAMIC_SIZING", "false"):
            qs = sig.get("quality_score")
            if qs is not None:
                self._last_quality_score = float(qs)
                base = round(base * float(qs), 2)
                base = max(float(config.trading.min_bet_size), base)
        # Kelly sizing (optional)
        elif os.getenv("BTC15_KELLY_SIZING", os.getenv("LIVE_KELLY_SIZING", "false")).lower() == "true":
            direction = str(sig.get("direction") or "")
            conf = float(sig.get("avg_confidence") or 0)
            btc_move = abs(float(sig.get("btc_move_pct") or 0))
            up_ask = float(sig.get("up_ask") or 0.5)
            dn_ask = float(sig.get("down_ask") or 0.5)
            ep = up_ask if direction == "UP" else dn_ask
            spread_val = abs(up_ask - dn_ask)

            k_score = 0
            if conf >= 0.7:
                k_score += 1
            if btc_move >= 0.03:
                k_score += 1
            if spread_val <= 0.10:
                k_score += 1
            if ep <= 0.48:
                k_score += 1
            if btc_move <= 0.10:
                k_score += 1

            if k_score >= 4:
                base = round(base * 2.0, 2)
            elif k_score >= 3:
                base = round(base * 1.5, 2)
            elif k_score <= 1:
                base = round(base * 0.5, 2)

        return base

    # -----------------------------------------------------------------------
    # Score-based entry filter (same 7 signals as main.py)
    # -----------------------------------------------------------------------

    def _compute_entry_score(self, sig: dict, entry_price: float) -> int:
        """Compute 7-signal entry score."""
        score = 0
        direction = str(sig.get("direction") or "")

        btc_move = abs(float(sig.get("btc_move_pct") or 0))
        conf = float(sig.get("avg_confidence") or 0)
        ev = float(sig.get("gate_ev") or 0)
        prev = str(sig.get("prev_outcome") or "")
        if prev not in ("UP", "DOWN"):
            prev = None
        ov = float(sig.get("odds_velocity") or 0)
        accel_raw = sig.get("btc_accel_ok")
        accel = bool(int(accel_raw)) if accel_raw is not None else False

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

    # -----------------------------------------------------------------------
    # Order execution
    # -----------------------------------------------------------------------

    async def _place_entry(
        self,
        market: MarketInfo,
        direction: str,
        bet_size: float,
        entry_price: float,
    ) -> Optional[dict]:
        """Place FAK entry order via polymarket_client."""
        token_id = market.up_token_id if direction == "UP" else market.down_token_id
        if not token_id:
            logger.warning("No token_id for direction=%s in market %s", direction, market.slug)
            return None

        if self.dry_run:
            logger.info(
                "[DRY-RUN] Would place %s order: dir=%s $%.2f @ %.3f token=%s",
                "FAK", direction, bet_size, entry_price, token_id[:16],
            )
            # Simulate fill
            return {
                "ok": True,
                "mode": "DRY_RUN",
                "filled": True,
                "executed_notional": float(bet_size),
                "executed_price": float(entry_price),
                "executed_size": float(bet_size / entry_price) if entry_price > 0 else 0,
                "order_id": "dry-run-%d" % int(time.time()),
                "status": "matched",
            }

        result = await self.poly_client.place_entry_order(
            token_id=token_id,
            side=direction,
            amount=bet_size,
            reference_ask=float(entry_price),
        )
        return result

    # -----------------------------------------------------------------------
    # DB: trade recording
    # -----------------------------------------------------------------------

    def _record_trade_open(
        self,
        ws: int,
        direction: str,
        stake: float,
        entry_price: float,
        confidence: Optional[float],
        reason: Optional[str],
        source: str,
    ):
        """Insert open trade into live_trades_btc15."""
        try:
            we = ws + INTERVAL
            shares = float(stake / entry_price) if entry_price > 0 else 0
            payout_multiple = float(1.0 / entry_price) if entry_price > 0 else 0
            potential_win = float(shares - stake) if shares > 0 else 0
            opened_at = time.time()

            sql = (
                "INSERT INTO %s "
                "(window_start, window_end, direction, stake, entry_price, "
                "payout_multiple, shares, potential_win_pnl, "
                "signal_confidence, signal_reason, entry_source, "
                "status, opened_at) "
                "VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, 'OPEN', %%s) "
                "ON DUPLICATE KEY UPDATE "
                "direction=VALUES(direction), stake=VALUES(stake), "
                "entry_price=VALUES(entry_price), payout_multiple=VALUES(payout_multiple), "
                "shares=VALUES(shares), potential_win_pnl=VALUES(potential_win_pnl), "
                "signal_confidence=COALESCE(VALUES(signal_confidence), signal_confidence), "
                "signal_reason=COALESCE(VALUES(signal_reason), signal_reason), "
                "entry_source=COALESCE(VALUES(entry_source), entry_source), "
                "status='OPEN', opened_at=VALUES(opened_at), "
                "closed_at=NULL, actual_outcome=NULL, won=NULL, pnl=NULL, roi_pct=NULL"
            ) % TRADES_TABLE

            execute_write(
                self.db, sql,
                (ws, we, direction, stake, entry_price,
                 payout_multiple, shares, potential_win,
                 confidence, reason, source, opened_at),
            )
            self.db.commit()
        except Exception as e:
            logger.warning("Trade OPEN record failed: %s", e)

    def _record_trade_closed(
        self,
        ws: int,
        direction: str,
        stake: float,
        entry_price: float,
        actual_outcome: str,
        close_reason: str,
    ):
        """Update trade to CLOSED in live_trades_btc15."""
        try:
            shares = float(stake / entry_price) if entry_price > 0 else 0
            won = 1 if direction == actual_outcome else 0
            if won:
                raw_pnl = shares - stake
                pnl = apply_fee_to_pnl(raw_pnl, stake)
            else:
                pnl = -stake
            roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0
            closed_at = time.time()

            sql = (
                "UPDATE %s SET "
                "status='CLOSED', closed_at=%%s, actual_outcome=%%s, "
                "won=%%s, pnl=%%s, roi_pct=%%s, close_reason=%%s "
                "WHERE window_start=%%s AND status='OPEN' "
                "ORDER BY id DESC LIMIT 1"
            ) % TRADES_TABLE

            execute_write(
                self.db, sql,
                (closed_at, actual_outcome, won, pnl, roi_pct,
                 close_reason, ws),
            )
            self.db.commit()

            result_str = "WIN" if won else "LOSS"
            logger.warning(
                "Trade CLOSED: ws=%d dir=%s outcome=%s -> %s pnl=$%+.2f",
                ws, direction, actual_outcome, result_str, pnl,
            )
        except Exception as e:
            logger.warning("Trade CLOSED record failed: %s", e)

    def _correct_trade_outcome(
        self,
        ws: int,
        direction: str,
        old_outcome: str,
        new_outcome: str,
    ):
        """Correct settlement outcome in DB after Polymarket API verification."""
        try:
            trade_row = fetch_one_dict(
                self.db,
                "SELECT stake, shares FROM %s WHERE window_start=%%s ORDER BY id DESC LIMIT 1"
                % TRADES_TABLE,
                (ws,),
            )
            if not trade_row:
                return

            stake = float(trade_row.get("stake") or 0)
            shares = float(trade_row.get("shares") or 0)
            won = 1 if direction == new_outcome else 0
            if won:
                raw_pnl = shares - stake
                pnl = apply_fee_to_pnl(raw_pnl, stake)
            else:
                pnl = -stake
            roi_pct = (pnl / stake) * 100.0 if stake > 0 else 0.0

            sql = (
                "UPDATE %s SET "
                "actual_outcome=%%s, won=%%s, pnl=%%s, roi_pct=%%s, "
                "close_reason=CONCAT(COALESCE(close_reason,''), "
                "' [corrected: was ', %%s, ' now ', %%s, ']') "
                "WHERE window_start=%%s AND status='CLOSED' "
                "ORDER BY id DESC LIMIT 1"
            ) % TRADES_TABLE

            execute_write(
                self.db, sql,
                (new_outcome, won, pnl, roi_pct, old_outcome, new_outcome, ws),
            )
            self.db.commit()
            logger.error(
                "SETTLEMENT CORRECTION: ws=%d outcome=%s->%s pnl=$%+.2f",
                ws, old_outcome, new_outcome, pnl,
            )
        except Exception as e:
            logger.warning("Settlement correction failed: %s", e)

    # -----------------------------------------------------------------------
    # Settlement
    # -----------------------------------------------------------------------

    def _determine_outcome_from_ticks(self, ws: int) -> Optional[str]:
        """Determine UP/DOWN from btc_ticks table (Chainlink calibrated)."""
        try:
            start_row = fetch_one(
                self.db,
                "SELECT price FROM %s WHERE ts >= %%s AND ts <= %%s ORDER BY ts ASC LIMIT 1"
                % PRICE_TABLE,
                (ws, ws + 5),
            )
            if not start_row:
                start_row = fetch_one(
                    self.db,
                    "SELECT price FROM %s WHERE ts <= %%s ORDER BY ts DESC LIMIT 1"
                    % PRICE_TABLE,
                    (ws,),
                )
            if not start_row:
                return None
            start_price = float(start_row[0])

            end_ts = float(ws + INTERVAL)
            end_row = fetch_one(
                self.db,
                "SELECT price FROM %s WHERE ts BETWEEN %%s AND %%s ORDER BY ts DESC LIMIT 1"
                % PRICE_TABLE,
                (end_ts - 5, end_ts + 5),
            )
            if not end_row:
                return None
            end_price = float(end_row[0])

            if start_price <= 0:
                return None
            return "UP" if end_price >= start_price else "DOWN"
        except Exception as e:
            logger.debug("Tick outcome check failed: %s", e)
            return None

    async def _settle_trade(self, trade: dict):
        """Settle a completed trade: determine outcome, record, verify."""
        ws = int(trade["window_start"])
        direction = str(trade["direction"])
        stake = float(trade["stake"])
        entry_price = float(trade["entry_price"])

        # PRIMARY: Polymarket API
        actual = None
        try:
            actual = await self._fetch_settlement_outcome(ws)
            if actual:
                logger.info("Settlement from Polymarket API: ws=%d -> %s", ws, actual)
        except Exception as e:
            logger.debug("Polymarket settlement failed: %s", e)

        # FALLBACK 1: CLOB odds near window end
        if actual is None:
            try:
                odds_row = fetch_one_dict(
                    self.db,
                    "SELECT up_best_ask, down_best_ask FROM poly_odds "
                    "WHERE ts BETWEEN %%s AND %%s AND slug LIKE %%s "
                    "ORDER BY ts DESC LIMIT 1",
                    (float(ws + INTERVAL - 5), float(ws + INTERVAL + 5),
                     SLUG_PREFIX + "%"),
                )
                if odds_row:
                    ua = float(odds_row.get("up_best_ask") or 0.5)
                    da = float(odds_row.get("down_best_ask") or 0.5)
                    if ua <= 0.10 and da >= 0.85:
                        actual = "DOWN"
                        logger.info("Settlement from odds: ua=%.3f da=%.3f -> DOWN", ua, da)
                    elif da <= 0.10 and ua >= 0.85:
                        actual = "UP"
                        logger.info("Settlement from odds: ua=%.3f da=%.3f -> UP", ua, da)
            except Exception:
                pass

        # FALLBACK 2: btc_ticks
        if actual is None:
            actual = self._determine_outcome_from_ticks(ws)
            if actual:
                logger.warning(
                    "Settlement fallback to btc_ticks: ws=%d -> %s", ws, actual,
                )

        if actual is None:
            logger.error("Cannot determine outcome for ws=%d -- manual review needed", ws)
            return

        self._record_trade_closed(ws, direction, stake, entry_price, actual, "expiry_settlement")

        # Post-settlement exit: sell winning shares @ 0.99 (maker 0% fee)
        won = (actual == direction)
        if won and not self.dry_run:
            shares = stake / entry_price if entry_price > 0 else 0
            token_id = ""
            _mkt = await self._find_market_15m(ws)
            if _mkt:
                token_id = str(_mkt.up_token_id if direction == "UP" else _mkt.down_token_id)
            if token_id and shares > 0:
                try:
                    logger.info("Post-settlement exit: SELL %.2f shares @ 0.99 (maker 0%%)", shares)
                    _exit_result = await self.poly_client.place_settlement_exit_order(
                        token_id=token_id,
                        shares=shares,
                    )
                    if _exit_result and _exit_result.get("filled"):
                        logger.warning("Post-settlement exit filled! $%.2f", float(_exit_result.get("executed_notional") or 0))
                    else:
                        logger.info("Post-settlement exit: %s", _exit_result.get("status") if _exit_result else "no result")
                except Exception as _exit_err:
                    logger.warning("Post-settlement exit error: %s", _exit_err)

        # Send Telegram notification
        self._send_trade_close_telegram(trade, actual)

        # Schedule verification
        task = asyncio.get_event_loop().create_task(
            self._verify_settlement(ws, direction, actual)
        )
        self._verify_tasks.append(task)

    async def _verify_settlement(self, ws: int, direction: str, initial_outcome: str):
        """Re-check Polymarket after 15/30/60/120s and correct if needed."""
        for delay in (15, 30, 60, 120):
            await asyncio.sleep(delay)
            try:
                poly_outcome = await self._fetch_settlement_outcome(ws)
                if poly_outcome not in ("UP", "DOWN"):
                    continue
                if poly_outcome == initial_outcome:
                    logger.info(
                        "Settlement verified: ws=%d outcome=%s (confirmed at +%ds)",
                        ws, poly_outcome, delay,
                    )
                    return
                # Mismatch -- correct
                logger.error(
                    "SETTLEMENT MISMATCH: ws=%d Polymarket=%s but recorded=%s",
                    ws, poly_outcome, initial_outcome,
                )
                self._correct_trade_outcome(ws, direction, initial_outcome, poly_outcome)

                # Telegram correction notice
                try:
                    won = 1 if direction == poly_outcome else 0
                    tg_token = os.getenv("LIVE_TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
                    tg_chat = os.getenv("LIVE_TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
                    if tg_token and tg_chat:
                        send_telegram_message(
                            token=tg_token, chat_id=tg_chat,
                            text=(
                                "[BTC15 SETTLEMENT CORRECTION]\n"
                                "window: %d\n"
                                "direction: %s\n"
                                "was: %s -> now: %s\n"
                                "result: %s"
                            ) % (ws, direction, initial_outcome, poly_outcome,
                                 "WIN" if won else "LOSS"),
                        )
                except Exception:
                    pass
                return
            except Exception as e:
                logger.debug("Verify settlement attempt failed: %s", e)

        logger.info(
            "Settlement verification: no Polymarket result for ws=%d after all retries",
            ws,
        )

    # -----------------------------------------------------------------------
    # Telegram notifications
    # -----------------------------------------------------------------------

    def _send_trade_open_telegram(self, direction: str, stake: float, price: float, ws: int):
        """Send trade-open notification via Telegram."""
        try:
            tg_token = os.getenv("LIVE_TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
            tg_chat = os.getenv("LIVE_TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
            if not tg_token or not tg_chat:
                return
            dry_tag = " [DRY-RUN]" if self.dry_run else ""
            send_telegram_message(
                token=tg_token, chat_id=tg_chat,
                text=(
                    "[BTC15 TRADE OPEN%s]\n"
                    "direction: %s\n"
                    "stake: $%.2f @ %.3f\n"
                    "window: %d\n"
                    "payout: %.2fx"
                ) % (dry_tag, direction, stake, price, ws,
                     1.0 / price if price > 0 else 0),
            )
        except Exception as e:
            logger.debug("Telegram open notify failed: %s", e)

    def _send_trade_close_telegram(self, trade: dict, actual_outcome: str):
        """Send trade-close notification via Telegram."""
        try:
            tg_token = os.getenv("LIVE_TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
            tg_chat = os.getenv("LIVE_TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
            if not tg_token or not tg_chat:
                return
            direction = str(trade["direction"])
            stake = float(trade["stake"])
            entry_price = float(trade["entry_price"])
            shares = stake / entry_price if entry_price > 0 else 0
            won = direction == actual_outcome
            if won:
                raw_pnl = shares - stake
                pnl = apply_fee_to_pnl(raw_pnl, stake)
            else:
                pnl = -stake
            dry_tag = " [DRY-RUN]" if self.dry_run else ""
            send_telegram_message(
                token=tg_token, chat_id=tg_chat,
                text=(
                    "[BTC15 TRADE CLOSED%s]\n"
                    "direction: %s | outcome: %s -> %s\n"
                    "pnl: $%+.2f | roi: %+.1f%%\n"
                    "window: %d"
                ) % (dry_tag, direction, actual_outcome,
                     "WIN" if won else "LOSS",
                     pnl, (pnl / stake * 100) if stake > 0 else 0,
                     int(trade["window_start"])),
            )
        except Exception as e:
            logger.debug("Telegram close notify failed: %s", e)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    async def run(self):
        """Main async loop: poll signal_cache, enter trades, settle at window end."""

        # Start Binance WebSocket price feed
        price_task = asyncio.create_task(self.price_feed.start())

        logger.warning("LiveBTC15 starting main loop (poll=%.1fs, dry_run=%s)", POLL_INTERVAL, self.dry_run)

        prev_ws = 0  # track window transitions

        try:
            while True:
                try:
                    now = time.time()
                    ws = _current_window_start(now)
                    elapsed = now - float(ws)
                    remaining = float(ws + INTERVAL) - now

                    # Window transition: settle previous trade
                    if ws != prev_ws and prev_ws > 0:
                        if self._trade and self._trade_ws == prev_ws:
                            logger.info("Window changed: settling trade from ws=%d", prev_ws)
                            await self._settle_trade(self._trade)
                            self._trade = None
                            self._trade_ws = None
                        self._gate_lock = None
                        prev_ws = ws
                    elif prev_ws == 0:
                        prev_ws = ws

                    # Already have a trade this window -- skip entry
                    if self._trade and self._trade_ws == ws:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Read signal cache
                    self._ensure_db()
                    sig = self._read_signal_cache()
                    if not sig:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Check signal is for current window
                    sig_ws = int(sig.get("window_start") or 0)
                    if sig_ws != ws:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Check signal freshness (max 5s stale)
                    sig_ts = float(sig.get("ts") or 0)
                    sig_age = now - sig_ts
                    if sig_age > 5.0:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Apply entry filters
                    passed, block_reason = self._passes_entry_filters(sig)
                    if not passed:
                        logger.info("Skip: %s", block_reason)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    direction = str(sig.get("direction") or "")
                    up_ask = float(sig.get("up_ask") or 0.5)
                    dn_ask = float(sig.get("down_ask") or 0.5)
                    entry_price = up_ask if direction == "UP" else dn_ask

                    # Score-based filter
                    min_score = _env_int("MIN_ENTRY_SCORE", str(os.getenv("LIVE_MIN_ENTRY_SCORE", "0")))
                    if min_score > 0:
                        score = self._compute_entry_score(sig, entry_price)
                        logger.info(
                            "Score: %d (min=%d dir=%s ep=%.3f)",
                            score, min_score, direction, entry_price,
                        )
                        if score < min_score:
                            logger.info("Skip low score: %d < %d", score, min_score)
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                    # Bet sizing
                    bet_size = self._compute_bet_size(sig)
                    min_bet = float(os.getenv("BTC15_MIN_BET",
                                   os.getenv("LIVE_MIN_BET", "1.00")))
                    if bet_size < min_bet:
                        logger.info("Skip: bet_size=$%.2f < min=$%.2f", bet_size, min_bet)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Find market on Polymarket
                    market = await self._find_market_15m(ws)
                    if not market:
                        logger.warning("Market not found for ws=%d slug=%s", ws, _slug_for_window(ws))
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Log trade decision
                    conf = float(sig.get("avg_confidence") or 0)
                    gate_ev = float(sig.get("gate_ev") or 0)
                    btc_move = float(sig.get("btc_move_pct") or 0)
                    logger.warning(
                        ">>> TRADE: %s | $%.2f @ %.4f | conf=%.3f | ev=%.3f | btc_move=%.4f%% | ws=%d",
                        direction, bet_size, entry_price, conf, gate_ev, btc_move, ws,
                    )

                    # Place order
                    result = await self._place_entry(market, direction, bet_size, entry_price)

                    if result and result.get("filled"):
                        exec_price = float(result.get("executed_price") or entry_price)
                        exec_notional = float(result.get("executed_notional") or bet_size)
                        if not (0.0 < exec_price < 1.0):
                            exec_price = entry_price

                        signal_reason = str(sig.get("gate_reason") or "")[:200]

                        self._trade = {
                            "window_start": ws,
                            "direction": direction,
                            "stake": exec_notional,
                            "entry_price": exec_price,
                            "confidence": conf,
                            "reason": signal_reason,
                        }
                        self._trade_ws = ws

                        self._record_trade_open(
                            ws=ws,
                            direction=direction,
                            stake=exec_notional,
                            entry_price=exec_price,
                            confidence=conf,
                            reason=signal_reason,
                            source="live_btc15",
                        )
                        self._send_trade_open_telegram(direction, exec_notional, exec_price, ws)
                        logger.warning(
                            "FILLED: %s $%.2f @ %.4f (order_id=%s)",
                            direction, exec_notional, exec_price,
                            str(result.get("order_id", ""))[:32],
                        )
                    else:
                        reason = str((result or {}).get("reason", (result or {}).get("status", "unknown")))
                        logger.info(
                            "Order not filled: dir=%s mode=%s status=%s reason=%s",
                            direction,
                            str((result or {}).get("mode", "")),
                            str((result or {}).get("status", "")),
                            reason[:80],
                        )

                except Exception as e:
                    logger.error("Main loop error: %s", e, exc_info=True)
                    self._ensure_db()

                await asyncio.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.warning("Shutting down LiveBTC15")
        finally:
            # Settle any open trade before exit
            if self._trade:
                logger.warning("Settling open trade on shutdown")
                try:
                    await self._settle_trade(self._trade)
                except Exception as e:
                    logger.error("Shutdown settlement failed: %s", e)

            # Cancel verification tasks
            for task in self._verify_tasks:
                if not task.done():
                    task.cancel()

            # Stop price feed
            self.price_feed._running = False
            price_task.cancel()
            try:
                await price_task
            except (asyncio.CancelledError, Exception):
                pass

            try:
                self.db.close()
            except Exception:
                pass

            logger.warning("LiveBTC15 shutdown complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BTC 15-min live trader")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Dry-run mode -- simulate orders (default: true)",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Real money mode -- place actual orders",
    )
    parser.add_argument("--stake", type=float, default=None,
                        help="Stake per trade (dashboard sends this)")
    parser.add_argument("--sizing-mode", type=str, default="fixed",
                        help="Sizing mode (ignored, uses env)")
    args = parser.parse_args()

    trader = LiveBTC15Trader(dry_run=args.dry_run)
    asyncio.run(trader.run())


if __name__ == "__main__":
    main()
