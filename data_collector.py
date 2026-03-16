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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
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
        self._odds_flush_interval = 0.5
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

        # Run in parallel: Binance WS + Polymarket polling + flush loop + Chainlink
        await asyncio.gather(
            self._binance_ws_loop(),
            self._polymarket_poll_loop(),
            self._flush_loop(),
            self._window_tracker_loop(),
            self._chainlink.poll_loop(
                get_binance_price=lambda: self.btc_price,
                get_binance_price_at=self._get_raw_price_at,
            ),
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
                        # Binance fallback removed — wrong start price causes wrong direction.

                        # Buffer ticks with Chainlink-calibrated price
                        bucket = round(ts, 1)
                        self._tick_buffer.append((bucket, self.btc_price_adjusted, volume))

            except websockets.ConnectionClosed:
                logger.warning("Binance WS disconnected, reconnecting...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Binance WS error: {e}")
                await asyncio.sleep(5)

    async def _polymarket_poll_loop(self):
        """Poll Polymarket orderbook every second."""
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
            except Exception as e:
                logger.debug(f"Odds poll error: {e}")

            await asyncio.sleep(0.5)

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

                    # Start tracking new window — chainlink_adj immediately, scrape at +3s
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
                            # Immediate fallback — scrape will correct in ~3s
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
        # Φ(z) via math.erfc
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
                logger.warning("Window %s finalized without PTB start price — outcome UNKNOWN", window_start)
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

            # Schedule async PTB backfill — Gamma API only returns PTB after resolution
            asyncio.get_event_loop().create_task(
                self._backfill_ptb(window_start)
            )
        except Exception as e:
            logger.error(f"Finalize error: {e}")

    async def _backfill_ptb(self, window_start: int):
        """After window resolution, fetch official PTB and actual settlement outcome
        from Polymarket. Corrects market_windows with oracle-based data."""
        slug = f"{config.polymarket.market_slug_prefix}-{window_start}"
        for delay in (5, 15, 30):
            await asyncio.sleep(delay)
            try:
                ptb = await self.poly_client.fetch_price_to_beat(slug)
                # Also check Polymarket's actual settlement outcome (Chainlink-based)
                poly_outcome = await self.poly_client.fetch_settlement_outcome(window_start)
                if ptb is not None and float(ptb) > 0.0:
                    ptb_f = float(ptb)
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
                    # Prefer Polymarket's oracle-based outcome over Binance comparison
                    if poly_outcome in ("UP", "DOWN"):
                        outcome = poly_outcome
                    elif end_price is not None:
                        outcome = "UP" if end_price >= ptb_f else "DOWN"
                    else:
                        outcome = None
                    execute_write(
                        self.db,
                        """UPDATE market_windows
                           SET btc_start_price = ?, actual_outcome = COALESCE(?, actual_outcome)
                           WHERE window_start = ?""",
                        (ptb_f, outcome, window_start),
                    )
                    self.db.commit()
                    delta = abs(ptb_f - old_start) if old_start else 0
                    corrected = (
                        f" [CORRECTED from {old_outcome}]"
                        if old_outcome and outcome and old_outcome != outcome
                        else ""
                    )
                    logger.info(
                        "PTB backfill: %s | $%.2f (was $%.2f, delta=$%.2f) outcome=%s%s",
                        slug, ptb_f, old_start or 0, delta, outcome, corrected,
                    )
                    return
                elif poly_outcome in ("UP", "DOWN"):
                    # No PTB yet but we have the settlement outcome
                    execute_write(
                        self.db,
                        """UPDATE market_windows
                           SET actual_outcome = ?
                           WHERE window_start = ? AND actual_outcome != ?""",
                        (poly_outcome, window_start, poly_outcome),
                    )
                    self.db.commit()
                    logger.info(
                        "Settlement outcome backfill: %s outcome=%s (no PTB yet)",
                        slug, poly_outcome,
                    )
                    return
            except Exception as e:
                logger.debug("PTB backfill attempt failed for %s: %s", slug, e)
        logger.warning("PTB backfill exhausted retries for %s", slug)

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
            for ts, price, vol in self._tick_buffer:
                seen[ts] = (ts, price, vol)
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
