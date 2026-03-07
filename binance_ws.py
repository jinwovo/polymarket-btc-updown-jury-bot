"""
Binance WebSocket client for real-time BTC/USDT price streaming.
Maintains a rolling buffer of recent prices for technical analysis.
"""
import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
import httpx

from config import config

logger = logging.getLogger(__name__)


@dataclass
class PriceTick:
    price: float
    timestamp: float  # unix seconds
    volume: float = 0.0


class BinancePriceFeed:
    """Real-time BTC/USDT price feed via Binance WebSocket."""

    def __init__(self):
        self.ticks: deque[PriceTick] = deque(maxlen=50000)
        self.current_price: Optional[float] = None
        self.last_update: float = 0.0
        self._ws = None
        self._running = False

    @property
    def prices(self) -> list[float]:
        """Return list of recent prices."""
        return [t.price for t in self.ticks]

    @property
    def timestamps(self) -> list[float]:
        return [t.timestamp for t in self.ticks]

    def get_price_at(self, ts: float) -> Optional[float]:
        """Get the closest price to a given timestamp."""
        if not self.ticks:
            return None
        best = None
        best_diff = float("inf")
        for tick in self.ticks:
            diff = abs(tick.timestamp - ts)
            if diff < best_diff:
                best_diff = diff
                best = tick.price
        return best

    def get_prices_since(self, ts: float) -> list[PriceTick]:
        """Get all ticks since a given timestamp."""
        return [t for t in self.ticks if t.timestamp >= ts]

    def get_recent_prices(self, seconds: int = 300) -> list[PriceTick]:
        """Get ticks from the last N seconds."""
        cutoff = time.time() - seconds
        return self.get_prices_since(cutoff)

    def price_change_pct(self, from_price: float) -> Optional[float]:
        """Percentage change from a reference price to current."""
        if self.current_price is None or from_price == 0:
            return None
        return ((self.current_price - from_price) / from_price) * 100.0

    async def fetch_current_price_rest(self) -> Optional[float]:
        """Fallback: fetch current price via REST API."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(config.binance.rest_url)
                data = resp.json()
                return float(data["price"])
        except Exception as e:
            logger.error(f"REST price fetch failed: {e}")
            return None

    async def fetch_klines(self, interval: str = "1m", limit: int = 60) -> list[dict]:
        """Fetch historical klines for initial buffer fill."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    config.binance.kline_url,
                    params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
                )
                raw = resp.json()
                klines = []
                for k in raw:
                    klines.append({
                        "open_time": k[0] / 1000.0,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": k[6] / 1000.0,
                    })
                return klines
        except Exception as e:
            logger.error(f"Kline fetch failed: {e}")
            return []

    async def _fill_initial_buffer(self):
        """Load recent klines so judges have data immediately."""
        klines = await self.fetch_klines(interval="1m", limit=120)
        for k in klines:
            tick = PriceTick(price=k["close"], timestamp=k["close_time"], volume=k["volume"])
            self.ticks.append(tick)
        if klines:
            self.current_price = klines[-1]["close"]
            self.last_update = klines[-1]["close_time"]
            logger.info(f"Loaded {len(klines)} klines, latest price: {self.current_price}")

    async def connect(self):
        """Connect to Binance WebSocket and stream trades."""
        self._running = True
        await self._fill_initial_buffer()

        while self._running:
            try:
                async with websockets.connect(config.binance.ws_url) as ws:
                    self._ws = ws
                    logger.info("Connected to Binance WebSocket")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        tick = PriceTick(
                            price=float(data["p"]),
                            timestamp=float(data["T"]) / 1000.0,
                            volume=float(data.get("q", 0)),
                        )
                        self.ticks.append(tick)
                        self.current_price = tick.price
                        self.last_update = tick.timestamp

                        # Prune old ticks beyond buffer window
                        cutoff = time.time() - config.binance.price_buffer_seconds
                        while self.ticks and self.ticks[0].timestamp < cutoff:
                            self.ticks.popleft()

            except websockets.ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"WebSocket error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False
