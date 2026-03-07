"""
Polymarket client for BTC Up/Down 5-minute markets.
Handles market discovery, real-time odds monitoring, and order placement via CLOB API.
"""
import asyncio
import time
import math
import json
import logging
from typing import Any, Optional, Callable
from dataclasses import dataclass, field

import httpx

from config import config

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list:
    """Normalize API fields that may arrive as list or JSON-encoded string."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # Gamma occasionally returns list-like fields as JSON strings.
        if s[0] in "[{":
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                return []
        return [s]
    return []


def _to_float(value: Any, default: float = 0.5) -> float:
    """Convert mixed API numeric formats safely."""
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return default
            # Some fields can be nested JSON values.
            if s[0] in "[{":
                parsed = json.loads(s)
                if isinstance(parsed, list) and parsed:
                    return _to_float(parsed[0], default=default)
                return default
            return float(s)
    except Exception:
        return default
    return default


@dataclass
class MarketInfo:
    """Info about a single 5-minute Up/Down market."""
    condition_id: str
    question: str
    slug: str
    start_timestamp: int  # unix epoch when the 5-min window starts
    end_timestamp: int    # unix epoch when the 5-min window ends
    up_token_id: str
    down_token_id: str
    up_price: float       # current price of UP outcome (0-1)
    down_price: float     # current price of DOWN outcome (0-1)
    active: bool
    # Real-time orderbook snapshot
    up_best_bid: float = 0.0
    up_best_ask: float = 1.0
    down_best_bid: float = 0.0
    down_best_ask: float = 1.0
    last_odds_update: float = 0.0  # when odds were last refreshed


def compute_market_timestamps(now: Optional[float] = None) -> dict:
    """
    Compute the current and next 5-minute market timestamps.
    Markets are aligned to 5-minute boundaries in UTC.
    """
    if now is None:
        now = time.time()

    interval = config.polymarket.interval_seconds
    current_start = int(now // interval) * interval
    current_end = current_start + interval
    next_start = current_end
    next_end = next_start + interval

    return {
        "current": {"start": current_start, "end": current_end},
        "next": {"start": next_start, "end": next_end},
        "seconds_elapsed": now - current_start,
        "seconds_remaining": current_end - now,
    }


def market_slug_for_timestamp(ts: int) -> str:
    return f"{config.polymarket.market_slug_prefix}-{ts}"


def market_url_for_timestamp(ts: int) -> str:
    return f"https://polymarket.com/event/{market_slug_for_timestamp(ts)}"


class PolymarketClient:
    """Client for interacting with Polymarket's Gamma API and CLOB."""

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=10.0)
        self._clob_client = None
        self._odds_polling = False

    async def _init_clob(self):
        """Initialize the CLOB client for trading (requires API keys)."""
        if config.trading.dry_run:
            logger.info("Dry-run mode: CLOB client not initialized")
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=config.polymarket.api_key,
                api_secret=config.polymarket.api_secret,
                api_passphrase=config.polymarket.api_passphrase,
            )
            self._clob_client = ClobClient(
                config.polymarket.clob_url,
                chain_id=137,
                creds=creds,
                funder=config.polymarket.funder,
            )
            logger.info("CLOB client initialized")
        except Exception as e:
            logger.error(f"Failed to init CLOB client: {e}")
            self._clob_client = None

    async def find_market(self, start_timestamp: int) -> Optional[MarketInfo]:
        """Find a BTC Up/Down 5m market by its start timestamp."""
        slug = market_slug_for_timestamp(start_timestamp)
        try:
            resp = await self._http.get(
                f"{config.polymarket.gamma_url}/markets",
                params={"slug": slug, "closed": "false"},
            )
            if resp.status_code != 200:
                logger.warning(f"Gamma API returned {resp.status_code} for slug={slug}")
                return None

            markets = resp.json()
            if not markets:
                resp2 = await self._http.get(
                    f"{config.polymarket.gamma_url}/events",
                    params={"slug": slug},
                )
                if resp2.status_code == 200:
                    events = resp2.json()
                    if events and len(events) > 0:
                        event = events[0]
                        if "markets" in event and len(event["markets"]) > 0:
                            markets = event["markets"]

            if not markets:
                logger.debug(f"No market found for slug={slug}")
                return None

            market = markets[0] if isinstance(markets, list) else markets

            tokens = _as_list(market.get("tokens", []))
            clobTokenIds = _as_list(market.get("clobTokenIds", []))
            outcomePrices = _as_list(market.get("outcomePrices", []))

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
                        up_price = _to_float(t.get("price", 0.5), default=up_price)
                    elif outcome == "down":
                        down_token = str(t.get("token_id", t.get("tokenId", "")))
                        down_price = _to_float(t.get("price", 0.5), default=down_price)
            elif clobTokenIds and len(clobTokenIds) >= 2:
                up_token = str(clobTokenIds[0])
                down_token = str(clobTokenIds[1])

            if outcomePrices and len(outcomePrices) >= 2:
                up_price = _to_float(outcomePrices[0], default=up_price)
                down_price = _to_float(outcomePrices[1], default=down_price)

            return MarketInfo(
                condition_id=market.get("condition_id", market.get("conditionId", "")),
                question=market.get("question", ""),
                slug=slug,
                start_timestamp=start_timestamp,
                end_timestamp=start_timestamp + config.polymarket.interval_seconds,
                up_token_id=up_token,
                down_token_id=down_token,
                up_price=up_price,
                down_price=down_price,
                active=market.get("active", True),
                last_odds_update=time.time(),
            )

        except Exception as e:
            logger.error(f"Error finding market {slug}: {e}")
            return None

    async def refresh_odds(self, market: MarketInfo) -> bool:
        """
        Refresh UP/DOWN prices from the CLOB orderbook.
        This is the critical real-time data source.
        Returns True if prices were updated.
        """
        updated = False
        for side, token_id in [("up", market.up_token_id), ("down", market.down_token_id)]:
            if not token_id:
                continue
            try:
                resp = await self._http.get(
                    f"{config.polymarket.clob_url}/book",
                    params={"token_id": token_id},
                )
                if resp.status_code != 200:
                    continue
                book = resp.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])

                best_bid = max((float(b["price"]) for b in bids), default=0.0)
                best_ask = min((float(a["price"]) for a in asks), default=1.0)
                mid_price = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask < 1 else 0.5

                if side == "up":
                    market.up_price = mid_price
                    market.up_best_bid = best_bid
                    market.up_best_ask = best_ask
                else:
                    market.down_price = mid_price
                    market.down_best_bid = best_bid
                    market.down_best_ask = best_ask

                updated = True
            except Exception as e:
                logger.debug(f"Orderbook fetch failed for {side}: {e}")

        if updated:
            market.last_odds_update = time.time()
        return updated

    async def start_odds_polling(self, market: MarketInfo, interval: float = 0.5):
        """Poll orderbook at high frequency to keep odds current."""
        self._odds_polling = True
        while self._odds_polling and market.active:
            await self.refresh_odds(market)
            await asyncio.sleep(interval)

    def stop_odds_polling(self):
        self._odds_polling = False

    async def place_market_order(
        self, token_id: str, side: str, amount: float
    ) -> Optional[dict]:
        if config.trading.dry_run:
            logger.info(f"[DRY RUN] Would buy {side} for ${amount:.2f} (token={token_id[:16]}...)")
            return {"dry_run": True, "side": side, "amount": amount}

        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            logger.error("CLOB client not available, cannot place order")
            return None

        try:
            from py_clob_client.clob_types import MarketOrderArgs
            order_args = MarketOrderArgs(token_id=token_id, amount=amount)
            resp = self._clob_client.create_and_post_market_order(order_args)
            logger.info(f"Order placed: {side} ${amount:.2f} -> {resp}")
            return resp
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    async def place_limit_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> Optional[dict]:
        if config.trading.dry_run:
            logger.info(
                f"[DRY RUN] Limit {side} @ {price:.4f} x {size:.2f} shares "
                f"(token={token_id[:16]}...)"
            )
            return {"dry_run": True, "side": side, "price": price, "size": size}

        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            logger.error("CLOB client not available")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed)
            logger.info(f"Limit order: {side} @ {price:.4f} x {size:.2f} -> {resp}")
            return resp
        except Exception as e:
            logger.error(f"Limit order failed: {e}")
            return None

    async def close(self):
        self.stop_odds_polling()
        await self._http.aclose()
