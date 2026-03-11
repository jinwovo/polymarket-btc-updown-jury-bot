"""
Real-time data collector for Polymarket BTC Up/Down 5m markets.

Records BOTH Binance tick prices AND Polymarket real UP/DOWN odds
every second into a local SQLite database.

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
from pathlib import Path
from typing import Optional

import websockets
import httpx

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
    is_sqlite_backend,
    sqlite_db_path,
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

DB_PATH = sqlite_db_path()


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
        self.btc_price: Optional[float] = None
        self.window_start_price: Optional[float] = None
        self._window_start_official: bool = False
        self._last_ptb_sync_ts: float = 0.0
        self._running = False

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

        # Run in parallel: Binance WS + Polymarket polling + flush loop
        await asyncio.gather(
            self._binance_ws_loop(),
            self._polymarket_poll_loop(),
            self._flush_loop(),
            self._window_tracker_loop(),
        )

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

                        # If the window started before first BTC tick arrived, backfill start price once.
                        if self.current_window_start > 0 and self.window_start_price is None:
                            self.window_start_price = price
                            try:
                                execute_write(
                                    self.db,
                                    """UPDATE market_windows
                                       SET btc_start_price = COALESCE(btc_start_price, ?)
                                       WHERE window_start = ?""",
                                    (price, self.current_window_start),
                                )
                                self.db.commit()
                            except Exception as e:
                                logger.debug(f"Start price backfill failed: {e}")

                        # Buffer ticks (aggregate to ~100ms resolution to reduce DB writes)
                        # Only keep the latest tick per 100ms bucket
                        bucket = round(ts, 1)
                        self._tick_buffer.append((bucket, price, volume))

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
                        self._odds_buffer.append((
                            now,
                            self.current_window_start,
                            self.current_market.slug,
                            self.current_market.up_price,
                            self.current_market.down_price,
                            self.current_market.up_best_bid,
                            self.current_market.up_best_ask,
                            self.current_market.down_best_bid,
                            self.current_market.down_best_ask,
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
                    if self.current_window_start > 0 and self.btc_price is not None:
                        self._finalize_window(self.current_window_start, self.btc_price)

                    # Start tracking new window
                    self.current_window_start = window_start
                    self.window_start_price = self.btc_price
                    self._window_start_official = False
                    self._last_ptb_sync_ts = 0.0

                    # Find the Polymarket market
                    self.current_market = await self.poly_client.find_market(window_start)

                    if self.current_market:
                        if (
                            self.current_market.price_to_beat is not None
                            and float(self.current_market.price_to_beat) > 0.0
                        ):
                            # Use Polymarket official reference level when available.
                            self.window_start_price = float(self.current_market.price_to_beat)
                            self._window_start_official = True
                        self._record_window_start(
                            window_start,
                            window_start + config.polymarket.interval_seconds,
                            self.current_market,
                        )
                        btc_str = f"${self.btc_price:,.2f}" if self.btc_price is not None else "N/A"
                        start_str = (
                            f"${self.window_start_price:,.2f}"
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
        """Backfill/correct window start with official Price to Beat when it arrives late."""
        if self.current_window_start <= 0 or self.current_market is None:
            return
        if self._window_start_official and self.window_start_price is not None:
            return

        # Gamma can lag a few seconds after window rollover.
        if now_ts - float(self.current_window_start) > 120.0:
            return
        if (now_ts - self._last_ptb_sync_ts) < 3.0:
            return
        self._last_ptb_sync_ts = now_ts

        ptb = self.current_market.price_to_beat
        if ptb is None or float(ptb) <= 0.0:
            ptb = await self.poly_client.fetch_price_to_beat(self.current_market.slug)
            if ptb is None or float(ptb) <= 0.0:
                return
            self.current_market.price_to_beat = float(ptb)

        new_start = float(ptb)
        prev_start = self.window_start_price
        self.window_start_price = new_start
        self._window_start_official = True

        try:
            execute_write(
                self.db,
                """UPDATE market_windows
                   SET btc_start_price = ?
                   WHERE window_start = ?""",
                (new_start, self.current_window_start),
            )
            self.db.commit()
        except Exception as e:
            logger.debug(f"Price-to-beat sync DB update failed: {e}")
            return

        if prev_start is None:
            logger.info(
                "Window start set from Price to Beat: %s | $%.2f",
                self.current_market.slug,
                new_start,
            )
        elif abs(float(prev_start) - new_start) >= 0.01:
            logger.warning(
                "Window start corrected to Price to Beat: %s | %.2f -> %.2f",
                self.current_market.slug,
                float(prev_start),
                new_start,
            )

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

        btc_price = self.btc_price

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

        self._feature1s_buffer.append(
            (
                ts_sec,
                now_ts,
                self.current_window_start,
                slug,
                seconds_elapsed,
                seconds_remaining,
                float(start_price) if start_price is not None else None,
                float(btc_price) if btc_price is not None else None,
                float(btc_move_pct) if btc_move_pct is not None else None,
                up_ask,
                down_ask,
                up_mid,
                down_mid,
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
            # Get start price from DB
            row = fetch_one(
                self.db,
                "SELECT btc_start_price FROM market_windows WHERE window_start = ?",
                (window_start,),
            )

            if row and row[0] is not None:
                start_price = row[0]
                outcome = "UP" if end_price >= start_price else "DOWN"
            else:
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
        except Exception as e:
            logger.error(f"Finalize error: {e}")

    async def _flush_loop(self):
        """Periodically flush buffered data to SQLite."""
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
    if is_sqlite_backend() and not DB_PATH.exists():
        print("No database found. Run `python data_collector.py` first.")
        return

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

    db_size_line = f"{DB_PATH.stat().st_size / 1024 / 1024:.1f} MB" if is_sqlite_backend() else "N/A (MariaDB)"

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
    if is_sqlite_backend() and not DB_PATH.exists():
        print("No database found.")
        return

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
