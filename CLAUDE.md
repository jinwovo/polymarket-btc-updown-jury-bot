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
- `MIN_EDGE=0.08`, `MIN_EXPECTED_ROI=0.030`
- `PAPER_ENTRY_START_SEC=90`, `PAPER_DOWN_ENTRY_END_SEC=200`
- `PAPER_MIN_BOUNDARY_DIST_PCT=0.020`, `PAPER_D OWN_MIN_BOUNDARY_DIST_PCT=0.030`
- `PAPER_DOWN_MIN_ENTRY_PRICE=0.30`, `PAPER_MAX_ENTRY_PRICE=0.70`
- `PAPER_PERF_PAUSE_SEC=0` (disabled)
- `LIVE_MAX_DRAWDOWN_STOP_PCT=1.0` (disabled)
- `DRY_RUN` is NOT in env file — dashboard controls via env_overrides

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

## Data Collection
- `btc_ticks`: price, volume, buy_volume, sell_volume (Binance "m" flag)
- `poly_odds`: up/down bid/ask/mid/spread/overround (0.1s)
- `signal_cache`: direction, guards_passed, btc_move_pct, buy_sell_ratio, binance_rtds_gap, gate_allow/ev/reason
- Buy/sell volume bias in judges: DISABLED (backtest showed negative PF impact)

## Current Strategy (2026-03-28)
- **Entry**: judges direction + signal_cache gate_allow + auto_defense(3t2w) + accel
- **Entry mode**: MAKER_FIRST (0% fee entry, 2s GTC → FAK fallback)
- **Exit**: Dynamic profit-take at **entry_price + 0.10** (mid-trade exit)
  - Fallback: settlement if target not reached
  - near_certain_win still active (opp_ask <= 0.05)
  - hard_adverse_flush still active (roi <= -70%)
- **Sizing**: adaptive 10-15% of seed capital, conf-based
- **Filters**: MIN_EDGE=0.08, ROI=0.030, BOUNDARY=0.020/0.030
- **Price range**: ask 0.30-0.58, opposite ask < 0.65
- **Backtest**: 120h 122t, 80% mid-exit success, +$2,277 (with fees)
- **To revert**: set LIVE_PROFIT_TAKE_ENABLED=false, ENTRY_ORDER_MODE=MARKET

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
- `dashboard_server.py` — API + process manager
