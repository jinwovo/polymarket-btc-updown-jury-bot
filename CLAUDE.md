# CLAUDE.md

## Price Source Hierarchy (MOST IMPORTANT)
Settlement uses Chainlink oracle. The "ground truth" price is:
1. **RTDS WebSocket** (`wss://ws-live-data.polymarket.com`) = Chainlink Data Streams ~1s ← PRIMARY
2. **Playwright scrape** = same Chainlink price, fallback when RTDS down ← FALLBACK
3. **Chainlink RPC** = on-chain aggregator, 27s heartbeat ← LAST RESORT

**ALL entry decisions (Paper, Live, Backtest) must use data_collector's calibrated price.**
Live's own WebSocket price is ONLY used for the actual FOK order (CLOB handles matching).

## Log Files
- `bot.log` — Live trading (main.py) — INFO+ (console: WARNING+)
- `bot_paper.log` — Paper trading (paper_trade_sim.py) — INFO+ (console: WARNING+)
- `bot_collector.log` — Data collector (data_collector.py) — INFO+ (console: WARNING+)

## Architecture
```
data_collector (single process, 0.1s tick)
  ├→ Binance WebSocket + Chainlink calibration → btc_ticks (with buy/sell vol)
  ├→ Playwright scrapes Polymarket Current price → calibration offset
  ├→ CLOB polling (refresh_odds) → poly_odds
  ├→ Jury evaluation (3 judges: Statistical, Arbitrage, Orderbook)
  ├→ Price guards (divergence, momentum, trend) → guards_passed
  └→ signal_cache DB table (direction, guards_passed, prices, buy_sell_ratio)
         ↓
    ┌────┴────┐
  Paper      Live
  reads signal_cache → entry_gate → simulated trade
  reads signal_cache → entry_gate → MAKER_FIRST/FOK order
```

## Key Config (env/runtime.public.env)
- `JURY_THRESHOLD=2` (majority, no opposing votes with 3 judges)
- `ENTRY_ORDER_MODE=MAKER_FIRST` (try maker 0% fee → FOK fallback)
- `MIN_EDGE=0.10`, `MIN_EXPECTED_ROI=0.050`
- `PAPER_ENTRY_START_SEC=90`, `PAPER_DOWN_ENTRY_END_SEC=200`
- `PAPER_PERF_PAUSE_SEC=0` (disabled)
- `LIVE_MAX_DRAWDOWN_STOP_PCT=1.0` (disabled)
- `DRY_RUN` is NOT in env file — dashboard controls via env_overrides

## Critical Rules — DO NOT BREAK
- **Never set `DRY_RUN=true` in runtime.public.env** — config.py override=True overwrites dashboard's Start Live
- **Never use GTC orders for entry** — Polymarket POST /order takes 1-23s, use FOK
- **Paper and Live must read from signal_cache** — running separate judges causes divergence
- **entry_gate must use signal_cache prices (_btc_now/_btc_start)** — NOT ctx.current_binance_price (WebSocket has $50-200 offset)
- **Price guards run in data_collector only** — Paper/Live read guards_passed, no re-checking
- **py_clob_client HTTP must be patched** — default HTTP/2 + 5s timeout = ReadTimeout. Patch: HTTP/1.1, 45s, retries=3
- **All processes poll at 0.1s** — data_collector, paper, live. Rate limit 150 req/s, we use ~20
- **Duplicate orders → uncertain_fill** — never assume full fill on "duplicated" error
- **GTC cancel fail → skip FOK** — prevents double position

## Bugs Found & Fixed (don't reintroduce)
- **DRY_RUN override** — env file override=True overwrites dashboard's DRY_RUN=false
- **HTTP/2 ReadTimeout** — py_clob_client default. Patch to HTTP/1.1 + 45s
- **"order too old"** — create_market_order makes 3 slow API calls. Pre-fetch + cache
- **Paper/Live divergence** — different CLOB snapshots, different judges, different timing. Fix: shared signal_cache
- **Drawdown stop at 20%** — was silently blocking ALL Live entries with $25 seed
- **Perf pause from ghost losses** — DRY_RUN trades recorded as losses → 28min cooldown
- **Loss streak adaptive strictness** — old losses raised EV threshold from 5% to 5.8%
- **entry_gate using WebSocket prices** — caused different coinflip_guard results
- **Momentum guard timing divergence** — 3s BTC bounce = Paper passes, Live fails
- **Unicode ≈ in exit reason** — caused logging format error
- **Operator precedence in guards_passed check** — `not X if cached else True` bug
- **CLOB thin-book phantom edges** — judges saw asks=(0.08/0.93), normalized when sum>0.25
- **Polymarket page freeze** — auto-reload after 15s stale price

## Data Collection
- `btc_ticks`: price, volume, buy_volume, sell_volume (Binance "m" flag)
- `poly_odds`: up/down bid/ask/mid/spread/overround (0.1s)
- `signal_cache`: direction, guards_passed, btc_move_pct, buy_sell_ratio, binance_rtds_gap, gate_allow/ev/reason
- Buy/sell volume bias in judges: DISABLED (backtest showed negative PF impact)

## TODO: Pending Data Analysis (apply after sufficient data)

### Binance-RTDS Gap Arbitrage (data collecting since 2026-03-22 03:45 KST)
- `signal_cache.binance_rtds_gap` = Binance raw - RTDS Chainlink price
- **NOT the raw gap itself** — need gap DELTA (change from average)
- Strategy: avg_gap_60s = rolling mean, gap_delta = current - avg
  - gap_delta > threshold → UP (Binance surging ahead)
  - gap_delta < -threshold → DOWN (Binance dropping ahead)
- **Wait for 48+ hours of data**, then:
  1. Analyze avg gap, std dev, distribution
  2. Find optimal threshold via backtest
  3. Apply as fast-lane trigger or ArbitrageJudge enhancement
- Risk: don't apply raw gap as signal (constant offset ≠ direction)

### Buy/Sell Volume Ratio (data collecting since 2026-03-20)
- `btc_ticks.buy_volume / sell_volume` stored, ratio in signal_cache
- Judge bias DISABLED — backtest showed PF 1.71 → 1.26
- Revisit after 1 week of data with proper feature analysis

## Backtest Reference
- Last validated (11h): 18 trades, 61.1% WR, +$856, PF=1.71
- Judges accuracy: Statistical=61.1%, Arbitrage=66.7%, Orderbook=58.8%

## Polymarket API Notes
- GET /book: 1,500 req/10s (150/s)
- POST /order: 3,500 req/10s, response 0.2s-23s
- 425 "service not ready" = market not active yet after window start
- Min order size: 5 shares
- Maker = 0% fee, Taker ~3.15% at 50/50
- Heartbeat opt-in (not required)

## File Reference
- `main.py` — Live trading (reads signal_cache, places orders)
- `paper_trade_sim.py` — Paper trading (reads signal_cache, simulates)
- `data_collector.py` — CLOB polling + Jury + guards → signal_cache
- `judges.py` — 3 judges + MomentumJudge (disabled) + Jury
- `polymarket_client.py` — CLOB API + Playwright + HTTP patch
- `trade_gate.py` — Entry gate (EV, coinflip, probability)
- `entry_parity.py` — Adaptive threshold (strictness from loss_streak)
- `exit_policy.py` — Exit rules (hold-to-expiry, hard_adverse_flush)
- `config.py` — Settings via env (loads with override=True!)
- `env/runtime.public.env` — Runtime knobs
- `backtest.py` — Backtester
- `db_config.py` — DB schema (MariaDB port 3400)
- `dashboard_server.py` — API + process manager
