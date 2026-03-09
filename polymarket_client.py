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
            from py_clob_client.order_builder.constants import BUY

            order_args = MarketOrderArgs(token_id=token_id, amount=float(amount), side=BUY)
            signed = self._clob_client.create_market_order(order_args)
            resp = self._clob_client.post_order(signed, orderType=OrderType.FOK)
            result = self._normalize_execution_result(
                mode="MARKET",
                side=side,
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
            return None

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
            from py_clob_client.order_builder.constants import BUY

            ot = str(order_type or "GTC").strip().upper()
            order_type_value = getattr(OrderType, ot, OrderType.GTC)
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, orderType=order_type_value)

            result = self._normalize_execution_result(
                mode=mode,
                side=side,
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
                            side=side,
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
                        side=side,
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
            return None

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

    async def close(self):
        self.stop_odds_polling()
        await self._http.aclose()
