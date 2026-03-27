"""
Real-time data collector for Polymarket BTC Up/Down 5m markets.

Records BOTH Binance tick prices AND Polymarket real UP/DOWN odds
every second into the database.

Run this for a few days first, THEN backtest against real data.

Usage:
    python data_collector.py                 # start collecting
    python data_collector.py --minutes 15    # collect for 15 minutes then stop
    python data_collector.py --status        # show collection stats
    python data_collector.py --export        # export to CSV
"""
import asyncio
import json
import os
import time
import signal
import logging
import sys
import argparse
from datetime import datetime, timezone
from typing import Optional

import math

import numpy as np
import websockets

from binance_ws import ChainlinkCalibrator
from config import config
from judges import Jury, MarketContext
from db_config import (
    connect_db,
    db_label,
    execute_write,
    executemany_write,
    fetch_all,
    fetch_all_dicts,
    fetch_one,
    init_market_schema,
    upsert_btc_ticks_sql,
    upsert_feature_1s_sql,
    upsert_market_window_sql,
    upsert_poly_odds_sql,
)
from polymarket_client import (
    PolymarketClient,
    MarketInfo,
    compute_market_timestamps,
    market_slug_for_timestamp,
)

_log_fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Console: WARNING+ only (trades, errors, corrections)
_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)
# File: all INFO+
_fh = logging.FileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_collector.log"),
    encoding="utf-8",
)
_fh.setLevel(logging.INFO)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)
logger = logging.getLogger("collector")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = connect_db()
    init_market_schema(conn)
    conn.commit()
    return conn


class DataCollector:
    def __init__(self):
        self.db = init_db()
        self.poly_client = PolymarketClient()
        self.current_market: Optional[MarketInfo] = None
        self.current_window_start: int = 0
        self.btc_price: Optional[float] = None  # raw Binance
        self.btc_price_adjusted: Optional[float] = None  # Chainlink-calibrated
        self._chainlink = ChainlinkCalibrator()
        self.window_start_price: Optional[float] = None
        self._window_start_official: bool = False
        self._ptb_scrape_done: bool = False
        self._running = False
        # Shared Jury -- evaluates signal for both paper and live
        self._jury = Jury(threshold=int(os.getenv("JURY_THRESHOLD", "2")))
        # RTDS WebSocket state
        self._rtds_price: float = 0.0
        self._rtds_updated_at: float = 0.0
        self._rtds_alive: bool = False
        # Trade event tracking (for server console display)
        self._last_paper_trade_id: int = 0
        self._last_live_trade_id: int = 0
        self._last_paper_close_id: int = 0
        self._last_live_close_id: int = 0
        self._parity_alerted_ws: set[int] = set()

        # Ring buffer for recent prices (for diffusion/lag_freshness computation)
        self._recent_prices: list[float] = []
        self._recent_timestamps: list[float] = []
        # Raw Binance prices for Chainlink calibration offset lookup
        self._raw_prices: list[float] = []
        self._raw_timestamps: list[float] = []
        self._RECENT_MAX = 600  # keep ~10 min of 1s data

        # Batch insert buffer
        self._tick_buffer: list[tuple] = []
        self._odds_buffer: list[tuple] = []
        self._feature1s_buffer: list[tuple] = []
        # Flush odds faster than ticks to reduce visible UI latency.
        self._flush_loop_interval = 0.25
        self._tick_flush_interval = 1.0
        self._odds_flush_interval = 0.1
        self._feature1s_flush_interval = 1.0
        self._last_tick_flush = 0.0
        self._last_odds_flush = 0.0
        self._last_feature1s_flush = 0.0
        self._last_feature1s_bucket: Optional[int] = None
        self._stopped = False

    async def start(self):
        self._running = True
        logger.info("=" * 50)
        logger.info("Data Collector Started")
        logger.info(f"DB: {db_label()}")
        logger.info("Recording: BTC ticks + Polymarket odds")
        logger.info("=" * 50)
        # Init trade tracking to avoid replaying old trades on startup
        try:
            r = fetch_one(self.db, "SELECT COALESCE(MAX(id),0) FROM paper_trades WHERE archived_at IS NULL")
            self._last_paper_trade_id = int(r[0]) if r else 0
            self._last_paper_close_id = self._last_paper_trade_id
            for t in ("live_trades", "trades"):
                try:
                    r = fetch_one(self.db, f"SELECT COALESCE(MAX(id),0) FROM {t}")
                    self._last_live_trade_id = int(r[0]) if r else 0
                    self._last_live_close_id = self._last_live_trade_id
                    break
                except Exception:
                    continue
        except Exception:
            pass

        # Run in parallel: Binance WS + CLOB polling + flush + window + Chainlink + RTDS + Playwright fallback
        await asyncio.gather(
            self._binance_ws_loop(),
            self._polymarket_poll_loop(),
            self._flush_loop(),
            self._window_tracker_loop(),
            self._chainlink.poll_loop(
                get_binance_price=lambda: self.btc_price,
                get_binance_price_at=self._get_raw_price_at,
            ),
            self._rtds_price_loop(),              # primary: RTDS WebSocket
            self._polymarket_price_sync_loop(),    # fallback: Playwright (only when RTDS down)
        )

    def _get_raw_price_at(self, ts: float):
        """Look up raw Binance price closest to a given timestamp."""
        if not self._raw_prices:
            return None
        best = None
        best_diff = float("inf")
        for i in range(len(self._raw_timestamps)):
            diff = abs(self._raw_timestamps[i] - ts)
            if diff < best_diff:
                best_diff = diff
                best = self._raw_prices[i]
        # Only use if within 5 seconds
        if best_diff > 5.0:
            return None
        return best

    async def _binance_ws_loop(self):
        """Connect to Binance WebSocket and record every trade."""
        while self._running:
            try:
                async with websockets.connect(config.binance.ws_url) as ws:
                    logger.info("Binance WebSocket connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        ts = float(data["T"]) / 1000.0
                        price = float(data["p"])
                        volume = float(data.get("q", 0))

                        self.btc_price = price
                        self.btc_price_adjusted = self._chainlink.adjust(price)

                        # Store raw Binance prices for Chainlink offset calibration
                        self._raw_prices.append(price)
                        self._raw_timestamps.append(ts)
                        if len(self._raw_prices) > self._RECENT_MAX:
                            self._raw_prices = self._raw_prices[-self._RECENT_MAX:]
                            self._raw_timestamps = self._raw_timestamps[-self._RECENT_MAX:]

                        # Update ring buffer with calibrated price
                        self._recent_prices.append(self.btc_price_adjusted)
                        self._recent_timestamps.append(ts)
                        if len(self._recent_prices) > self._RECENT_MAX:
                            self._recent_prices = self._recent_prices[-self._RECENT_MAX:]
                            self._recent_timestamps = self._recent_timestamps[-self._RECENT_MAX:]

                        # Note: btc_start_price is only set from official Price to Beat (PTB).
                        # Binance fallback removed -- wrong start price causes wrong direction.

                        # Buffer ticks with Chainlink-calibrated price + buy/sell split
                        bucket = round(ts, 1)
                        is_buyer_maker = bool(data.get("m", False))
                        buy_vol = volume if not is_buyer_maker else 0.0
                        sell_vol = volume if is_buyer_maker else 0.0
                        self._tick_buffer.append((bucket, self.btc_price_adjusted, volume, buy_vol, sell_vol))

            except websockets.ConnectionClosed:
                logger.warning("Binance WS disconnected, reconnecting...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Binance WS error: {e}")
                await asyncio.sleep(5)

    async def _polymarket_poll_loop(self):
        """Poll Polymarket orderbook + run Jury every tick."""
        while self._running:
            try:
                if self.current_market and self.current_market.up_token_id:
                    updated = await self.poly_client.refresh_odds(self.current_market)
                    if updated:
                        now = time.time()
                        _ub = self.current_market.up_best_bid
                        _ua = self.current_market.up_best_ask
                        _db = self.current_market.down_best_bid
                        _da = self.current_market.down_best_ask
                        _spread_up = (float(_ua) - float(_ub)) if (_ua and _ub) else None
                        _spread_down = (float(_da) - float(_db)) if (_da and _db) else None
                        _overround = (float(_ua) + float(_da) - 1.0) if (_ua and _da) else None
                        self._odds_buffer.append((
                            now,
                            self.current_window_start,
                            self.current_market.slug,
                            self.current_market.up_price,
                            self.current_market.down_price,
                            _ub, _ua, _db, _da,
                            _spread_up, _spread_down, _overround,
                        ))
                        # -- Shared Jury evaluation --
                        self._evaluate_and_cache_signal(now, _ua, _da, _ub, _db)
                # -- Check for new trades (display on server console) --
                self._check_new_trades()
            except Exception as e:
                logger.debug(f"Odds poll error: {e}")

            await asyncio.sleep(0.1)  # rate limit: 150 req/s, we use ~20

    def _check_new_trades(self):
        """Poll paper_trades and live_trades for new entries, log to server console."""
        try:
            # Paper opens
            rows = fetch_all_dicts(
                self.db,
                "SELECT id, window_start, direction, stake, entry_price FROM paper_trades WHERE id > ? AND archived_at IS NULL ORDER BY id ASC LIMIT 5",
                (self._last_paper_trade_id,),
            )
            for r in rows:
                self._last_paper_trade_id = max(self._last_paper_trade_id, int(r["id"]))
                logger.warning(
                    "[PAPER OPEN] %s $%.2f @ %.3f (ws=%s)",
                    r["direction"], float(r["stake"] or 0), float(r["entry_price"] or 0), r["window_start"],
                )
            # Paper closes
            rows = fetch_all_dicts(
                self.db,
                "SELECT id, window_start, direction, pnl, close_reason FROM paper_trades WHERE id > ? AND pnl IS NOT NULL AND closed_at IS NOT NULL AND archived_at IS NULL ORDER BY id ASC LIMIT 5",
                (self._last_paper_close_id,),
            )
            for r in rows:
                self._last_paper_close_id = max(self._last_paper_close_id, int(r["id"]))
                pnl = float(r["pnl"] or 0)
                tag = "WIN" if pnl > 0 else "LOSS"
                logger.warning(
                    "[PAPER %s] %s $%+.2f (%s) ws=%s",
                    tag, r["direction"], pnl, r.get("close_reason", ""), r["window_start"],
                )
            # Live opens/closes
            for table in ("live_trades", "trades"):
                try:
                    rows = fetch_all_dicts(
                        self.db,
                        f"SELECT id, window_start, direction, stake, entry_price, pnl, status FROM {table} WHERE id > ? ORDER BY id ASC LIMIT 5",
                        (self._last_live_trade_id,),
                    )
                except Exception:
                    continue
                for r in rows:
                    self._last_live_trade_id = max(self._last_live_trade_id, int(r["id"]))
                    status = str(r.get("status", ""))
                    pnl = float(r.get("pnl") or 0)
                    if status == "CLOSED":
                        tag = "WIN" if pnl > 0 else "LOSS"
                        logger.warning(
                            "[LIVE %s] %s $%+.2f ws=%s",
                            tag, r["direction"], pnl, r["window_start"],
                        )
                    else:
                        logger.warning(
                            "[LIVE OPEN] %s $%.2f @ %.3f ws=%s",
                            r["direction"], float(r.get("stake") or 0), float(r.get("entry_price") or 0), r["window_start"],
                        )
                break  # only check first existing table
            self._check_parity_mismatch()
        except Exception as e:
            logger.debug("Trade check error: %s", e)

    def _check_parity_mismatch(self):
        """If Paper opened but Live didn't within 45s of Paper's entry (or vice versa), alert."""
        try:
            now = time.time()

            # Recent paper trades with opened_at
            paper_trades = fetch_all_dicts(
                self.db,
                "SELECT window_start, opened_at FROM paper_trades WHERE opened_at > %s AND archived_at IS NULL ORDER BY opened_at DESC LIMIT 5",
                (now - 300,),
            )
            # Recent live trades
            live_trades = []
            for table in ("live_trades", "trades"):
                try:
                    live_trades = fetch_all_dicts(
                        self.db,
                        f"SELECT window_start, opened_at FROM {table} WHERE opened_at > %s ORDER BY opened_at DESC LIMIT 5",
                        (now - 300,),
                    )
                    break
                except Exception:
                    continue

            # Need both to have recent activity
            if not paper_trades and not live_trades:
                return

            paper_ws = {int(r["window_start"]) for r in paper_trades}
            live_ws = {int(r["window_start"]) for r in live_trades}

            # Paper opened but Live didn't -- check 45s after Paper's opened_at
            for r in paper_trades:
                ws = int(r["window_start"])
                age = now - float(r["opened_at"])
                if ws not in live_ws and age >= 45 and ws not in self._parity_alerted_ws:
                    # Verify Live has traded at least once recently (process is running)
                    # Check if Live process is running (bot.log updated in last 30s)
                    try:
                        _bot_log = os.path.join(os.path.dirname(__file__), "bot.log")
                        _live_active = (time.time() - os.path.getmtime(_bot_log)) < 30 if os.path.exists(_bot_log) else False
                    except Exception:
                        _live_active = False
                    if not _live_active:
                        continue
                    self._parity_alerted_ws.add(ws)
                    msg = f"[!] PARITY MISMATCH\nPaper OPEN but Live missing\nws={ws} (age={age:.0f}s after Paper entry)"
                    logger.warning(msg.replace("\n", " | "))
                    self._send_parity_telegram(msg)

            # Live opened but Paper didn't -- check 45s after Live's opened_at
            for r in live_trades:
                ws = int(r["window_start"])
                age = now - float(r["opened_at"])
                if ws not in paper_ws and age >= 45 and ws not in self._parity_alerted_ws:
                    # Check if Paper process is running (bot_paper.log updated in last 30s)
                    try:
                        _paper_log = os.path.join(os.path.dirname(__file__), "bot_paper.log")
                        _paper_active = (time.time() - os.path.getmtime(_paper_log)) < 30 if os.path.exists(_paper_log) else False
                    except Exception:
                        _paper_active = False
                    if not _paper_active:
                        continue
                    self._parity_alerted_ws.add(ws)
                    msg = f"[!] PARITY MISMATCH\nLive OPEN but Paper missing\nws={ws} (age={age:.0f}s after Live entry)"
                    logger.warning(msg.replace("\n", " | "))
                    self._send_parity_telegram(msg)
        except Exception as e:
            logger.debug("Parity check error: %s", e)

    def _send_parity_telegram(self, msg: str):
        try:
            from telegram_notifier import send_telegram_message
            tg_token = str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
            tg_chat = str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
            if tg_token and tg_chat:
                send_telegram_message(token=tg_token, chat_id=tg_chat, text=msg)
        except Exception:
            pass

    def _evaluate_and_cache_signal(self, now: float, ua, da, ub, db):
        """Run Jury with current data, write signal to DB for paper/live to read."""
        try:
            if not self.window_start_price or self.window_start_price <= 0:
                return
            if not self.btc_price_adjusted or self.btc_price_adjusted <= 0:
                return
            if len(self._recent_prices) < 20:
                return

            interval = int(config.polymarket.interval_seconds)
            elapsed = max(0.0, now - float(self.current_window_start))
            remaining = max(0.0, float(self.current_window_start + interval) - now)

            up_ask = float(ua) if ua and 0 < float(ua) < 1 else None
            dn_ask = float(da) if da and 0 < float(da) < 1 else None
            up_bid = float(ub) if ub and 0 < float(ub) < 1 else None
            dn_bid = float(db) if db and 0 < float(db) < 1 else None

            ctx = MarketContext(
                current_binance_price=self.btc_price_adjusted,
                market_start_price=self.window_start_price,
                recent_prices=list(self._recent_prices[-600:]),
                recent_timestamps=list(self._recent_timestamps[-600:]),
                poly_up_price=self.current_market.up_price if self.current_market else 0.5,
                poly_down_price=self.current_market.down_price if self.current_market else 0.5,
                seconds_elapsed=elapsed,
                seconds_remaining=remaining,
                poly_up_bid=up_bid,
                poly_up_ask=up_ask,
                poly_down_bid=dn_bid,
                poly_down_ask=dn_ask,
            )

            # Buy/sell volume ratio (last 60s)
            _bs_ratio = None
            try:
                _vol_row = fetch_one(
                    self.db,
                    "SELECT SUM(buy_volume), SUM(sell_volume) FROM btc_ticks WHERE ts > ?",
                    (now - 60,),
                )
                if _vol_row and _vol_row[0] is not None and _vol_row[1] is not None:
                    _bv = float(_vol_row[0])
                    _sv = float(_vol_row[1])
                    if _sv > 0:
                        _bs_ratio = _bv / _sv
                    elif _bv > 0:
                        _bs_ratio = 10.0
                ctx.buy_sell_ratio = _bs_ratio
            except Exception:
                pass

            decision = self._jury.deliberate(ctx)

            # -- Price guards (shared module -- same logic as backtest) --
            btc_move_pct = 0.0
            recent_move_pct = None
            trend_move_pct = None
            guards_passed = 0

            if decision.direction in ("UP", "DOWN") and self.window_start_price and self.window_start_price > 0:
                btc_move_pct = ((self.btc_price_adjusted - self.window_start_price) / self.window_start_price) * 100.0

                from entry_guards import evaluate_market_guards
                _guard = evaluate_market_guards(
                    direction=decision.direction,
                    btc_price=self.btc_price_adjusted,
                    start_price=self.window_start_price,
                    up_ask=float(self.current_market.up_best_ask) if self.current_market and self.current_market.up_best_ask else None,
                    down_ask=float(self.current_market.down_best_ask) if self.current_market and self.current_market.down_best_ask else None,
                    elapsed=elapsed,
                    db_conn=self.db,
                    window_start=self.current_window_start,
                    now_ts=now,
                )
                guards_passed = 1 if _guard.passed else 0

            buy_sell_ratio = _bs_ratio

            # -- Direction stability: once gate_allow=1 for a direction, hold 5s --
            _now_ts = time.time()
            _stable_dir = getattr(self, '_stable_direction', '')
            _stable_until = getattr(self, '_stable_until', 0.0)
            _stable_ws = getattr(self, '_stable_ws', 0)
            if _stable_ws == int(self.current_window_start) and _now_ts < _stable_until:
                if decision.direction != _stable_dir and decision.direction in ("UP", "DOWN"):
                    decision = type(decision)(
                        final_vote=decision.final_vote,
                        direction=_stable_dir,
                        avg_confidence=decision.avg_confidence,
                        max_edge=decision.max_edge,
                        verdicts=decision.verdicts,
                        unanimous=decision.unanimous,
                    )

            # -- Entry gate (same check as Paper/Live) --
            gate_allow = 0
            gate_ev = None
            gate_reason = None
            if decision.direction in ("UP", "DOWN") and guards_passed:
                try:
                    from trade_gate import evaluate_entry_gate
                    _entry_price = float(dn_ask if decision.direction == "DOWN" else up_ask) if (up_ask and dn_ask) else 0.5
                    # Price range filter: reject extreme odds (market 70%+ confident other way)
                    _down_min = float(os.getenv("PAPER_DOWN_MIN_ENTRY_PRICE", "0.30"))
                    _max_ask = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.70"))
                    if _entry_price < _down_min:
                        gate_reason = f"cheap_token: ask={_entry_price:.3f} < {_down_min:.3f}"
                        raise ValueError(gate_reason)
                    if _entry_price > _max_ask:
                        gate_reason = f"expensive_entry: ask={_entry_price:.3f} > {_max_ask:.3f}"
                        raise ValueError(gate_reason)
                    _support = sum(1 for v in decision.verdicts if v.vote.value == decision.direction)
                    _support_ratio = _support / max(len(decision.verdicts), 1)
                    _gate = evaluate_entry_gate(
                        direction=decision.direction,
                        entry_price=_entry_price,
                        current_price=self.btc_price_adjusted or 0,
                        start_price=self.window_start_price or 0,
                        seconds_elapsed=elapsed,
                        jury_confidence=float(decision.avg_confidence),
                        support_ratio=float(_support_ratio),
                        seconds_remaining=remaining,
                        recent_prices=list(self._recent_prices[-600:]),
                        recent_timestamps=list(self._recent_timestamps[-600:]),
                        poly_up_ask=up_ask,
                        poly_down_ask=dn_ask,
                    )
                    gate_allow = 1 if _gate.allow else 0
                    gate_ev = float(_gate.expected_roi) if _gate.expected_roi else None
                    gate_reason = str(_gate.reason or "")[:200]
                    # Lock direction for 5s when gate passes
                    if gate_allow:
                        self._stable_direction = decision.direction
                        self._stable_until = _now_ts + 5.0
                        self._stable_ws = int(self.current_window_start)
                except Exception as _ge:
                    logger.debug("Entry gate check failed: %s", _ge)

            # Write to signal_cache table (single row, always overwritten)
            # Binance-RTDS gap: positive = Binance above Chainlink = BTC rising
            binance_rtds_gap = None
            if self.btc_price and self._rtds_price and self._rtds_price > 0:
                binance_rtds_gap = float(self.btc_price) - float(self._rtds_price)

            import json as _json
            judges_json = _json.dumps([
                {"judge": v.judge_name, "vote": v.vote.value,
                 "confidence": v.confidence, "reason": v.reason}
                for v in decision.verdicts
            ])
            execute_write(
                self.db,
                """REPLACE INTO signal_cache
                   (id, ts, window_start, direction, avg_confidence, max_edge,
                    unanimous, judges_json, up_ask, down_ask, btc_price, start_price,
                    seconds_elapsed, seconds_remaining,
                    btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                    buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now, self.current_window_start, decision.direction,
                    float(decision.avg_confidence), float(decision.max_edge),
                    1 if decision.unanimous else 0, judges_json,
                    up_ask, dn_ask, self.btc_price_adjusted, self.window_start_price,
                    elapsed, remaining,
                    btc_move_pct, recent_move_pct, trend_move_pct, guards_passed,
                    buy_sell_ratio, gate_allow, gate_ev, gate_reason, binance_rtds_gap,
                ),
            )
            self.db.commit()
        except Exception as e:
            logger.warning("Signal cache update failed: %s", e)

    async def _window_tracker_loop(self):
        """Track 5-minute windows and record start/end prices."""
        while self._running:
            try:
                now = time.time()
                ts = compute_market_timestamps(now)
                window_start = ts["current"]["start"]

                if window_start != self.current_window_start:
                    # New window! Record end of previous window
                    if self.current_window_start > 0 and self.btc_price_adjusted is not None:
                        self._finalize_window(self.current_window_start, self.btc_price_adjusted)

                    # Start tracking new window -- chainlink_adj immediately, scrape at +3s
                    self.current_window_start = window_start
                    self.window_start_price = None
                    self._window_start_official = False
                    self._window_start_source = "none"
                    self._ptb_scrape_done = False

                    # Find the Polymarket market
                    self.current_market = await self.poly_client.find_market(window_start)

                    if self.current_market:
                        if (
                            self.current_market.price_to_beat is not None
                            and float(self.current_market.price_to_beat) > 0.0
                        ):
                            self.window_start_price = float(self.current_market.price_to_beat)
                            self._window_start_official = True
                            self._window_start_source = "ptb_api"
                            self._ptb_scrape_done = True
                        elif (
                            self._chainlink.is_calibrated
                            and self.btc_price is not None
                        ):
                            # Immediate fallback -- scrape will correct in ~3s
                            self.window_start_price = self._chainlink.adjust(self.btc_price)
                            self._window_start_official = True
                            self._window_start_source = "chainlink_adj"
                        self._record_window_start(
                            window_start,
                            window_start + config.polymarket.interval_seconds,
                            self.current_market,
                        )
                        btc_str = f"${self.btc_price_adjusted:,.2f}" if self.btc_price_adjusted is not None else "N/A"
                        start_str = (
                            f"${self.window_start_price:,.2f} ({self._window_start_source})"
                            if self.window_start_price is not None
                            else "N/A"
                        )
                        up_str = (
                            f"{self.current_market.up_price:.3f}"
                            if self.current_market.up_price is not None
                            else "N/A"
                        )
                        down_str = (
                            f"{self.current_market.down_price:.3f}"
                            if self.current_market.down_price is not None
                            else "N/A"
                        )
                        logger.info(
                            f"Window: {self.current_market.slug} | "
                            f"BTC={btc_str} | Start={start_str} | "
                            f"UP={up_str} DOWN={down_str}"
                        )
                    else:
                        logger.warning(f"Market not found for ts={window_start}")

                await self._maybe_sync_window_start_from_price_to_beat(now)

                # 1-second feature snapshots for model training.
                self._collect_feature_1s(now)

            except Exception as e:
                logger.error(f"Window tracker error: {e}")

            await asyncio.sleep(1.0)

    async def _maybe_sync_window_start_from_price_to_beat(self, now_ts: float):
        """Scrape exact PTB ~3s after window start, correct chainlink_adj estimate."""
        if self.current_window_start <= 0 or self.current_market is None:
            return
        elapsed = now_ts - float(self.current_window_start)

        # --- Phase 1: if no price at all, set chainlink_adj immediately ---
        if not self._window_start_official or self.window_start_price is None:
            if self._chainlink.is_calibrated and self.btc_price is not None:
                adj_px = self._chainlink.adjust(self.btc_price)
                self.window_start_price = adj_px
                self._window_start_official = True
                self._window_start_source = "chainlink_adj"
                try:
                    execute_write(
                        self.db,
                        """UPDATE market_windows
                           SET btc_start_price = ?
                           WHERE window_start = ?""",
                        (adj_px, self.current_window_start),
                    )
                    self.db.commit()
                except Exception:
                    pass
                logger.info(
                    "Window start set from calibrated Binance: %s | $%.2f",
                    self.current_market.slug, adj_px,
                )
            elif elapsed > 5.0:
                logger.warning(
                    "No start price for %s (elapsed=%.0fs)",
                    self.current_market.slug, elapsed,
                )
            return

        # --- Phase 2: scrape exact PTB + Current price at ~3s (one-shot) ---
        if self._ptb_scrape_done:
            return
        if elapsed < 3.0:
            return
        self._ptb_scrape_done = True

        scraped_ptb, scraped_current = await self.poly_client.scrape_prices(
            self.current_market.slug
        )

        # --- Calibrate offset using Polymarket Current price ---
        if scraped_current is not None and scraped_current > 0:
            binance_now = self.btc_price
            if binance_now is not None and binance_now > 0:
                new_offset = binance_now - scraped_current
                old_offset = self._chainlink.offset if self._chainlink else 0
                if self._chainlink is not None:
                    self._chainlink.offset = new_offset
                    self._chainlink.chainlink_price = scraped_current
                    self._chainlink.binance_at_update = binance_now
                logger.info(
                    "Calibration updated from Polymarket scrape: "
                    "poly_current=$%.2f binance=$%.2f new_offset=$%.2f (was $%.2f)",
                    scraped_current, binance_now, new_offset, old_offset,
                )

        if scraped_ptb is None or scraped_ptb <= 0:
            logger.warning("PTB scrape returned None for %s, keeping %s ($%.2f)",
                           self.current_market.slug, self._window_start_source,
                           self.window_start_price or 0)
            return

        prev = self.window_start_price
        prev_src = self._window_start_source
        self.window_start_price = scraped_ptb
        self._window_start_source = "ptb_scrape"
        delta = abs(scraped_ptb - prev) if prev else 0
        try:
            execute_write(
                self.db,
                """UPDATE market_windows
                   SET btc_start_price = ?
                   WHERE window_start = ?""",
                (scraped_ptb, self.current_window_start),
            )
            self.db.commit()
        except Exception:
            pass
        logger.info(
            "Window start corrected by scrape: %s | $%.2f -> $%.2f (delta=$%.2f, was %s)",
            self.current_market.slug, prev or 0, scraped_ptb, delta, prev_src,
        )

    async def _rtds_price_loop(self):
        """Stream Chainlink BTC/USD from RTDS WebSocket (~1s updates).
        Auto-reconnect on disconnect. Primary price source."""
        import websockets as _ws
        _last_log = 0.0
        _reconnect_delay = 1.0

        while self._running:
            try:
                async with _ws.connect("wss://ws-live-data.polymarket.com", ping_interval=None, close_timeout=10, open_timeout=10) as ws:
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}'
                        }]
                    }))
                    logger.warning("RTDS Chainlink feed connected (streaming)")
                    _reconnect_delay = 1.0
                    self._rtds_alive = True

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            await ws.send("PING")
                            continue
                        except Exception:
                            break

                        if not msg or msg == "PONG":
                            continue
                        try:
                            data = json.loads(msg)
                        except Exception:
                            continue

                        payload = data.get("payload", {})
                        chainlink_price = None
                        if isinstance(payload, dict):
                            if "value" in payload:
                                chainlink_price = float(payload["value"])
                            elif "data" in payload and isinstance(payload["data"], list):
                                for d in payload["data"]:
                                    if "value" in d:
                                        chainlink_price = float(d["value"])

                        if not chainlink_price or chainlink_price < 10000:
                            continue

                        now = time.time()
                        self._rtds_price = chainlink_price
                        self._rtds_updated_at = now

                        binance_now = self.btc_price
                        if binance_now and binance_now > 0:
                            new_offset = binance_now - chainlink_price
                            old_offset = self._chainlink.offset
                            self._chainlink.offset = new_offset
                            self._chainlink.chainlink_price = chainlink_price
                            self._chainlink.binance_at_update = binance_now
                            self._chainlink.chainlink_updated_at = now
                            self._chainlink.polymarket_sync_active = True
                            self.btc_price_adjusted = chainlink_price

                            offset_delta = abs(new_offset - old_offset)
                            if offset_delta > 5.0 or (now - _last_log) > 60.0:
                                logger.info(
                                    "RTDS: $%.2f (binance=$%.2f offset=$%.2f)",
                                    chainlink_price, binance_now, new_offset,
                                )
                                _last_log = now

            except Exception as e:
                self._rtds_alive = False
                logger.warning("RTDS error: %s (reconnect in %.0fs)", e, _reconnect_delay)
                await asyncio.sleep(_reconnect_delay)
                _reconnect_delay = min(_reconnect_delay * 2, 30.0)

    async def _polymarket_price_sync_loop(self):
        """Playwright: fallback when RTDS down + periodic price comparison.
        Also handles Playwright crash recovery."""
        _last_log = 0.0
        _last_compare = 0.0
        _pw_consecutive_fail = 0
        await asyncio.sleep(15.0)  # let RTDS connect first
        while self._running:
            try:
                now = time.time()
                rtds_age = now - self._rtds_updated_at
                rtds_ok = rtds_age < 5.0

                # Always try to extract Playwright price (for comparison + fallback)
                poly_price = await self.poly_client.extract_current_price()

                if poly_price is not None and poly_price > 0:
                    _pw_consecutive_fail = 0

                    if rtds_ok:
                        # RTDS alive -- only log if diff > $10 (something wrong)
                        diff = abs(self._rtds_price - poly_price)
                        if diff > 10.0:
                            logger.warning(
                                "Price MISMATCH: RTDS=$%.2f Playwright=$%.2f diff=$%.2f",
                                self._rtds_price, poly_price, diff,
                            )
                    else:
                        # RTDS down -- use Playwright as primary
                        binance_now = self.btc_price
                        if binance_now is not None and binance_now > 0:
                            new_offset = binance_now - poly_price
                            self._chainlink.offset = new_offset
                            self._chainlink.chainlink_price = poly_price
                            self._chainlink.binance_at_update = binance_now
                            self._chainlink.chainlink_updated_at = now
                            self._chainlink.polymarket_sync_active = True
                            self.btc_price_adjusted = poly_price

                        if (now - _last_log) > 15.0:
                            logger.warning(
                                "Playwright active (RTDS down %.0fs): $%.2f",
                                rtds_age, poly_price,
                            )
                            _last_log = now
                else:
                    _pw_consecutive_fail += 1
                    # Auto-recover Playwright after 10 consecutive failures
                    if _pw_consecutive_fail >= 10:
                        logger.warning("Playwright: %d fails, restarting browser", _pw_consecutive_fail)
                        _pw_consecutive_fail = 0
                        try:
                            self.poly_client.close_scraper()
                        except Exception:
                            pass
                        await asyncio.sleep(3.0)

            except Exception as e:
                logger.debug("Playwright sync error: %s", e)
            await asyncio.sleep(3.0 if rtds_ok else 1.0)  # slower when RTDS is fine

    # ------------------------------------------------------------------
    # Inline diffusion / lag helpers (mirror judges.py logic)
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_sigma(prices: list[float], timestamps: list[float]) -> float | None:
        """Estimate annualised volatility (sigma) from recent tick log-returns."""
        n = min(len(prices), len(timestamps))
        if n < 24:
            return None
        p = np.asarray(prices[:n], dtype=float)
        t = np.asarray(timestamps[:n], dtype=float)
        if np.any(p <= 0):
            return None
        logp = np.log(p)
        dlog = np.diff(logp)
        dt = np.diff(t)
        valid = dt > 1e-6
        if not np.any(valid):
            return None
        dlog = dlog[valid]
        dt = dt[valid]
        if len(dlog) < 12:
            return None
        total_dt = float(np.sum(dt))
        if total_dt <= 1e-6:
            return None
        mu = float(np.sum(dlog) / total_dt)
        resid = dlog - (mu * dt)
        var = float(np.sum((resid ** 2) / np.maximum(dt, 1e-6)) / len(resid))
        return math.sqrt(max(var, 1e-12))

    @staticmethod
    def _diffusion_p_up(
        start_price: float,
        current_price: float,
        seconds_remaining: float,
        sigma: float,
    ) -> float | None:
        """P(UP at close) via diffusion model N(x / (sigma*sqrt(t)))."""
        if start_price <= 0 or current_price <= 0 or sigma <= 1e-10:
            return None
        t = max(1.0, seconds_remaining)
        x = math.log(current_price / start_price)
        denom = sigma * math.sqrt(t)
        if denom < 1e-8:
            return 1.0 if x > 0 else 0.0
        z = max(-8.0, min(8.0, x / denom))
        # Phi(z) via math.erfc
        return 0.5 * math.erfc(-z / math.sqrt(2.0))

    @staticmethod
    def _compute_lag_freshness(
        prices: list[float],
        timestamps: list[float],
        start_price: float,
    ) -> float | None:
        """Lag freshness 0-1: how recently the BTC move happened."""
        n = min(len(prices), len(timestamps))
        if n < 15 or start_price <= 0:
            return None
        current = float(prices[n - 1])
        latest_ts = float(timestamps[n - 1])

        def _price_ago(sec: float) -> float | None:
            target = latest_ts - max(1.0, sec)
            for i in range(n - 1, -1, -1):
                if float(timestamps[i]) <= target:
                    return float(prices[i])
            return float(prices[0])

        p5 = _price_ago(5.0)
        p10 = _price_ago(10.0)
        p30 = _price_ago(30.0)
        move_5s = abs(current - p5) / start_price if p5 else 0.0
        move_10s = abs(current - p10) / start_price if p10 else 0.0
        move_30s = abs(current - p30) / start_price if p30 else 0.0
        if move_30s < 1e-8:
            return 0.5
        recency_ratio = max(move_5s, move_10s * 0.8) / max(move_30s, 1e-8)
        return max(0.0, min(1.0, recency_ratio * 1.4))

    def _collect_feature_1s(self, now_ts: float):
        if self.current_window_start <= 0:
            return

        ts_sec = int(now_ts)
        if self._last_feature1s_bucket == ts_sec:
            return

        window_end = self.current_window_start + config.polymarket.interval_seconds
        seconds_elapsed = max(0.0, now_ts - float(self.current_window_start))
        seconds_remaining = max(0.0, float(window_end) - now_ts)

        start_price = self.window_start_price
        if start_price is None:
            row = fetch_one(
                self.db,
                "SELECT btc_start_price FROM market_windows WHERE window_start = ?",
                (self.current_window_start,),
            )
            if row and row[0] is not None:
                start_price = float(row[0])
                self.window_start_price = start_price

        btc_price = self.btc_price_adjusted

        slug = (
            self.current_market.slug
            if self.current_market and self.current_market.slug
            else market_slug_for_timestamp(self.current_window_start)
        )
        up_ask = (
            float(self.current_market.up_best_ask)
            if self.current_market and self.current_market.up_best_ask is not None
            else None
        )
        down_ask = (
            float(self.current_market.down_best_ask)
            if self.current_market and self.current_market.down_best_ask is not None
            else None
        )
        up_mid = (
            float(self.current_market.up_price)
            if self.current_market and self.current_market.up_price is not None
            else None
        )
        down_mid = (
            float(self.current_market.down_price)
            if self.current_market and self.current_market.down_price is not None
            else None
        )

        # Keep feature table clean for training: require full core fields.
        if start_price is None or btc_price is None or up_ask is None or down_ask is None:
            return
        if start_price <= 0:
            return

        btc_move_pct = ((btc_price - start_price) / start_price) * 100.0
        self._last_feature1s_bucket = ts_sec

        # --- Enrichment: bid, spread, overround, diffusion, lag ---
        up_bid = (
            float(self.current_market.up_best_bid)
            if self.current_market and self.current_market.up_best_bid is not None
            else None
        )
        down_bid = (
            float(self.current_market.down_best_bid)
            if self.current_market and self.current_market.down_best_bid is not None
            else None
        )
        spread_up = (up_ask - up_bid) if (up_ask is not None and up_bid is not None) else None
        spread_down = (down_ask - down_bid) if (down_ask is not None and down_bid is not None) else None
        overround = (up_ask + down_ask - 1.0) if (up_ask is not None and down_ask is not None) else None

        sigma_est = self._estimate_sigma(self._recent_prices, self._recent_timestamps)
        p_up_diffusion = (
            self._diffusion_p_up(start_price, btc_price, seconds_remaining, sigma_est)
            if sigma_est is not None
            else None
        )
        lag_freshness = self._compute_lag_freshness(
            self._recent_prices, self._recent_timestamps, start_price,
        )

        self._feature1s_buffer.append(
            (
                ts_sec,
                now_ts,
                self.current_window_start,
                slug,
                seconds_elapsed,
                seconds_remaining,
                float(start_price),
                float(btc_price),
                float(btc_move_pct),
                up_ask,
                down_ask,
                up_mid,
                down_mid,
                # --- new enrichment columns (7 cols) ---
                up_bid,
                down_bid,
                spread_up,
                spread_down,
                overround,
                sigma_est,
                p_up_diffusion,
                lag_freshness,
            )
        )

    def _record_window_start(self, start: int, end: int, market: MarketInfo):
        try:
            execute_write(
                self.db,
                upsert_market_window_sql(),
                (start, end, market.slug, self.window_start_price,
                 market.condition_id, market.up_token_id, market.down_token_id),
            )
            self.db.commit()
        except Exception as e:
            logger.error(f"DB write error: {e}")

    def _finalize_window(self, window_start: int, end_price: float):
        try:
            # Get start price from DB (should be official PTB)
            row = fetch_one(
                self.db,
                "SELECT btc_start_price FROM market_windows WHERE window_start = ?",
                (window_start,),
            )

            if row and row[0] is not None:
                start_price = row[0]
                outcome = "UP" if end_price >= start_price else "DOWN"
            else:
                logger.warning("Window %s finalized without PTB start price -- outcome UNKNOWN", window_start)
                outcome = "UNKNOWN"

            execute_write(
                self.db,
                """UPDATE market_windows
                   SET btc_end_price = ?, actual_outcome = ?
                   WHERE window_start = ?""",
                (end_price, outcome, window_start),
            )
            self.db.commit()

            if outcome != "UNKNOWN":
                change_pct = ((end_price - start_price) / start_price) * 100
                logger.info(
                    f"Window ended: {outcome} | "
                    f"${start_price:,.2f} -> ${end_price:,.2f} ({change_pct:+.4f}%)"
                )

            # Schedule async PTB backfill -- Gamma API only returns PTB after resolution
            asyncio.get_event_loop().create_task(
                self._backfill_ptb(window_start)
            )
        except Exception as e:
            logger.error(f"Finalize error: {e}")

    async def _backfill_ptb(self, window_start: int):
        """After window resolution, fetch official PTB, actual settlement outcome,
        and Final price from Polymarket. Corrects market_windows with oracle-based data.
        If outcome changes, also corrects paper_trades/live_trades and sends Telegram."""
        slug = f"{config.polymarket.market_slug_prefix}-{window_start}"
        final_price_scraped = False
        for delay in (5, 15, 30, 60, 120, 300, 600, 900):
            await asyncio.sleep(delay)
            try:
                ptb = await self.poly_client.fetch_price_to_beat(slug)
                poly_outcome = await self.poly_client.fetch_settlement_outcome(window_start)

                # At 60s+, also scrape Final price from the resolved page
                fp = None
                if delay >= 60 and not final_price_scraped:
                    fp = await self.poly_client.scrape_final_price(slug)
                    if fp is not None and fp > 10000:
                        execute_write(
                            self.db,
                            "UPDATE market_windows SET btc_end_price = ? WHERE window_start = ?",
                            (fp, window_start),
                        )
                        self.db.commit()
                        final_price_scraped = True
                        logger.info(
                            "Final price backfill: %s | end=$%.2f (scraped from Polymarket)",
                            slug, fp,
                        )

                # -- Read current DB state --
                row = fetch_one(
                    self.db,
                    "SELECT btc_start_price, btc_end_price, actual_outcome FROM market_windows WHERE window_start = ?",
                    (window_start,),
                )
                if row is None:
                    return
                old_start = float(row[0]) if row[0] else None
                end_price = float(row[1]) if row[1] else None
                old_outcome = str(row[2]) if row[2] else None

                # -- Determine PTB (start price) --
                ptb_f = float(ptb) if ptb is not None and float(ptb) > 0 else old_start

                # -- Determine best outcome --
                # Priority: 1) Polymarket oracle API  2) Final price vs PTB  3) end_price vs PTB
                outcome = None
                outcome_source = "unknown"
                if poly_outcome in ("UP", "DOWN"):
                    outcome = poly_outcome
                    outcome_source = "poly_api"
                elif fp is not None and fp > 10000 and ptb_f is not None and ptb_f > 0:
                    outcome = "UP" if fp >= ptb_f else "DOWN"
                    outcome_source = f"final_price(${fp:.2f} vs ptb=${ptb_f:.2f})"
                elif end_price is not None and ptb_f is not None and ptb_f > 0:
                    outcome = "UP" if end_price >= ptb_f else "DOWN"
                    outcome_source = "binance_vs_ptb"

                if ptb_f is None and outcome is None:
                    # No useful data yet, keep trying
                    continue

                # -- Update market_windows --
                if ptb_f is not None:
                    execute_write(
                        self.db,
                        """UPDATE market_windows
                           SET btc_start_price = ?, actual_outcome = COALESCE(?, actual_outcome)
                           WHERE window_start = ?""",
                        (ptb_f, outcome, window_start),
                    )
                elif outcome is not None:
                    execute_write(
                        self.db,
                        """UPDATE market_windows
                           SET actual_outcome = ?
                           WHERE window_start = ? AND actual_outcome != ?""",
                        (outcome, window_start, outcome),
                    )
                self.db.commit()

                outcome_changed = (
                    old_outcome is not None
                    and outcome is not None
                    and old_outcome != outcome
                )

                corrected_tag = (
                    f" [CORRECTED {old_outcome}->{outcome} via {outcome_source}]"
                    if outcome_changed else ""
                )
                logger.info(
                    "PTB backfill: %s | ptb=$%.2f end=$%.2f outcome=%s%s",
                    slug,
                    ptb_f or 0,
                    end_price or (fp or 0),
                    outcome,
                    corrected_tag,
                )

                # -- If outcome changed, correct trades --
                if outcome_changed:
                    self._correct_trades_for_outcome_change(
                        window_start, slug, old_outcome, outcome, outcome_source,
                    )

                # Done if we have definitive outcome
                if outcome is not None and (poly_outcome or final_price_scraped):
                    return

            except Exception as e:
                logger.debug("PTB backfill attempt failed for %s: %s", slug, e)
        logger.warning("PTB backfill exhausted retries for %s", slug)

    def _correct_trades_for_outcome_change(
        self,
        window_start: int,
        slug: str,
        old_outcome: str,
        new_outcome: str,
        source: str,
    ):
        """When actual_outcome changes, update paper_trades and live_trades,
        recalculate PnL, and send Telegram alert."""
        corrections = []

        # -- Correct paper_trades --
        paper_rows = fetch_all_dicts(
            self.db,
            """SELECT id, direction, stake, entry_price, pnl, close_reason
               FROM paper_trades
               WHERE window_start = ? AND archived_at IS NULL""",
            (window_start,),
        )
        for pt in paper_rows:
            direction = str(pt.get("direction", ""))
            stake = float(pt.get("stake") or 0)
            entry_price = float(pt.get("entry_price") or 0)
            old_pnl = float(pt.get("pnl") or 0)
            if not direction or stake <= 0 or entry_price <= 0:
                continue
            won = direction == new_outcome
            shares = stake / entry_price
            new_pnl = (shares * 1.0 - stake) if won else -stake
            execute_write(
                self.db,
                """UPDATE paper_trades
                   SET pnl = ?, won = ?, actual_outcome = ?,
                       close_reason = CONCAT(COALESCE(close_reason,''), ' [adj: ', ?, '->', ?, ']')
                   WHERE id = ?""",
                (new_pnl, 1 if won else 0, new_outcome, old_outcome, new_outcome, pt["id"]),
            )
            corrections.append(
                f"  Paper #{pt['id']} {direction}: ${old_pnl:+.2f}->${new_pnl:+.2f}"
            )

        # -- Correct live_trades (trades table) --
        for table in ("trades", "live_trades"):
            try:
                live_rows = fetch_all_dicts(
                    self.db,
                    f"""SELECT id, direction, COALESCE(stake, amount) as stake, COALESCE(entry_price, price) as entry_price, pnl
                        FROM {table}
                        WHERE window_start = ?""",
                    (window_start,),
                )
            except Exception:
                continue
            for lt in live_rows:
                direction = str(lt.get("direction", ""))
                stake = float(lt.get("stake") or 0)
                entry_price = float(lt.get("entry_price") or 0)
                old_pnl = float(lt.get("pnl") or 0)
                if not direction or stake <= 0 or entry_price <= 0:
                    continue
                won = direction == new_outcome
                shares = stake / entry_price
                new_pnl = (shares * 1.0 - stake) if won else -stake
                execute_write(
                    self.db,
                    f"""UPDATE {table}
                        SET actual_outcome = ?, won = ?, pnl = ?,
                            close_reason = CONCAT(COALESCE(close_reason,''), ' [adj: {old_outcome}->{new_outcome}]')
                        WHERE id = ?""",
                    (new_outcome, 1 if won else 0, new_pnl, lt["id"]),
                )
                corrections.append(
                    f"  Live #{lt['id']} {direction}: ${old_pnl:+.2f}->${new_pnl:+.2f}"
                )

        self.db.commit()
        logger.warning(
            "OUTCOME ADJUSTED %s: %s->%s (via %s)\n%s",
            slug, old_outcome, new_outcome, source,
            "\n".join(corrections) if corrections else "  (no trades affected)",
        )

        # -- Telegram alert --
        if corrections:
            try:
                from telegram_notifier import send_telegram_message
                tg_token = str(getattr(config.trading, "live_telegram_bot_token", "") or "").strip()
                tg_chat = str(getattr(config.trading, "live_telegram_chat_id", "") or "").strip()
                if tg_token:
                    msg = (
                        f"[!] OUTCOME ADJUSTED\n"
                        f"Window: {slug}\n"
                        f"Change: {old_outcome} -> {new_outcome}\n"
                        f"Source: {source}\n"
                        f"\n"
                        + "\n".join(corrections)
                    )
                    send_telegram_message(token=tg_token, chat_id=tg_chat, text=msg)
            except Exception as e:
                logger.debug("Telegram alert failed for outcome correction: %s", e)

    async def _flush_loop(self):
        """Periodically flush buffered data to the database."""
        while self._running:
            await asyncio.sleep(self._flush_loop_interval)
            self._flush(force=False)

    def _flush(self, force: bool = False):
        now = time.time()

        if self._tick_buffer and (force or (now - self._last_tick_flush) >= self._tick_flush_interval):
            # Deduplicate ticks (keep latest per timestamp bucket)
            seen = {}
            for row in self._tick_buffer:
                seen[row[0]] = row  # dedupe by timestamp bucket
            ticks = list(seen.values())

            try:
                executemany_write(
                    self.db,
                    upsert_btc_ticks_sql(),
                    ticks,
                )
                self.db.commit()
                self._last_tick_flush = now
            except Exception as e:
                logger.error(f"Tick flush error: {e}")
            self._tick_buffer.clear()

        if self._odds_buffer and (force or (now - self._last_odds_flush) >= self._odds_flush_interval):
            try:
                executemany_write(
                    self.db,
                    upsert_poly_odds_sql(),
                    self._odds_buffer,
                )
                self.db.commit()
                self._last_odds_flush = now
            except Exception as e:
                logger.error(f"Odds flush error: {e}")
            self._odds_buffer.clear()

        if self._feature1s_buffer and (
            force or (now - self._last_feature1s_flush) >= self._feature1s_flush_interval
        ):
            # Keep only latest row for each (second, window) key.
            seen = {}
            for row in self._feature1s_buffer:
                seen[(row[0], row[2])] = row
            rows = list(seen.values())
            try:
                executemany_write(
                    self.db,
                    upsert_feature_1s_sql(),
                    rows,
                )
                self.db.commit()
                self._last_feature1s_flush = now
            except Exception as e:
                logger.error(f"Feature1s flush error: {e}")
            self._feature1s_buffer.clear()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        self._flush(force=True)
        self.db.close()
        logger.info("Collector stopped, data saved")


# ---------------------------------------------------------------------------
# Status & Export
# ---------------------------------------------------------------------------

def show_status():
    try:
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
    except Exception as e:
        print(f"Database connection error ({db_label()}): {e}")
        return

    tick_count = fetch_one(conn, "SELECT COUNT(*) FROM btc_ticks")[0]
    odds_count = fetch_one(conn, "SELECT COUNT(*) FROM poly_odds")[0]
    feature1s_count = fetch_one(conn, "SELECT COUNT(*) FROM feature_1s")[0]
    window_count = fetch_one(conn, "SELECT COUNT(*) FROM market_windows")[0]
    resolved = fetch_one(
        conn,
        "SELECT COUNT(*) FROM market_windows WHERE actual_outcome IS NOT NULL"
    )[0]

    if tick_count > 0:
        first_tick = fetch_one(conn, "SELECT MIN(ts) FROM btc_ticks")[0]
        last_tick = fetch_one(conn, "SELECT MAX(ts) FROM btc_ticks")[0]
        hours = (last_tick - first_tick) / 3600
        first_dt = datetime.fromtimestamp(first_tick, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(last_tick, tz=timezone.utc)
    else:
        hours = 0
        first_dt = last_dt = "N/A"

    up_count = fetch_one(
        conn,
        "SELECT COUNT(*) FROM market_windows WHERE actual_outcome='UP'"
    )[0]
    down_count = fetch_one(
        conn,
        "SELECT COUNT(*) FROM market_windows WHERE actual_outcome='DOWN'"
    )[0]

    db_size_line = "MariaDB"

    print(f"""
{'='*50}
 DATA COLLECTION STATUS
{'='*50}
  Database:        {db_label()}
  Size:            {db_size_line}

  BTC Ticks:       {tick_count:,}
  Poly Odds:       {odds_count:,}
  Feature 1s:      {feature1s_count:,}
  5-min Windows:   {window_count} ({resolved} resolved)
  
  Time range:      {first_dt} -> {last_dt}
  Duration:        {hours:.1f} hours

  Outcomes:        UP={up_count} DOWN={down_count}
{'='*50}
""")

    # Show recent windows
    rows = fetch_all(
        conn,
        """SELECT window_start, slug, btc_start_price, btc_end_price, actual_outcome
           FROM market_windows ORDER BY window_start DESC LIMIT 10"""
    )

    if rows:
        print("  Recent windows:")
        for ws, slug, sp, ep, outcome in rows:
            dt = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%H:%M")
            if sp and ep:
                chg = ((ep - sp) / sp) * 100
                print(f"    {dt} | {outcome or '?':4s} | ${sp:,.2f} -> ${ep:,.2f} ({chg:+.4f}%)")
            else:
                print(f"    {dt} | pending...")

    # Show odds sample for latest window
    if rows:
        latest_ws = rows[0][0]
        odds_sample = fetch_all(
            conn,
            """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask
               FROM poly_odds WHERE window_start = ?
               ORDER BY ts LIMIT 5""",
            (latest_ws,),
        )
        if odds_sample:
            print(f"\n  Latest window odds sample:")
            for ts, up, down, bid, ask in odds_sample:
                sec = ts - latest_ws
                print(f"    +{sec:5.1f}s | UP={up:.3f} DOWN={down:.3f} | bid={bid:.3f} ask={ask:.3f}")

    conn.close()


def export_data():
    import pandas as pd

    try:
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
    except Exception as e:
        print(f"Database connection error ({db_label()}): {e}")
        return

    # Export ticks
    df_ticks = pd.DataFrame(fetch_all_dicts(conn, "SELECT * FROM btc_ticks ORDER BY ts"))
    df_ticks.to_csv("btc_ticks.csv", index=False)
    print(f"Exported {len(df_ticks)} ticks to btc_ticks.csv")

    # Export odds
    df_odds = pd.DataFrame(fetch_all_dicts(conn, "SELECT * FROM poly_odds ORDER BY ts"))
    df_odds.to_csv("poly_odds.csv", index=False)
    print(f"Exported {len(df_odds)} odds records to poly_odds.csv")

    # Export windows
    df_windows = pd.DataFrame(
        fetch_all_dicts(conn, "SELECT * FROM market_windows ORDER BY window_start")
    )
    df_windows.to_csv("market_windows.csv", index=False)
    print(f"Exported {len(df_windows)} windows to market_windows.csv")

    # Export 1s features
    df_feature = pd.DataFrame(
        fetch_all_dicts(conn, "SELECT * FROM feature_1s ORDER BY ts_sec, window_start")
    )
    df_feature.to_csv("feature_1s.csv", index=False)
    print(f"Exported {len(df_feature)} 1s feature rows to feature_1s.csv")

    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Collect live market data")
    parser.add_argument("--status", action="store_true", help="Show collection stats")
    parser.add_argument("--export", action="store_true", help="Export data to CSV")
    parser.add_argument(
        "--minutes",
        type=float,
        default=None,
        help="Auto-stop after N minutes (e.g. --minutes 15)",
    )
    args = parser.parse_args()

    if args.status:
        show_status()
        return
    if args.export:
        export_data()
        return

    collector = DataCollector()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        collector.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    async def run_with_optional_timer():
        timer_task = None
        if args.minutes is not None and args.minutes > 0:
            auto_stop_seconds = args.minutes * 60.0
            logger.info(f"Auto-stop enabled: {args.minutes:g} minutes")

            async def auto_stop():
                await asyncio.sleep(auto_stop_seconds)
                logger.info(f"Auto-stop reached ({args.minutes:g} minutes)")
                collector.stop()

            timer_task = asyncio.create_task(auto_stop())

        try:
            await collector.start()
        finally:
            if timer_task:
                timer_task.cancel()

    try:
        loop.run_until_complete(run_with_optional_timer())
    except KeyboardInterrupt:
        collector.stop()
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
