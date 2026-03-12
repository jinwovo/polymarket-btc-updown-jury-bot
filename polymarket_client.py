"""
Polymarket client for BTC Up/Down 5-minute markets.
Handles market discovery, real-time odds monitoring, and order placement via CLOB API.
"""
import asyncio
import time
import json
import logging
from typing import Any, Optional
from dataclasses import dataclass

import httpx

from clob_auth import create_authenticated_clob_client
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


def _to_optional_float(value: Any) -> Optional[float]:
    """Best-effort conversion to float; returns None on failure."""
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            return float(s)
    except Exception:
        return None
    return None


def _normalize_order_side(side: Any, *, default: str = "BUY") -> str:
    s = str(side or "").strip().upper()
    if s in {"BUY", "B"}:
        return "BUY"
    if s in {"SELL", "S"}:
        return "SELL"
    d = str(default or "BUY").strip().upper()
    return "SELL" if d == "SELL" else "BUY"


def _find_nested_value(payload: Any, candidates: set[str]) -> Any:
    """Find first matching key value in a nested dict/list payload."""
    keys = {str(k).lower() for k in candidates}
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                if str(key).lower() in keys:
                    return val
                if isinstance(val, (dict, list, tuple)):
                    stack.append(val)
        elif isinstance(cur, (list, tuple)):
            for item in cur:
                if isinstance(item, (dict, list, tuple)):
                    stack.append(item)
    return None


def _extract_order_id(payload: Any) -> Optional[str]:
    # Prefer explicit order-id keys; plain "id" is a last-resort fallback.
    if isinstance(payload, dict):
        for key in ("orderID", "orderId", "order_id", "orderid", "id"):
            if key in payload:
                raw = payload.get(key)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()

    val = _find_nested_value(payload, {"orderid", "order_id"})
    if val is None:
        val = _find_nested_value(payload, {"id"})
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _extract_order_status(payload: Any) -> Optional[str]:
    val = _find_nested_value(payload, {"status", "state", "order_status"})
    if val is None:
        return None
    s = str(val).strip().lower()
    return s if s else None


def _extract_filled_size(payload: Any) -> Optional[float]:
    val = _find_nested_value(
        payload,
        {
            "size_matched",
            "matched_size",
            "filled_size",
            "filled",
            "filledsize",
            "executed_size",
            "size_filled",
            "filled_qty",
            "filled_quantity",
        },
    )
    return _to_optional_float(val)


def _extract_avg_price(payload: Any) -> Optional[float]:
    val = _find_nested_value(
        payload,
        {
            "avg_price",
            "average_price",
            "fill_price",
            "avg_fill_price",
            "executed_price",
            "price",
        },
    )
    price = _to_optional_float(val)
    if price is None:
        return None
    if not (0.0 < price < 1.0):
        return None
    return price


def _extract_balance_value(payload: Any) -> float:
    val = _find_nested_value(
        payload,
        {
            "balance",
            "available_balance",
            "asset_balance",
            "available",
            "amount",
            "size",
        },
    )
    num = _to_optional_float(val)
    if num is None:
        return 0.0
    return max(0.0, float(num))


def _normalize_conditional_amount(raw_value: Any, payload: Any) -> Optional[float]:
    """
    Normalize conditional token balances into share units.
    CLOB may return base-unit integers (commonly 1e6 scale).
    """
    num = _to_optional_float(raw_value)
    if num is None:
        return None
    value = max(0.0, float(num))

    dec_raw = _find_nested_value(
        payload,
        {"decimals", "token_decimals", "asset_decimals", "conditional_decimals"},
    )
    dec_val = _to_optional_float(dec_raw)
    if dec_val is not None:
        try:
            dec = int(dec_val)
        except Exception:
            dec = -1
        if 0 <= dec <= 18:
            scale = float(10 ** dec)
            if scale > 1.0 and abs(value - round(value)) < 1e-9 and value >= scale:
                return float(value / scale)

    # Fallback: conditional balances are often emitted in 6-decimal base units.
    if abs(value - round(value)) < 1e-9 and value >= 1_000_000:
        return float(value / 1_000_000.0)

    return value


def _normalize_collateral_amount(raw_value: Any, payload: Any) -> Optional[float]:
    num = _to_optional_float(raw_value)
    if num is None:
        return None
    value = float(num)
    if value < 0.0:
        value = 0.0

    dec_raw = _find_nested_value(
        payload,
        {"decimals", "token_decimals", "asset_decimals", "collateral_decimals"},
    )
    dec_val = _to_optional_float(dec_raw)
    if dec_val is not None:
        try:
            dec = int(dec_val)
        except Exception:
            dec = -1
        if 0 <= dec <= 18:
            scale = float(10 ** dec)
            if scale > 1.0 and abs(value - round(value)) < 1e-9 and value >= scale:
                value = float(value / scale)
                return max(0.0, value)

    # Fallback for USDC-like base units (6 decimals) when metadata is missing.
    if abs(value - round(value)) < 1e-9 and value >= 1_000_000:
        value = float(value / 1_000_000.0)
    return max(0.0, value)


def _is_terminal_status(status: Optional[str]) -> bool:
    if not status:
        return False
    return status in {
        "filled",
        "cancelled",
        "canceled",
        "rejected",
        "expired",
        "failed",
        "matched",
    }


def _classify_order_exception(exc: Exception) -> tuple[str, bool]:
    """
    Classify exchange exceptions into deterministic rejections vs uncertain fills.
    Returns: (status, uncertain_fill)
    """
    msg = str(exc or "")
    lower = msg.lower()

    # Deterministic reject: exchange explicitly rejected the order.
    if (
        ("not enough balance" in lower)
        or ("insufficient balance" in lower)
        or ("insufficient funds" in lower)
        or ("allowance" in lower)
    ):
        return "rejected_balance_allowance", False

    if ("invalid price" in lower) or ("invalid size" in lower) or ("tick size" in lower):
        return "rejected_invalid_order", False

    # Deterministic reject: FAK had no immediate match (normal liquidity miss).
    if (
        ("no orders found to match with fak order" in lower)
        or (
            ("fak order" in lower or "fak orders" in lower)
            and ("no match" in lower)
        )
        or (
            ("fak order" in lower or "fak orders" in lower)
            and ("partially filled or killed" in lower)
        )
    ):
        return "rejected_no_match_fak", False

    # Unknown exceptions are treated as uncertain to preserve safety.
    return "unknown_error", True


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
    # Official Polymarket reference level ("Price to Beat"), when available.
    price_to_beat: Optional[float] = None


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
        self._claim_unsupported_logged = False

    async def _fetch_price_to_beat(self, slug: str) -> Optional[float]:
        """Fetch official Price to Beat from Gamma eventMetadata."""
        if not slug:
            return None
        try:
            resp = await self._http.get(
                f"{config.polymarket.gamma_url}/events",
                params={"slug": slug},
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                return None
            event = payload[0] if isinstance(payload[0], dict) else None
            if not event:
                return None
            meta = event.get("eventMetadata")
            if not isinstance(meta, dict):
                return None
            ptb = _to_optional_float(meta.get("priceToBeat"))
            if ptb is None or ptb <= 0.0:
                return None
            return float(ptb)
        except Exception:
            return None

    async def fetch_price_to_beat(self, slug: str) -> Optional[float]:
        """Public wrapper for runtime loops to refresh official start reference."""
        return await self._fetch_price_to_beat(slug)

    async def _init_clob(self, *, force: bool = False):
        """Initialize authenticated CLOB client for trading."""
        if config.trading.dry_run and not force:
            logger.info("Dry-run mode: CLOB client not initialized")
            return
        try:
            client, meta = create_authenticated_clob_client()
            self._clob_client = client
            logger.info(
                "CLOB client initialized (creds=%s, funder_source=%s, sig_type=%s/%s)",
                meta.get("creds_source"),
                meta.get("funder_source"),
                meta.get("signature_type"),
                meta.get("signature_type_source"),
            )
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

            price_to_beat = _to_optional_float(
                _find_nested_value(market, {"pricetobeat", "price_to_beat"})
            )
            if price_to_beat is None or price_to_beat <= 0.0:
                price_to_beat = await self._fetch_price_to_beat(slug)

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
                price_to_beat=(
                    float(price_to_beat)
                    if price_to_beat is not None and float(price_to_beat) > 0.0
                    else None
                ),
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
        async def _fetch_side(
            side: str,
            token_id: str,
        ) -> Optional[tuple[str, float, float, float]]:
            if not token_id:
                return None
            resp = await self._http.get(
                f"{config.polymarket.clob_url}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                return None
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = max((float(b.get("price", 0.0)) for b in bids), default=0.0)
            best_ask = min((float(a.get("price", 1.0)) for a in asks), default=1.0)
            mid_price = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask < 1 else 0.5
            return side, best_bid, best_ask, mid_price

        tasks = []
        if market.up_token_id:
            tasks.append(_fetch_side("up", market.up_token_id))
        if market.down_token_id:
            tasks.append(_fetch_side("down", market.down_token_id))

        if not tasks:
            return False

        updated = False
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.debug(f"Orderbook fetch failed: {result}")
                continue
            if result is None:
                continue
            side, best_bid, best_ask, mid_price = result
            if side == "up":
                market.up_price = mid_price
                market.up_best_bid = best_bid
                market.up_best_ask = best_ask
            else:
                market.down_price = mid_price
                market.down_best_bid = best_bid
                market.down_best_ask = best_ask
            updated = True

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

    def _normalize_execution_result(
        self,
        *,
        mode: str,
        side: str,
        token_id: str,
        requested_amount: float,
        raw_payload: Any,
        default_price: Optional[float] = None,
        requested_size: Optional[float] = None,
        order_id_hint: Optional[str] = None,
        status_hint: Optional[str] = None,
    ) -> dict:
        order_id = order_id_hint or _extract_order_id(raw_payload)
        status = (status_hint or _extract_order_status(raw_payload) or "unknown").lower()

        filled_size = _extract_filled_size(raw_payload)
        if filled_size is None and requested_size is not None and status in {"filled", "matched"}:
            filled_size = float(requested_size)
        filled_size = float(filled_size or 0.0)

        avg_price = _extract_avg_price(raw_payload)
        if avg_price is None and default_price is not None and 0.0 < float(default_price) < 1.0:
            avg_price = float(default_price)

        executed_notional = 0.0
        if filled_size > 0.0 and avg_price is not None and avg_price > 0.0:
            executed_notional = float(filled_size * avg_price)
        elif status in {"filled", "matched"} and requested_amount > 0.0:
            executed_notional = float(requested_amount)
            if filled_size <= 0.0 and avg_price is not None and avg_price > 0.0:
                filled_size = float(executed_notional / avg_price)

        filled = bool(executed_notional > 0.0 or filled_size > 0.0)

        return {
            "ok": True,
            "mode": str(mode),
            "side": str(side),
            "token_id": str(token_id),
            "order_id": order_id,
            "status": status,
            "requested_amount": float(requested_amount),
            "requested_size": float(requested_size) if requested_size is not None else None,
            "requested_price": float(default_price) if default_price is not None else None,
            "executed_notional": float(executed_notional),
            "executed_size": float(filled_size),
            "executed_price": float(avg_price) if avg_price is not None else None,
            "filled": bool(filled),
            "accepted": bool(status not in {"rejected", "failed"}),
            "timed_out": False,
            "cancel_attempted": False,
            "cancelled": False,
            "reason": None,
            "raw": raw_payload,
        }

    async def _get_best_ask(self, token_id: str) -> Optional[float]:
        if not token_id:
            return None
        try:
            resp = await self._http.get(
                f"{config.polymarket.clob_url}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                return None
            book = resp.json()
            asks = book.get("asks", [])
            best_ask = min((float(a.get("price", 1.0)) for a in asks), default=1.0)
            if 0.0 < best_ask < 1.0:
                return float(best_ask)
            return None
        except Exception:
            return None

    async def _get_best_bid(self, token_id: str) -> Optional[float]:
        if not token_id:
            return None
        try:
            resp = await self._http.get(
                f"{config.polymarket.clob_url}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                return None
            book = resp.json()
            bids = book.get("bids", [])
            best_bid = max((float(b.get("price", 0.0)) for b in bids), default=0.0)
            if 0.0 < best_bid < 1.0:
                return float(best_bid)
            return None
        except Exception:
            return None

    async def _check_entry_price_drift(
        self, token_id: str, reference_ask: Optional[float]
    ) -> tuple[bool, Optional[float], Optional[str]]:
        ref = _to_optional_float(reference_ask)
        if ref is None or not (0.0 < ref < 1.0):
            return True, None, None

        current_ask = await self._get_best_ask(token_id)
        if current_ask is None:
            return False, None, "skip entry: unable to fetch live ask for drift check"

        if current_ask <= ref:
            return True, current_ask, None

        drift_abs = float(current_ask - ref)
        drift_ratio = float(drift_abs / max(ref, 1e-9))

        max_abs = max(0.0, float(config.trading.max_entry_price_drift_abs))
        max_ratio = max(0.0, float(config.trading.max_entry_price_drift_ratio))
        if drift_abs > max_abs or drift_ratio > max_ratio:
            return (
                False,
                current_ask,
                (
                    "skip entry: ask drift too large "
                    f"(ref={ref:.3f}, live={current_ask:.3f}, abs=+{drift_abs:.4f}, rel=+{drift_ratio:.2%})"
                ),
            )
        return True, current_ask, None

    async def _get_conditional_balance(self, token_id: str) -> float:
        if not token_id:
            return 0.0
        if config.trading.dry_run:
            return 0.0
        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            return 0.0
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=str(token_id),
            )
            payload = await asyncio.to_thread(self._clob_client.get_balance_allowance, params)
            bal_raw = _find_nested_value(
                payload,
                {"balance", "available_balance", "asset_balance"},
            )
            normalized = _normalize_conditional_amount(bal_raw, payload)
            if normalized is None:
                normalized = _normalize_conditional_amount(_extract_balance_value(payload), payload)
            return float(normalized or 0.0)
        except Exception as e:
            logger.warning("conditional balance fetch failed (token=%s): %s", token_id, e)
            return 0.0

    async def get_collateral_balance(self) -> Optional[float]:
        """
        Return available collateral balance in UI units (e.g., USDC),
        normalized from API base units when needed.
        """
        if config.trading.dry_run:
            return None
        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            return None
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            payload = await asyncio.to_thread(self._clob_client.get_balance_allowance, params)
            bal_raw = _find_nested_value(
                payload,
                {"available_balance", "asset_balance", "balance", "available", "amount", "size"},
            )
            normalized = _normalize_collateral_amount(bal_raw, payload)
            if normalized is None:
                extracted = _extract_balance_value(payload)
                normalized = _normalize_collateral_amount(extracted, payload)
            return normalized
        except Exception as e:
            logger.warning("collateral balance fetch failed: %s", e)
            return None

    async def _get_open_orders_for_asset(self, asset_id: str) -> list[dict]:
        if not asset_id:
            return []
        if config.trading.dry_run:
            return []
        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams

            params = OpenOrderParams(asset_id=str(asset_id))
            payload = await asyncio.to_thread(self._clob_client.get_orders, params)
            if isinstance(payload, list):
                return [x for x in payload if isinstance(x, dict)]
            if isinstance(payload, dict):
                # py-clob-client may wrap results as {data:[...]} / {orders:[...]}
                for key in ("data", "orders", "items", "results"):
                    items = payload.get(key)
                    if isinstance(items, list):
                        return [x for x in items if isinstance(x, dict)]
            return []
        except Exception as e:
            logger.warning("open-order fetch failed (asset=%s): %s", asset_id, e)
            return []

    async def inspect_market_exposure(self, market: MarketInfo) -> dict:
        up_token = str(market.up_token_id or "")
        down_token = str(market.down_token_id or "")
        if not up_token or not down_token:
            return {
                "ok": False,
                "error": "missing market token ids",
                "up_balance": 0.0,
                "down_balance": 0.0,
                "up_open_orders": 0,
                "down_open_orders": 0,
                "open_orders_total": 0,
            }

        up_balance, down_balance, up_orders, down_orders = await asyncio.gather(
            self._get_conditional_balance(up_token),
            self._get_conditional_balance(down_token),
            self._get_open_orders_for_asset(up_token),
            self._get_open_orders_for_asset(down_token),
        )
        up_count = len(up_orders)
        down_count = len(down_orders)
        return {
            "ok": True,
            "error": None,
            "up_balance": float(up_balance),
            "down_balance": float(down_balance),
            "up_open_orders": int(up_count),
            "down_open_orders": int(down_count),
            "open_orders_total": int(up_count + down_count),
        }

    async def cancel_market_orders(self, market: MarketInfo) -> dict:
        if config.trading.dry_run:
            return {"ok": True, "cancelled": 0, "errors": []}
        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            return {"ok": False, "cancelled": 0, "errors": ["clob client unavailable"]}

        cancelled = 0
        errors: list[str] = []
        for token_id in [str(market.up_token_id or ""), str(market.down_token_id or "")]:
            if not token_id:
                continue
            try:
                await asyncio.to_thread(self._clob_client.cancel_market_orders, "", token_id)
                cancelled += 1
            except Exception as e:
                errors.append(f"{token_id[:12]}...: {e}")
        return {"ok": len(errors) == 0, "cancelled": cancelled, "errors": errors}

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        reference_price: Optional[float] = None,
    ) -> Optional[dict]:
        if config.trading.dry_run:
            sim_price = _to_optional_float(reference_price) or 0.5
            sim_size = float(amount / sim_price) if sim_price > 0 else 0.0
            logger.info(
                f"[DRY RUN] Market/FOK {side} ${amount:.2f} "
                f"(token={token_id[:16]}..., ref={sim_price:.4f})"
            )
            return {
                "ok": True,
                "mode": "MARKET",
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": "dry_run",
                "requested_amount": float(amount),
                "requested_size": sim_size,
                "requested_price": float(sim_price),
                "executed_notional": float(amount),
                "executed_size": float(sim_size),
                "executed_price": float(sim_price),
                "filled": True,
                "accepted": True,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": None,
                "dry_run": True,
            }

        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            logger.error("CLOB client not available, cannot place order")
            return None

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = _normalize_order_side(side, default="BUY")
            side_const = BUY if order_side == "BUY" else SELL
            order_args = MarketOrderArgs(token_id=token_id, amount=float(amount), side=side_const)
            signed = self._clob_client.create_market_order(order_args)
            resp = self._clob_client.post_order(signed, orderType=OrderType.FOK)
            result = self._normalize_execution_result(
                mode="MARKET",
                side=order_side,
                token_id=token_id,
                requested_amount=float(amount),
                raw_payload=resp,
                default_price=_to_optional_float(reference_price),
            )
            logger.info(
                "Market/FOK order %s: status=%s filled=$%.2f",
                side,
                result.get("status"),
                result.get("executed_notional", 0.0),
            )
            return result
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            status, uncertain = _classify_order_exception(e)
            reason = (
                f"market order exception: {e}"
                if uncertain
                else f"market order rejected: {e}"
            )
            return {
                "ok": False,
                "mode": "MARKET",
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": status,
                "requested_amount": float(amount),
                "requested_size": None,
                "requested_price": _to_optional_float(reference_price),
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "uncertain_fill": bool(uncertain),
                "reason": reason,
            }

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        timeout_seconds: Optional[float] = None,
        poll_interval_seconds: Optional[float] = None,
    ) -> Optional[dict]:
        price = float(price)
        size = float(size)
        requested_amount = float(price * size)
        mode = f"LIMIT_{str(order_type).strip().upper()}"

        if config.trading.dry_run:
            logger.info(
                f"[DRY RUN] {mode} {side} @ {price:.4f} x {size:.4f} shares "
                f"(token={token_id[:16]}...)"
            )
            return {
                "ok": True,
                "mode": mode,
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": "dry_run",
                "requested_amount": requested_amount,
                "requested_size": size,
                "requested_price": price,
                "executed_notional": requested_amount,
                "executed_size": size,
                "executed_price": price,
                "filled": True,
                "accepted": True,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": None,
                "dry_run": True,
            }

        if not self._clob_client:
            await self._init_clob()
        if not self._clob_client:
            logger.error("CLOB client not available")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            ot = str(order_type or "GTC").strip().upper()
            order_type_value = getattr(OrderType, ot, OrderType.GTC)
            order_side = _normalize_order_side(side, default="BUY")
            side_const = BUY if order_side == "BUY" else SELL
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=side_const)
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, orderType=order_type_value)

            result = self._normalize_execution_result(
                mode=mode,
                side=order_side,
                token_id=token_id,
                requested_amount=requested_amount,
                raw_payload=resp,
                default_price=price,
                requested_size=size,
            )
            order_id = result.get("order_id")

            if ot == "GTC" and order_id:
                timeout = (
                    float(timeout_seconds)
                    if timeout_seconds is not None
                    else float(config.trading.limit_order_timeout_seconds)
                )
                poll = (
                    float(poll_interval_seconds)
                    if poll_interval_seconds is not None
                    else float(config.trading.order_poll_interval_seconds)
                )
                timeout = max(0.25, timeout)
                poll = max(0.05, poll)
                deadline = time.monotonic() + timeout
                latest_payload = resp

                while time.monotonic() < deadline:
                    await asyncio.sleep(poll)
                    try:
                        state = self._clob_client.get_order(order_id)
                        latest_payload = state
                    except Exception as e:
                        logger.debug(f"get_order({order_id}) failed: {e}")
                        continue

                    state_status = _extract_order_status(state)
                    state_filled = _extract_filled_size(state) or 0.0
                    if _is_terminal_status(state_status) or float(state_filled) >= float(size * 0.999):
                        result = self._normalize_execution_result(
                            mode=mode,
                            side=order_side,
                            token_id=token_id,
                            requested_amount=requested_amount,
                            raw_payload=state,
                            default_price=price,
                            requested_size=size,
                            order_id_hint=order_id,
                        )
                        break
                else:
                    cancel_ok = False
                    try:
                        self._clob_client.cancel(order_id)
                        cancel_ok = True
                    except Exception as e:
                        logger.warning(f"Cancel order failed ({order_id}): {e}")
                    try:
                        latest_payload = self._clob_client.get_order(order_id)
                    except Exception:
                        pass

                    result = self._normalize_execution_result(
                        mode=mode,
                        side=order_side,
                        token_id=token_id,
                        requested_amount=requested_amount,
                        raw_payload=latest_payload,
                        default_price=price,
                        requested_size=size,
                        order_id_hint=order_id,
                    )
                    result["timed_out"] = True
                    result["cancel_attempted"] = True
                    result["cancelled"] = bool(cancel_ok)
                    if not result.get("filled"):
                        result["reason"] = (
                            f"limit timeout ({timeout:.2f}s): unfilled size cancelled"
                            if cancel_ok
                            else f"limit timeout ({timeout:.2f}s): cancel failed"
                        )

            logger.info(
                "%s order %s: status=%s filled=$%.2f",
                mode,
                side,
                result.get("status"),
                result.get("executed_notional", 0.0),
            )
            return result
        except Exception as e:
            logger.error(f"Limit order failed: {e}")
            status, uncertain = _classify_order_exception(e)
            reason = (
                f"limit order exception: {e}"
                if uncertain
                else f"limit order rejected: {e}"
            )
            return {
                "ok": False,
                "mode": mode,
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": status,
                "requested_amount": requested_amount,
                "requested_size": size,
                "requested_price": price,
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "uncertain_fill": bool(uncertain),
                "reason": reason,
            }

    async def place_entry_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        reference_ask: Optional[float] = None,
    ) -> Optional[dict]:
        mode = str(config.trading.entry_order_mode or "LIMIT_GTC").strip().upper()
        if mode not in {"LIMIT_GTC", "LIMIT_FAK", "MARKET"}:
            logger.warning("Invalid ENTRY_ORDER_MODE=%s, fallback to LIMIT_GTC", mode)
            mode = "LIMIT_GTC"

        # Dry-run should not be blocked by live drift checks.
        if config.trading.dry_run:
            sim_ask = _to_optional_float(reference_ask)
            if sim_ask is None or not (0.0 < sim_ask < 1.0):
                sim_ask = 0.5

            if mode == "MARKET":
                return await self.place_market_order(
                    token_id=token_id,
                    side=side,
                    amount=float(amount),
                    reference_price=float(sim_ask),
                )

            return await self.place_limit_order(
                token_id=token_id,
                side=side,
                price=float(sim_ask),
                size=float(amount / sim_ask),
                order_type=("FAK" if mode == "LIMIT_FAK" else "GTC"),
                timeout_seconds=float(config.trading.limit_order_timeout_seconds),
                poll_interval_seconds=float(config.trading.order_poll_interval_seconds),
            )

        # Protect against stale UI/model ask when the live orderbook has already moved.
        drift_ok, live_ask, drift_reason = await self._check_entry_price_drift(token_id, reference_ask)
        if not drift_ok:
            logger.info(drift_reason)
            return {
                "ok": True,
                "mode": mode,
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": "rejected_drift",
                "requested_amount": float(amount),
                "requested_size": None,
                "requested_price": _to_optional_float(reference_ask),
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": drift_reason,
            }

        working_ask = _to_optional_float(live_ask)
        if working_ask is None:
            working_ask = _to_optional_float(reference_ask)

        if mode == "MARKET":
            return await self.place_market_order(
                token_id=token_id,
                side=side,
                amount=float(amount),
                reference_price=working_ask,
            )

        if working_ask is None or not (0.0 < working_ask < 1.0):
            return {
                "ok": True,
                "mode": mode,
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": "invalid_price",
                "requested_amount": float(amount),
                "requested_size": None,
                "requested_price": None,
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": "skip entry: no valid ask for limit order",
            }

        size = float(amount / working_ask)
        if size <= 0.0:
            return {
                "ok": True,
                "mode": mode,
                "side": str(side),
                "token_id": str(token_id),
                "order_id": None,
                "status": "invalid_size",
                "requested_amount": float(amount),
                "requested_size": 0.0,
                "requested_price": float(working_ask),
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": "skip entry: computed share size <= 0",
            }

        if mode == "LIMIT_FAK":
            return await self.place_limit_order(
                token_id=token_id,
                side=side,
                price=float(working_ask),
                size=float(size),
                order_type="FAK",
            )

        return await self.place_limit_order(
            token_id=token_id,
            side=side,
            price=float(working_ask),
            size=float(size),
            order_type="GTC",
            timeout_seconds=float(config.trading.limit_order_timeout_seconds),
            poll_interval_seconds=float(config.trading.order_poll_interval_seconds),
        )

    async def place_exit_order(
        self,
        token_id: str,
        side: str,
        shares: float,
        reference_bid: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Exit an open position by selling shares (taker-ish LIMIT_FAK at current best bid).
        """
        side_norm = _normalize_order_side(side, default="SELL")
        if side_norm != "SELL":
            side_norm = "SELL"

        sz = float(shares or 0.0)
        if sz <= 0.0:
            return {
                "ok": True,
                "mode": "LIMIT_FAK",
                "side": side_norm,
                "token_id": str(token_id),
                "order_id": None,
                "status": "invalid_size",
                "requested_amount": 0.0,
                "requested_size": 0.0,
                "requested_price": _to_optional_float(reference_bid),
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": "skip exit: computed size <= 0",
            }

        working_bid = await self._get_best_bid(token_id)
        if working_bid is None:
            working_bid = _to_optional_float(reference_bid)
        if working_bid is None or not (0.0 < float(working_bid) < 1.0):
            return {
                "ok": True,
                "mode": "LIMIT_FAK",
                "side": side_norm,
                "token_id": str(token_id),
                "order_id": None,
                "status": "invalid_price",
                "requested_amount": 0.0,
                "requested_size": float(sz),
                "requested_price": None,
                "executed_notional": 0.0,
                "executed_size": 0.0,
                "executed_price": None,
                "filled": False,
                "accepted": False,
                "timed_out": False,
                "cancel_attempted": False,
                "cancelled": False,
                "reason": "skip exit: no valid live bid",
            }

        price = float(working_bid)
        return await self.place_limit_order(
            token_id=token_id,
            side=side_norm,
            price=price,
            size=float(sz),
            order_type="FAK",
        )

    def _normalize_hex_bytes32(self, value: Any) -> Optional[bytes]:
        s = str(value or "").strip().lower()
        if not s:
            return None
        if s.startswith("0x"):
            s = s[2:]
        if len(s) != 64:
            return None
        try:
            return bytes.fromhex(s)
        except Exception:
            return None

    def _resolve_claim_owner(self) -> Optional[str]:
        owner = str(config.polymarket.funder or "").strip()
        if owner:
            return owner
        if self._clob_client:
            try:
                addr = str(self._clob_client.get_address() or "").strip()
                if addr:
                    return addr
            except Exception:
                pass
        return None

    def _build_relay_client(self):
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

        private_key = str(config.polymarket.private_key or "").strip()
        if private_key and not private_key.startswith("0x") and len(private_key) == 64:
            private_key = f"0x{private_key}"
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY missing for relayer claim")

        builder_key = str(config.polymarket.builder_api_key or "").strip()
        builder_secret = str(config.polymarket.builder_api_secret or "").strip()
        builder_passphrase = str(config.polymarket.builder_api_passphrase or "").strip()
        if not (builder_key and builder_secret and builder_passphrase):
            raise RuntimeError(
                "POLY_BUILDER_API_KEY/SECRET/PASSPHRASE required for relayer redeemPositions"
            )

        relayer_url = str(config.polymarket.relayer_url or "").strip()
        if not relayer_url:
            raise RuntimeError("POLYMARKET_RELAYER_URL missing")

        chain_id = int(getattr(config.polymarket, "relayer_chain_id", 137) or 137)
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_key,
                secret=builder_secret,
                passphrase=builder_passphrase,
            )
        )
        return RelayClient(
            relayer_url=relayer_url,
            chain_id=chain_id,
            private_key=private_key,
            builder_config=builder_config,
        )

    async def _fetch_redeemable_positions(self, owner: str) -> list[dict[str, Any]]:
        data_api_url = str(config.polymarket.data_api_url or "").rstrip("/")
        if not data_api_url:
            return []
        try:
            resp = await self._http.get(
                f"{data_api_url}/positions",
                params={
                    "user": owner,
                    "redeemable": "true",
                    "sizeThreshold": "0",
                },
            )
            if resp.status_code != 200:
                return []
            payload = resp.json()
            if isinstance(payload, list):
                return [x for x in payload if isinstance(x, dict)]
            if isinstance(payload, dict):
                for key in ("data", "items", "positions", "results"):
                    items = payload.get(key)
                    if isinstance(items, list):
                        return [x for x in items if isinstance(x, dict)]
            return []
        except Exception:
            return []

    async def _build_redeem_transactions(self, owner: str):
        from py_builder_relayer_client.models import OperationType, SafeTransaction

        positions = await self._fetch_redeemable_positions(owner)
        if not positions:
            return [], {}, []

        grouped_index_sets: dict[str, set[int]] = {}
        grouped_current_value: dict[str, float] = {}

        for pos in positions:
            condition_id = str(pos.get("conditionId") or pos.get("condition_id") or "").strip()
            condition_bytes = self._normalize_hex_bytes32(condition_id)
            if condition_bytes is None:
                continue

            index_set_val = _to_optional_float(pos.get("indexSet"))
            if index_set_val is None:
                outcome_idx = _to_optional_float(pos.get("outcomeIndex"))
                if outcome_idx is not None and int(outcome_idx) >= 0:
                    index_set_val = float(1 << int(outcome_idx))
            if index_set_val is None:
                continue

            index_set = int(index_set_val)
            if index_set <= 0:
                continue

            grouped_index_sets.setdefault(condition_id, set()).add(index_set)
            current_value = float(_to_optional_float(pos.get("currentValue")) or 0.0)
            grouped_current_value[condition_id] = grouped_current_value.get(condition_id, 0.0) + current_value

        if not grouped_index_sets:
            return [], {}, positions

        collateral = ""
        conditional_tokens = ""
        if self._clob_client:
            try:
                collateral = str(self._clob_client.get_collateral_address() or "").strip()
                conditional_tokens = str(self._clob_client.get_conditional_address() or "").strip()
            except Exception:
                collateral = ""
                conditional_tokens = ""
        if not collateral or not conditional_tokens:
            from py_clob_client.config import get_contract_config

            cfg = get_contract_config(int(getattr(config.polymarket, "relayer_chain_id", 137) or 137))
            collateral = str(cfg.collateral)
            conditional_tokens = str(cfg.conditional_tokens)

        from eth_abi import encode as abi_encode
        from eth_utils import keccak, to_checksum_address

        selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        parent_collection_id = b"\x00" * 32
        collateral_addr = to_checksum_address(collateral)
        ctf_addr = to_checksum_address(conditional_tokens)

        transactions: list[SafeTransaction] = []
        for condition_id, idx_sets in grouped_index_sets.items():
            condition_bytes = self._normalize_hex_bytes32(condition_id)
            if condition_bytes is None:
                continue
            sorted_sets = sorted({int(x) for x in idx_sets if int(x) > 0})
            if not sorted_sets:
                continue
            calldata = selector + abi_encode(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [collateral_addr, parent_collection_id, condition_bytes, sorted_sets],
            )
            transactions.append(
                SafeTransaction(
                    to=ctf_addr,
                    operation=OperationType.Call,
                    data=f"0x{calldata.hex()}",
                    value="0",
                )
            )

        return transactions, grouped_current_value, positions

    async def _claim_via_relayer(self, *, owner: str) -> dict:
        try:
            relay = self._build_relay_client()
            expected_safe = str(await asyncio.to_thread(relay.get_expected_safe) or "").strip()
            if expected_safe and expected_safe.lower() != owner.lower():
                return {
                    "ok": False,
                    "supported": True,
                    "claimed": 0.0,
                    "status": "claim_error",
                    "method": "relayer_redeemPositions",
                    "owner": owner,
                    "expected_safe": expected_safe,
                    "error": (
                        "relayer signer/private key maps to a different proxy safe; "
                        "set funder to expected safe or use matching signer key"
                    ),
                }

            if expected_safe:
                deployed = bool(await asyncio.to_thread(relay.get_deployed, expected_safe))
                if not deployed:
                    return {
                        "ok": False,
                        "supported": True,
                        "claimed": 0.0,
                        "status": "claim_error",
                        "method": "relayer_redeemPositions",
                        "owner": owner,
                        "expected_safe": expected_safe,
                        "error": f"expected safe {expected_safe} is not deployed",
                    }

            txs, grouped_current_value, positions = await self._build_redeem_transactions(owner)
            if not txs:
                return {
                    "ok": True,
                    "supported": True,
                    "claimed": 0.0,
                    "status": "nothing_to_claim",
                    "method": "relayer_redeemPositions",
                    "owner": owner,
                    "positions_found": len(positions),
                    "conditions": 0,
                }

            response = await asyncio.to_thread(relay.execute, txs, "auto_redeem_positions")
            wait_payload = await asyncio.to_thread(response.wait)
            tx_state = str(_find_nested_value(wait_payload, {"state"}) or "").strip().upper()
            ok = bool(wait_payload) and tx_state in {"STATE_MINED", "STATE_CONFIRMED"}
            estimated_claim = float(sum(grouped_current_value.values()))
            return {
                "ok": ok,
                "supported": True,
                "claimed": estimated_claim if ok else 0.0,
                "status": "claimed" if ok else "claim_pending",
                "method": "relayer_redeemPositions",
                "owner": owner,
                "positions_found": len(positions),
                "conditions": len(txs),
                "transaction_id": getattr(response, "transaction_id", None),
                "transaction_hash": getattr(response, "transaction_hash", None),
                "state": tx_state or None,
                "raw": wait_payload,
            }
        except Exception as e:
            return {
                "ok": False,
                "supported": True,
                "claimed": 0.0,
                "status": "claim_error",
                "method": "relayer_redeemPositions",
                "owner": owner,
                "error": str(e),
            }

    async def auto_claim_winnings(self, *, ignore_dry_run: bool = False) -> dict:
        """
        Best-effort auto-claim hook.
        Tries native py_clob_client claim APIs first, then relayer redeemPositions.
        """
        if config.trading.dry_run and not ignore_dry_run:
            return {"ok": True, "supported": False, "claimed": 0.0, "status": "dry_run"}

        if not self._clob_client:
            await self._init_clob(force=ignore_dry_run)
        if not self._clob_client and not ignore_dry_run:
            return {"ok": False, "supported": False, "claimed": 0.0, "status": "no_client"}

        candidate_methods = (
            "claim",
            "redeem",
            "redeem_positions",
            "settle",
            "settle_positions",
            "claim_rewards",
        )
        funder = str(config.polymarket.funder or "").strip()
        arg_trials: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            (tuple(), {}),
        ]
        if funder:
            arg_trials.append(((funder,), {}))
            arg_trials.append((tuple(), {"funder": funder}))

        if self._clob_client:
            for method_name in candidate_methods:
                fn = getattr(self._clob_client, method_name, None)
                if not callable(fn):
                    continue
                last_err: Optional[str] = None
                for args, kwargs in arg_trials:
                    try:
                        payload = await asyncio.to_thread(fn, *args, **kwargs)
                        claimed_raw = _find_nested_value(
                            payload,
                            {"claimed", "redeemed", "amount", "value", "payout", "usdc"},
                        )
                        claimed = float(_to_optional_float(claimed_raw) or 0.0)
                        if claimed <= 0.0:
                            claimed = 0.0
                        return {
                            "ok": True,
                            "supported": True,
                            "claimed": float(claimed),
                            "status": "claimed",
                            "method": method_name,
                            "raw": payload,
                        }
                    except TypeError as e:
                        last_err = str(e)
                        continue
                    except Exception as e:
                        return {
                            "ok": False,
                            "supported": True,
                            "claimed": 0.0,
                            "status": "claim_error",
                            "method": method_name,
                            "error": str(e),
                        }
                return {
                    "ok": False,
                    "supported": True,
                    "claimed": 0.0,
                    "status": "claim_signature_mismatch",
                    "method": method_name,
                    "error": last_err or "unsupported argument signature",
                }

            if not self._claim_unsupported_logged:
                logger.warning(
                    "Auto-claim unavailable: this py_clob_client build exposes no claim/redeem API. "
                    "Falling back to relayer redeemPositions."
                )
                self._claim_unsupported_logged = True

        owner = self._resolve_claim_owner()
        if not owner:
            return {
                "ok": False,
                "supported": True,
                "claimed": 0.0,
                "status": "claim_error",
                "method": "relayer_redeemPositions",
                "error": "missing funder/owner address for redeem lookup",
            }
        return await self._claim_via_relayer(owner=owner)

    async def close(self):
        self.stop_odds_polling()
        await self._http.aclose()
