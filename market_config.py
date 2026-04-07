"""
Multi-market configuration for Polymarket binary options.

Each market has its own:
  - Signal cache tables (signal_cache_X, signal_cache_log_X)
  - Trade tables (paper_trades_X, live_trades_X)
  - Entry parameters (timing, price range, filters)
  - Price source (btc_ticks or eth_ticks)

Shared across all markets:
  - judges.py (Jury is asset-agnostic)
  - trade_gate.py, exit_policy.py
  - polymarket_client.py (order execution)
  - Telegram + Polymarket API credentials
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketDef:
    """Definition of a single binary options market."""
    key: str                    # e.g. "btc5", "btc15", "eth5"
    label: str                  # e.g. "BTC 5min", "BTC 15min", "ETH 5min"
    slug_prefix: str            # e.g. "btc-updown-5m", "btc-updown-15m"
    interval_seconds: int       # 300 (5min) or 900 (15min)
    price_table: str            # "btc_ticks" or "eth_ticks"
    price_col: str              # column name in price table: "price"
    env_prefix: str             # e.g. "BTC15_", "ETH5_" for env vars
    signal_cache_table: str     # e.g. "signal_cache_btc15"
    signal_cache_log_table: str # e.g. "signal_cache_log_btc15"
    paper_trades_table: str     # e.g. "paper_trades_btc15"
    live_trades_table: str      # e.g. "live_trades_btc15"


# -- Market Definitions --

BTC_5M = MarketDef(
    key="btc5",
    label="BTC 5min",
    slug_prefix="btc-updown-5m",
    interval_seconds=300,
    price_table="btc_ticks",
    price_col="price",
    env_prefix="PAPER_",  # existing env vars (no prefix change)
    signal_cache_table="signal_cache",
    signal_cache_log_table="signal_cache_log",
    paper_trades_table="paper_trades",
    live_trades_table="live_trades",
)

BTC_15M = MarketDef(
    key="btc15",
    label="BTC 15min",
    slug_prefix="btc-updown-15m",
    interval_seconds=900,
    price_table="btc_ticks",
    price_col="price",
    env_prefix="BTC15_",
    signal_cache_table="signal_cache_btc15",
    signal_cache_log_table="signal_cache_log_btc15",
    paper_trades_table="paper_trades_btc15",
    live_trades_table="live_trades_btc15",
)

ETH_5M = MarketDef(
    key="eth5",
    label="ETH 5min",
    slug_prefix="eth-updown-5m",
    interval_seconds=300,
    price_table="eth_ticks",
    price_col="price",
    env_prefix="ETH5_",
    signal_cache_table="signal_cache_eth5",
    signal_cache_log_table="signal_cache_log_eth5",
    paper_trades_table="paper_trades_eth5",
    live_trades_table="live_trades_eth5",
)

ALL_MARKETS = [BTC_5M, BTC_15M, ETH_5M]
NEW_MARKETS = [BTC_15M, ETH_5M]


def get_market(key: str) -> MarketDef:
    """Look up market by key (btc5, btc15, eth5)."""
    for m in ALL_MARKETS:
        if m.key == key:
            return m
    raise ValueError(f"Unknown market key: {key}")


def env(market: MarketDef, name: str, default: str = "") -> str:
    """Read env var with market prefix fallback.

    First tries market-specific: BTC15_ENTRY_START_SEC
    Then falls back to PAPER_ENTRY_START_SEC (BTC 5min default)
    """
    val = os.getenv(f"{market.env_prefix}{name}")
    if val is not None:
        return val
    # Fallback to PAPER_ prefix (BTC 5min defaults)
    val = os.getenv(f"PAPER_{name}")
    if val is not None:
        return val
    return default
