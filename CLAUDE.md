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
- `ENTRY_ORDER_MODE=LIMIT_FAK` (instant market order with limit price protection)
- `MIN_EDGE=0.12`, `MIN_EXPECTED_ROI=0.150`
- `PAPER_ENTRY_START_SEC=80`, `PAPER_DOWN_ENTRY_END_SEC=200`
- `PAPER_MIN_BOUNDARY_DIST_PCT=0.020`, `PAPER_DOWN_MIN_BOUNDARY_DIST_PCT=0.030`
- `PAPER_DOWN_MIN_ENTRY_PRICE=0.35`, `PAPER_MAX_ENTRY_PRICE=0.58`
- `PAPER_PERF_PAUSE_SEC=0` (disabled)
- `LIVE_MAX_DRAWDOWN_STOP_PCT=1.0` (disabled)
- `LIVE_ENTRY_START_SECONDS=80` (synced with paper)
- `DRY_RUN` is NOT in env file — dashboard controls via env_overrides
- **Technical filters**: BB Extreme (|bb|>0.5), VWAP Agree, Ask Drift <=0.08

## Critical Rules — DO NOT BREAK
- **Never set `DRY_RUN=true` in runtime.public.env** — config.py override=True overwrites dashboard's Start Live
- **Never use GTC orders for entry** — Polymarket POST /order takes 1-23s, use FOK
- **Backtest = Paper = Live entry conditions must be IDENTICAL** — same jury, same guards, same gate, same prices. If any one differs, performance diverges and backtest results are meaningless
- **Paper and Live must read from signal_cache** — running separate judges causes divergence
- **entry_gate must use signal_cache prices (_btc_now/_btc_start)** — NOT ctx.current_binance_price (WebSocket has $50-200 offset)
- **Price guards run in data_collector only** — Paper/Live read guards_passed, no re-checking
- **Paper must NOT run its own guards when guards_passed is available** — causes Paper to block trades that Live enters (or vice versa)
- **Live must respect signal_cache.gate_allow** — Live should not bypass gate when Paper is blocked
- **Live must SKIP adaptive parity guards when gate_allow=1** — data_collector already validated. Live-specific loss_streak/strictness must NOT override signal_cache decisions
- **Paper must align to Live, not the other way around** — Live is real money. Paper/backtest match Live's entry logic. Live's safety guards (FOK price limit, kill-switch) are the only Live-only additions
- **FOK must use limit-price** — max reference_ask + $0.05. Prevents slippage (was 0.61→0.79)
- **Settlement outcome must use Gamma API finalPrice** — btc_ticks can differ $5-10 from Chainlink oracle. In close calls this flips UP/DOWN
- **py_clob_client HTTP must be patched** — default HTTP/2 + 5s timeout = ReadTimeout. Patch: HTTP/1.1, 45s, retries=3
- **All processes poll at 0.1s** — data_collector, paper, live. Rate limit 150 req/s, we use ~20
- **Duplicate orders → uncertain_fill** — never assume full fill on "duplicated" error
- **GTC cancel fail → skip FOK** — prevents double position
- **After ANY git revert/reset/rebase** — MUST run verification before commit:
  ```
  grep "BET_PCT_MIN\|BET_PCT_MAX" risk_manager.py main.py backtest.py
  grep "timeout_seconds=" polymarket_client.py
  grep "conf_norm\|conv_norm" risk_manager.py main.py
  grep "import os" polymarket_client.py
  ```
  Expected: MIN=0.10, MAX=0.15, timeout=2.0, conf_norm everywhere, import os present
- **Never mix multiple file changes in one revert** — revert file-by-file, verify each
- **NEVER use `git checkout <commit> -- <file>`** — overwrites entire file, kills all later changes. Copy needed code manually instead
- **Post-restore verify data_collector.py**: `grep "_check_parity_mismatch\|COALESCE(stake\|OUTCOME ADJUSTED" data_collector.py` — all must exist

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
- **Unicode em-dash/arrows/box-drawing in logger** — Windows cp949 can't encode → crash kills Live order flow. NEVER use non-ASCII in logger messages (—, ─, →, ≈, ⚠️). Use ASCII only (--, -, ->, ~, [!])
- **Missing `import os` in polymarket_client.py** — caused MAKER_FIRST drift check to crash, blocking all Live orders
- **Operator precedence in guards_passed check** — `not X if cached else True` bug
- **CLOB thin-book phantom edges** — judges saw asks=(0.08/0.93), normalized when sum>0.25
- **Polymarket page freeze** — auto-reload after 15s stale price
- **Revert damage** — git revert/reset can silently undo changes in OTHER files. ALWAYS verify key values after any revert: `BET_PCT_MIN=0.10, BET_PCT_MAX=0.15, timeout_seconds=2.0, conf_norm formula`
- **Sizing formula mismatch** — risk_manager used edge*confidence, main.py used confidence-only. Must be identical: `conf_norm = (confidence - 0.3) / 0.7`
- **Settlement used Binance price** — caused wrong outcome ($52 diff). Must use btc_ticks (Chainlink calibrated) or Gamma API finalPrice
- **`direction` NameError in main.py VWAP filter** — used bare `direction` instead of `decision.direction`, crashed live every tick
- **`_lag_dir`/`_lag_ep` undefined in dead else-branch** — _GateProxy referenced variables from removed lag_arb path
- **Chainlink RPC stuck on dead endpoint** — `_init_web3()` didn't rotate `_rpc_idx` on failure, retried same dead URL forever
- **polygon.llamarpc.com DNS failure** — removed from RPC list, added polygon-rpc.com
- **Chainlink stale warning log spam** — fired every 0.1s tick, rate-limited to 30s
- **Settlement verification too short** — 15/30s retries missed slow Polymarket API, extended to 15/30/60/120s

## Data Collection
- `btc_ticks`: price, volume, buy_volume, sell_volume (Binance "m" flag)
- `poly_odds`: up/down bid/ask/mid/spread/overround (0.1s)
- `signal_cache`: direction, guards_passed, btc_move_pct, buy_sell_ratio, binance_rtds_gap, gate_allow/ev/reason, bb_pos, vwap_agree, ask_drift
- Buy/sell volume bias in judges: DISABLED (backtest showed negative PF impact)

## Current Strategy (2026-04-06)
- **Entry**: judges direction + signal_cache gate_allow + BB/VWAP/drift filters
- **Technical filters (2026-04-06)**:
  - BB Extreme: only enter when |Bollinger Band position| > 0.5 (1s tick, 60-tick window)
  - VWAP Agree: price must be above/below anchored VWAP matching bet direction
  - Ask Drift <= 0.08: skip when CLOB ask rose >8 cents from window start (edge gone)
  - ENTRY_START=80s: 60-80s entries had 35% WR (CLOB unstable), 80s+ = 59% WR
- **Entry mode**: LIMIT_FAK via Rust binary (fallback Python). Rust v2: correct EIP-712 signing, normal exchange (not neg-risk)
- **Exit**: hold-to-settlement (all early exits disabled)
- **Sizing**: FIXED flat (Paper $100, Live = seed*LIVE_FIXED_SEED_PCT). No mega multiplier.
- **Filters**: MIN_EDGE=0.12, ROI=0.150 (time-weighted: 0.06 at 60s remaining)
- **Price range**: ask 0.35-0.58, spread < 0.20, opposite ask < 0.78
- **Orderbook**: WebSocket primary (wss://ws-subscriptions-clob.polymarket.com/ws/market), REST fallback
- **paper_replay (BB+VWAP+drift+start80)**: 480h 573t 54% PF1.39 +$10,312

### Strategy Sweep Results (2026-04-02, $10 fixed, 20 days)
- baseline (no filter): 489t 54.4% PF1.29 +$652
- conf>=0.7: 273t 56% PF1.45 +$532
- conf+btc: 228t 57% PF1.48 +$469
- **score>=3: 301t 56% PF1.46 +$607 (BEST across all periods)**
- Pure momentum 0.10%+20s: 42t 62% PF1.62 (too few trades)
- Judge min_edge tuning: no effect (gate is the bottleneck)

### Technical Filter Sweep Results (2026-04-06, paper_replay $100 fixed, 480h)
- **BB+VWAP (no drift)**: 785t 53% PF1.36 +$13,334
- **BB+VWAP+drift0.08**: 690t 53% PF1.34 +$10,985
- **BB+VWAP+drift0.08+start80**: 573t 54% PF1.39 +$10,312 (CURRENT)
- BB+VWAP+score5: 755t 53% PF1.38 +$13,385
- BB+VWAP+score7: 310t 57% PF1.43 +$5,694 (high PF, low PnL)
- BB+VWAP+CVD: 556t 53% PF1.37 +$9,588
- BB+VWAP+vel0.6: 443t 51% PF1.37 +$8,036
- BB+VWAP+volsurge1.5: 394t 49% PF1.34 +$6,829

### Indicators Tested & Rejected (2026-04-06)
- **Efficiency Ratio**: 480h PF 0.87 (WORSE - zigzag is normal in 5min binary)
- **Immediate Momentum (10s)**: PF 1.31 (worse - 10s is noise)
- **Peak Retracement**: all thresholds worse than baseline
- **BTC 15min confirm**: PF 1.33 (worse - filters good trades too)
- **ETH correlation**: PF 1.33 (marginal, not worth complexity)
- **CLOB Exit (hold-to-settlement vs sell)**: all worse (bid already near 0 when CLOB warns)
- **CLOB Mismatch Exit**: all worse (same bid-near-0 problem)
- **CLOB Velocity filter**: all worse at 480h
- **Volatility regime**: 0.08-0.10% block = small sample, not reliable
- **Contrarian volume**: PF 2.03 at 120h but PnL -28% (too few trades)
- **Underreaction filter**: worse at 480h (CLOB catches up by 80s)
- **Time-of-day**: insufficient sample per hour

### BTC vs CLOB Correlation Analysis (2026-04-06)
- **CLOB prediction at 10s remaining: 92% accurate**
- CLOB is leading indicator: market makers have direct Chainlink node access
- BTC UP + CLOB DOWN at 270s -> CLOB right 64% (reversal signal)
- **CLOB Underreaction = our edge**: BTC 0.03%+ moved, CLOB ask<0.50 = 100% WR (24 windows)
- But this state rarely exists at 80s+ (CLOB catches up), so can't be used as hard filter
- Contrarian volume: vol disagrees with bet -> 60% WR (vs agrees 54%)

### Live Loss Pattern Analysis (2026-04-06, 28 losses / 72h)
- 82% had CLOB warning (opp>0.65) at 10s remaining
- 54% were coin flips (BTC final move < 0.03%)
- 36% had BTC reversal (peak then fade)
- 32% entered at ask >= 0.55 (bad risk/reward)
- 29% were early entries (60-80s) -> fixed by START=80s
- Entry timing: 60-80s=35% WR, 80-100s=62%, 150-200s=64%
- Entry price sweet spot: 0.45-0.55 (58-61% WR), 0.40-0.45=43%, 0.55-0.60=54%

### Key Learnings (2026-04-02)
- Judge min_edge 0.018->0.10: NO effect (gate MIN_ROI already filters)
- Gate time-weighted MIN_ROI: minimal effect (+12 gate_allow entries)
- CLOB WebSocket: works perfectly, 15s = hundreds of messages
- BTC 5min is NOT neg-risk (neg_risk=false) — Rust was using wrong exchange address
- Oracle lag ~55s: CLOB repricing takes ~55s after Chainlink update
- CLOB is LEADING indicator: DOWN spike to 0.80 preceded BTC drop by 1-2s
- Polymarket "Price To Beat" changed to capital T/B — PTB scraper case-sensitivity fix
- Ankr RPC requires API key since 2026-04 — removed from Chainlink RPC list
- conf>=0.7 trades: 56.9% WR vs conf<0.7: 50.5% WR (meaningful difference)

### Previous Settings (rollback reference)
- 2026-03-29: ENTRY_START=45s, BOUNDARY=0.010/0.030, MAKER_FIRST
- 2026-04-01: conf2x (2x when conf>=0.7), s80, ask0.50
- ENTRY_START=90, BOUNDARY=0.020/0.030 → 72h 81t(1.1/h) +$401
- prev3x (3x when prev momentum): 20d +$5,015 but unstable (48h +$72)
- PROFIT_TAKE_ENABLED=true, offset=0.10 → hold better for settlement

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
- Last validated (48h, 2026-03-25): 39 trades, 66.7% WR, +$1,891, PF=2.00
- Settings: start=90s, boundary=0.020/0.030, edge=0.08, roi=0.030
- Sweep winner (24h): s90+bd02 = 28t 71% +$1,458 PF=2.28
- Sweep winner (48h): s60+bd02 = 77t 75% +$5,198 PF=2.63
- Simple momentum (no judges): 180s+0.04% = 65t 90.8% +$10,010 (but unrealistic CLOB pricing)

## Known Parity Issues (must fix)
- **backtest.py runs its own guards** — different from data_collector's guards_passed
- **Paper has self-guards that override signal_cache** — implied-side, trend-align checked separately
- **data_collector guards_passed is often 0** — momentum + trend too strict, blocks everything
- **Live bypassed guards in some code paths** — traded when Paper couldn't
- **entry_gate called with different prices** — Paper uses DB odds, Live uses CLOB cache

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

## Database Connection
- **Credentials in `.env.secrets`** (not `env/` dir, project root)
- config.py loads `.env.secrets` via dotenv — must be loaded before DB calls
- Port 3400, password in `.env.secrets` (MARIADB_PASSWORD)
- If `connect_db()` fails with auth_gssapi_client, `.env.secrets` not loaded
- `dashboard_server.py` — API + process manager
