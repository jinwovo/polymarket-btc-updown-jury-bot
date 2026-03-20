# CLAUDE.md

## Log Files
- `bot.log` — Live trading (main.py) — INFO+
- `bot_paper.log` — Paper trading (paper_trade_sim.py) — INFO+
- `bot_collector.log` — Data collector (data_collector.py) — INFO+
- Console shows WARNING+ only (trades, errors, corrections)

## Architecture
```
data_collector (single process)
  ├→ Binance WebSocket + Chainlink calibration → btc_ticks
  ├→ CLOB polling 0.1s (refresh_odds) → poly_odds
  ├→ Jury evaluation (3 judges) → signal_cache
  ├→ Playwright PTB/Final price scraping (separate tab)
  └→ Window tracking + outcome backfill (up to 15min retry)
         ↓ signal_cache (DB)
    ┌────┴────┐
  Paper      Live
  (reads signal_cache → entry gates → simulated trade)
  (reads signal_cache → entry gates → FOK order via CLOB API)
```

## Key Config (env/runtime.public.env)
- `JURY_THRESHOLD=2` (majority, no opposing votes allowed)
- `ENTRY_ORDER_MODE=MARKET` (FOK — Fill-or-Kill, 1 API call)
- `MIN_EDGE=0.10`, `MIN_EXPECTED_ROI=0.050`
- `PAPER_ENTRY_START_SEC=90`, `PAPER_DOWN_ENTRY_END_SEC=200`
- `PAPER_PERF_PAUSE_SEC=0` (disabled — old DRY_RUN losses poisoned stats)
- `DRY_RUN` is NOT in env file — dashboard controls via env_overrides

## Critical Rules — DO NOT BREAK
- **Never set `DRY_RUN=true` in runtime.public.env** — config.py loads with override=True, which overwrites dashboard's Start Live DRY_RUN=false
- **Never use GTC orders** — Polymarket POST /order takes 1-23s, GTC requires 4+ API calls (post/poll/poll/cancel) causing timeouts. FOK = 1 call
- **Paper and Live must read from signal_cache** — running separate judges causes divergence (different CLOB snapshots at different times)
- **py_clob_client HTTP must be patched** — default is httpx HTTP/2 with 5s timeout, causes ReadTimeout on every order. Patch in polymarket_client.py: HTTP/1.1, timeout=45s, retries=3
- **All 3 processes poll at 0.1s** — data_collector, paper, live. Rate limit is 150 req/s, we use ~20

## Bugs Found & Fixed (don't reintroduce)
- **DRY_RUN override** — env file's DRY_RUN=true was overwriting dashboard's DRY_RUN=false because config.py uses load_dotenv(override=True). Fix: removed DRY_RUN from env file entirely.
- **HTTP/2 ReadTimeout** — py_clob_client defaults to httpx.Client(http2=True) with 5s timeout. Polymarket doesn't reliably close HTTP/2 streams. Fix: monkey-patch to HTTP/1.1 + 45s timeout.
- **"order too old"** — create_market_order internally calls 3 slow APIs (tick_size, neg_risk, calculate_market_price) each taking 5-15s. By the time post_order runs, the signing timestamp is expired. Fix: pre-fetch all 3 once, then only sign+post in retry loop.
- **"Duplicated" order error** — network error on post_order → retry with same signed order → server says "already accepted". Fix: each retry creates fresh signed order (new nonce). Duplicated response treated as success.
- **Paper/Live divergence** — Paper used build_snapshot() (data_collector's DB signal), Live ran own judges with different CLOB cache → different decisions. Fix: shared signal_cache in DB, single Jury in data_collector.
- **CLOB thin-book phantom edges** — CLOB asks like (0.08/0.93) look like huge edge to judges but are just low liquidity. Fix: _get_ask_prices normalizes when sum deviates >0.25 from 1.0.
- **Perf pause from ghost losses** — DRY_RUN trades recorded as losses → 12.5% WR → 28min cooldown blocking all live entries. Fix: disabled perf_pause entirely (PAPER_PERF_PAUSE_SEC=0).

## Data Reliability
- Data before 2026-03-18 has inaccurate prices (Chainlink RPC drift). Only 2026-03-18+ is reliable.
- Settlement uses Polymarket API (Chainlink-based), NOT Binance prices
- Final price appears on Polymarket page 10-15 min after window close; backfill_final_prices.py corrects DB
- 5 outcome flips found in 24h backfill (Binance-Chainlink $1-7 gap)

## Backtest Results (48h, latest config)
- 113 trades, 62.8% WR, +$6,193 PnL, PF=1.97, maxDD=$673
- UP: 42 trades 61.9% WR, DOWN: 71 trades 63.4% WR
- Trades/hour: 2.4

## File Reference
- `main.py` — Live trading bot (async, reads signal_cache)
- `paper_trade_sim.py` — Paper simulator (sync, reads signal_cache)
- `data_collector.py` — Data collection + shared Jury + signal_cache writer
- `judges.py` — 3 judges (Statistical, Arbitrage, Orderbook) + Jury deliberation
- `polymarket_client.py` — CLOB API client + Playwright scraper + HTTP patch
- `entry_parity.py` — Adaptive threshold calculation
- `exit_policy.py` — Exit rules (hold-to-expiry strategy)
- `trade_gate.py` — Entry gate (EV, probability, coinflip guard)
- `config.py` — All settings via env vars (loads .env with override=True!)
- `env/runtime.public.env` — Runtime knobs (safe to commit)
- `backtest.py` — Backtester using DB data
- `backfill_final_prices.py` — One-time script to correct end prices from Polymarket
- `param_sweep.py` — Parameter optimization script
- `dashboard_server.py` — API server + process manager (Start Live/Paper)
- `db_config.py` — DB schema + helpers (MariaDB port 3400)
- `clob_auth.py` — Polymarket CLOB authentication

## Polymarket API Notes
- GET /book rate limit: 1,500 req/10s (150/s)
- POST /order rate limit: 3,500 req/10s
- POST /order response time: 0.2s (normal) to 23s (under load)
- 425 "service not ready" = market not yet active after window start
- Minimum order size: 5 shares
- Maker orders = 0% fee, Taker ~3.15% at 50/50 odds
- Heartbeat is opt-in (not required unless explicitly started)
