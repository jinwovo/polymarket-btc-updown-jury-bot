"""
BTC price feed with Chainlink oracle calibration.

Primary: Binance WS (real-time, sub-second)
Calibration: Chainlink on-chain Polygon feed (~27s heartbeat)

adjusted_price = binance_price - offset
where offset = binance_at_last_chainlink_update - chainlink_price

This gives real-time responsiveness with Chainlink-aligned price levels,
matching what Polymarket uses for settlement.
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

# Chainlink BTC/USD on Polygon (27s heartbeat, 0.1% deviation)
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
CHAINLINK_POLL_INTERVAL = 10  # poll every 10s to catch 27s updates quickly
POLYGON_RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]

AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class PriceTick:
    price: float
    timestamp: float  # unix seconds
    volume: float = 0.0


class ChainlinkCalibrator:
    """Polls Chainlink on-chain feed and maintains a price offset for calibration."""

    def __init__(self):
        self.chainlink_price: Optional[float] = None
        self.chainlink_updated_at: float = 0.0
        self.binance_at_update: Optional[float] = None  # binance price when chainlink updated
        self.offset: float = 0.0  # binance - chainlink at last update
        self._last_round_id: Optional[int] = None
        self._w3 = None
        self._feed = None
        self._running = False
        self._decimals: int = 8
        self._rpc_idx = 0

    def _init_web3(self):
        try:
            from web3 import Web3
            rpc_url = POLYGON_RPC_URLS[self._rpc_idx % len(POLYGON_RPC_URLS)]
            self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            self._feed = self._w3.eth.contract(
                address=Web3.to_checksum_address(CHAINLINK_BTC_USD_POLYGON),
                abi=AGGREGATOR_V3_ABI,
            )
            self._decimals = self._feed.functions.decimals().call()
            logger.info("Chainlink calibrator connected via %s (decimals=%d)", rpc_url, self._decimals)
        except Exception as e:
            logger.warning("Chainlink init failed: %s", e)
            self._w3 = None
            self._feed = None

    def _fetch_once(self) -> bool:
        """Fetch latest round. Returns True if new round detected."""
        if self._feed is None:
            self._init_web3()
        if self._feed is None:
            return False
        try:
            round_id, answer, _, updated_at, _ = (
                self._feed.functions.latestRoundData().call()
            )
            price = answer / (10 ** self._decimals)
            if round_id != self._last_round_id:
                self._last_round_id = round_id
                self.chainlink_price = price
                self.chainlink_updated_at = float(updated_at)
                return True
            return False
        except Exception as e:
            logger.warning("Chainlink fetch error: %s", e)
            # Rotate RPC on failure
            self._rpc_idx += 1
            self._w3 = None
            self._feed = None
            return False

    def update_offset(self, binance_price_at_update: float):
        """Recalculate offset using the Binance price at the time Chainlink updated.
        This avoids timing mismatch where Chainlink updated seconds ago but we
        poll now and use a stale/drifted Binance price."""
        if self.chainlink_price is not None and binance_price_at_update > 0:
            self.binance_at_update = binance_price_at_update
            self.offset = binance_price_at_update - self.chainlink_price
            age = time.time() - self.chainlink_updated_at
            logger.info(
                "Chainlink calibration: chainlink=$%.2f binance_at_update=$%.2f "
                "offset=$%.2f (chainlink age=%.1fs)",
                self.chainlink_price,
                binance_price_at_update,
                self.offset,
                age,
            )

    # Max age (seconds) before calibration is considered stale.
    # If Chainlink hasn't updated in this long, fall back to raw Binance
    # to avoid huge price errors from an outdated offset.
    MAX_CALIBRATION_AGE_SEC = 120.0

    def adjust(self, binance_price: float) -> float:
        """Apply calibration offset to a Binance price.
        Returns Chainlink-estimated price."""
        if self.chainlink_price is None:
            return binance_price  # no calibration data yet
        # Stale calibration guard: if last Chainlink update is too old,
        # the offset may be wildly wrong.  Fall back to raw Binance.
        age = time.time() - self.chainlink_updated_at if self.chainlink_updated_at > 0 else 999
        if age > self.MAX_CALIBRATION_AGE_SEC:
            logger.warning(
                "Chainlink calibration stale (%.0fs old, max=%ds). "
                "Using raw Binance price $%.2f instead of adjusted.",
                age, int(self.MAX_CALIBRATION_AGE_SEC), binance_price,
            )
            return binance_price
        return binance_price - self.offset

    @property
    def offset_bps(self) -> float:
        """Current offset in basis points."""
        if self.chainlink_price is None or self.chainlink_price <= 0:
            return 0.0
        return (self.offset / self.chainlink_price) * 10000.0

    @property
    def is_calibrated(self) -> bool:
        return self.chainlink_price is not None

    async def poll_loop(self, get_binance_price, get_binance_price_at=None):
        """Async loop that polls Chainlink and updates offset.

        get_binance_price: callable returning current Binance price
        get_binance_price_at: callable(ts) returning Binance price at timestamp ts
            If provided, offset uses the Binance price at chainlink_updated_at
            to avoid timing mismatch.
        """
        self._running = True
        while self._running:
            try:
                new_round = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_once
                )
                if new_round:
                    # Use Binance price at the exact time Chainlink updated
                    bp = None
                    if get_binance_price_at is not None and self.chainlink_updated_at > 0:
                        bp = get_binance_price_at(self.chainlink_updated_at)
                    # Fallback to current price if lookup fails
                    if bp is None or bp <= 0:
                        bp = get_binance_price()
                    if bp is not None and bp > 0:
                        self.update_offset(bp)
            except Exception as e:
                logger.warning("Chainlink poll error: %s", e)
            await asyncio.sleep(CHAINLINK_POLL_INTERVAL)

    def stop(self):
        self._running = False


class BinancePriceFeed:
    """Real-time BTC/USDT price feed via Binance WebSocket
    with Chainlink oracle calibration."""

    def __init__(self, enable_chainlink: bool = True):
        self.ticks: deque[PriceTick] = deque(maxlen=50000)
        self.current_price: Optional[float] = None  # raw Binance price
        self.last_update: float = 0.0
        self._ws = None
        self._running = False
        self.calibrator = ChainlinkCalibrator() if enable_chainlink else None

    @property
    def adjusted_price(self) -> Optional[float]:
        """Chainlink-calibrated price (or raw Binance if not calibrated)."""
        if self.current_price is None:
            return None
        if self.calibrator is not None:
            return self.calibrator.adjust(self.current_price)
        return self.current_price

    @property
    def prices(self) -> list[float]:
        """Return list of recent prices (raw Binance)."""
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
        """Connect to Binance WebSocket and stream trades.
        Also starts Chainlink calibrator if enabled."""
        self._running = True
        await self._fill_initial_buffer()

        # Start Chainlink calibrator in background
        if self.calibrator is not None:
            asyncio.create_task(
                self.calibrator.poll_loop(
                    get_binance_price=lambda: self.current_price,
                    get_binance_price_at=self.get_price_at,
                )
            )
            logger.info("Chainlink calibrator started (poll every %ds)", CHAINLINK_POLL_INTERVAL)

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
        if self.calibrator is not None:
            self.calibrator.stop()
