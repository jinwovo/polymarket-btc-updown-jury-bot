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
- **ETH5 odds flapping/"--" (fixed 2026-08-13) — THREE stacked bugs in extra-markets path:**
  1. **poly_odds PK collision erased ETH rows (ROOT CAUSE, live since SOL5 launch 8/10).**
     PK is (ts, window_start) WITHOUT slug; eth-updown-5m and sol-updown-5m share identical
     window_start values and Windows time.time() ticks at ~15.6ms, so same-tick writes collide
     and `ON DUPLICATE KEY UPDATE slug=VALUES(slug)` rewrote eth rows into sol rows -- committed
     eth rows literally vanished seconds later (sol is last in _EXTRA_MARKETS, so sol always
     survived). Fix: per-market ts offsets `_MKT_TS_OFFSET` (1-3ms). Clean fix pending: ALTER
     PK to (ts, window_start, slug) offline. Fingerprint: COUNT(*) for a slug DECREASES.
  2. **WS resubscribe-on-reconnect missing**: `_subscribed_tokens` persisted across reconnects,
     so a reconnected socket never re-sent subscriptions; server idle-closes unsubscribed conns
     in ~10s -> infinite 10s reconnect loop, WS leg dead after ANY disconnect. Fix: clear
     subscription state per connection. ALSO: server 1008s ("invalid subscription payload")
     multi-message subscribes -- batch ALL tokens into ONE message per connection; on new-window
     tokens, deliberately reconnect (subscribe-once semantics).
  3. **Stale-token stomp + no empty-book guard**: rotated-out windows' settle events
     (asks=[] -> ask=1.0/bid=0.0) were written under the CURRENT slug. Fix: tag _token_map with
     window ws + ignore mismatches; skip ba<=0/ba>=1.0 events (same guard as REST path).
  Silent-failure lesson: all these paths logged at DEBUG (invisible at INFO). Poll/discover
  failures now WARN throttled (every 15th) + "no odds rows for 30s" watchdog warning.
  Data damage note: eth-updown-5m poly_odds rows 8/10~8/13 have holes (erased by #1).
- **RTDS silent stream stall** — socket stays open but price messages stop; `_rtds_price_loop` had no stall detection (recv timeout just sent text "PING" and waited), so reconnect only happened at the RTDS server's exactly-2h connection lifetime. Caused 47-min Playwright fallback 2026-08-10 and the 7/29 "12h RTDS outage" (log fingerprint: reconnects at exact 2h intervals). Fixed 2026-08-10: 30s no-price stall watchdog forces reconnect. Note: "Price MISMATCH" warnings with diff $10-30 while RTDS is alive are Playwright scrape lag noise, not a fault

## Data Collection
- `btc_ticks`: price, volume, buy_volume, sell_volume (Binance "m" flag)
- `eth_ticks` / `sol_ticks`: 1s buckets, Chainlink-calibrated (RTDS-primary, PTB-offset fallback)
- `poly_odds`: up/down bid/ask/mid/spread/overround (0.1s) — slugs btc-updown-5m/15m, eth-updown-5m, sol-updown-5m
- **SOL5 collection added 2026-08-10** (collection ONLY, no signal generator/trading yet):
  `_EXTRA_MARKETS` sol-updown-5m + `_sol_ws` (solusdt@trade) + RTDS "sol/usd" branch + PTB scrape.
  RTDS probe 2026-08-10: feed carries btc/eth/sol/bnb/xrp/doge/zec/hype at ~1msg/s each, `symbol`
  field ALWAYS populated -> classification is symbol-based; price-range fallback is dead code
  (ETH fallback band tightened 100->1000 so SOL/BNB/ZEC can never leak into ETH calibration).
  PTB calibration in `_scrape_extra_ptb` is now gated per market label (was: any 100<p<10000
  applied to ETH offset -- would have corrupted ETH the day SOL crossed $100).
  After ~2-4 weeks of data: validate SOL5 with the ETH5 direct recipe (tick-based replay), then
  clone signal_generator_eth5 -> signal_generator_sol5 if it passes the all-months bar.
- `signal_cache`: direction, guards_passed, btc_move_pct, buy_sell_ratio, binance_rtds_gap, gate_allow/ev/reason, bb_pos, vwap_agree, ask_drift
- Buy/sell volume bias in judges: DISABLED (backtest showed negative PF impact)

## PnL Levers, Ranked (2026-07-04 analysis)
1. **UPTIME — dominant lever.** Collector coverage: Mar 65%, Apr 96%, May 17%, Jun 7%, Jul 1%.
   May-Jun PnL was earned in 7-17% of the month. Full-month extrapolation: $1,000+/mo at $10
   stake (vs Apr actual $244 at 96%). Everything else is second-order until the stack runs 24/7.
   (Caveat: partial-month hours may be biased toward active/volatile sessions.)
2. **Stake scaling.** Validated edge ~25-35% avg ROI/trade (PF 1.7-2.3). $10 -> $50-100 is a
   direct multiplier IF slippage holds — scale stepwise ($25 -> $50 -> $100), watch realized
   fill price vs signal ask at each step. LIMIT_FAK price cap already bounds worst case.
3. **Multi-account** (infra already built) — parallel capital once single-account slippage is known.
4. NOT levers (tested, rejected): maker-first entry, gap arb, volume ratio, BTC15 (see below).

## OOS Validation + Param Update (2026-07-04)
- **Method**: replay signal_cache_log directly (`oos_replay_signal_cache.py`) — exact values
  paper/live read at runtime, unlike paper_replay_direct.py which re-derives BB/R2 from ticks
- **Direct-trigger strategy VALIDATED out-of-sample** (tuned on Mar-Apr, tested May-Jun):
  - BTC5 baseline: Apr 145t/PF1.29, May 62t/PF1.58, Jun 39t/PF1.74 — held up, no decay
  - ETH5: Apr-Jun 28t / 75% WR / PF 3.07 — stable but low volume (0.3 t/day)
- **Params updated** (each knob improved ALL months independently; sweep selected on May-Jun,
  cross-validated on Apr):
  - `PAPER_MAX_ASK_DRIFT` 0.05 -> 0.08
  - `PAPER_DIRECT_R2_MIN` 0.05 -> 0.10
  - `PAPER_ENTRY_START_SEC` 80 -> 100 (also fixes drift: live was already 100)
  - Combo result: Apr $+244/PF1.51, May $+201/PF2.06, Jun $+119/PF2.32 ($10 stake)
- **Parity bugs fixed (2026-07-04)**: main.py + live_eth5.py direct-mode BB band was
  HARDCODED 0.3-1.5 while paper read env (0.9-2.5 / 0.7-2.5). Live now reads same env vars.
- **Known residual gap**: direct mode never enforces PAPER_MAX_ENTRY_PRICE (adaptive_max_ask
  check skipped). OOS data says ask>0.55 entries were mildly profitable (63.5% WR, +$40/63t),
  so left as-is — but paper_replay_direct.py DOES cap at 0.55, so its results diverge from sim.
- Entry price band [0.40,0.45) is weak: 41.9% WR / 31t (Apr-Jul). Watch, small sample.
  (2026-08-01: tested excluding it -- costs Apr -$25, May +$8. Not actionable as a filter.)

## Dup-Collector Incident (2026-07-29) + Fix (2026-08-01)
- First watchdog session ran 2+ data_collector processes at once: watchdog spawned one at
  11:14:51, paper_trade_sim auto-started another 3s later (btc_ticks stale during warmup).
  signal_cache id=1 flapped between writers with different BB rolling-window state (same
  price, bb -1.6 vs +1.65 within 0.4s). Paper read the out-of-band writer at all 3 windows
  that passed the full entry funnel that day -> 0 trades in 12h. Fingerprint:
  signal_cache_log 2.11 rows/s vs 0.58 single-writer baseline; two interleaved bb series.
- RTDS was ALSO down the entire 12h session (Playwright fallback, calibration offset swung
  $59-80, one 5-min freeze) -> guards_passed rate 0.6% vs April 2.1%. Root cause SOLVED
  2026-08-10: silent stream stall + no client-side stall detection (see Bugs Found & Fixed).
- **Fixes applied**: `PAPER_AUTO_START_COLLECTOR=false` (watchdog owns the collector);
  data_collector.py `_exit_if_duplicate()` psutil singleton -- first-starter-wins, newcomer
  exits 0 so run_collector wrapper stops cleanly (kill-the-other would make wrappers duel).
  Old `_kill_existing` was pgrep-based = silent no-op on Windows.
- Healthy-state checks after restart: signal_cache_log ~0.5-0.6 rows/s (single writer),
  BTC5 paper 1-3 trades per 12h.
- **Rate fingerprint OBSOLETE since RTDS stall fix (2026-08-13 measurement)**: single
  confirmed writer now logs ~2.0 rows/s (RTDS actually alive = more evaluations; the old
  0.58 baseline was measured while RTDS silently stalled). Rows/s can no longer separate
  single vs dual writer — check PROCESS COUNT (psutil: exactly one data_collector.py;
  note an in-paper embedded collector would show as paper_trade_sim.py) and bb-interleave
  instead. 8/10+8/12 data checked: no dup evidence (flap-rate same as single-writer day).

## Param Sweep on signal_cache_log (2026-08-01, Apr-01 ~ Jul-05, $10 stake)
- Sweep engine reproduces oos_replay_signal_cache.py to the cent before sweeping
  (BTC5 base 217t/$+560/PF1.73; ETH5 base [guards off] 124t/$+370/PF1.97).
- **BTC5: current params CONFIRMED optimal.** None of 37 single-knob/combo configs beat
  base in all months. Near-misses are Apr-concentrated (e.g. dend240+bb0.7 = $+656 total
  but Jun PF 2.32->1.87). Inert knobs found: DOWN_MIN_PRICE 0.30-0.40 (no surviving DOWN
  entries below 0.40), R2_MIN_FILTER (DIRECT_R2_MIN dominates in direct mode).
- BTC5 guards_off: 3x trades (660t) at same total PnL, PF 1.73->1.20. NOT adopted --
  capacity lever only if stake scaling ever saturates per-window size.
- **ETH5_DOWN_ENTRY_END_SEC 200 -> 240 ADOPTED**: PnL up in EVERY month
  (Apr $209->238, May $31->48, Jun $131->158; total $370->$451, PF 1.97->2.03, 124->146t).
- ETH5 quality option (NOT adopted; vs dend240-only May PnL -$2.34): dend240 + BB_MIN 0.7
  = 123t/$+484/PF2.51, beats base on PnL AND PF in every month at flat volume. Flip
  ETH5_BB_MIN_ABS to 0.7 if live slippage at higher stakes demands more per-trade edge.

## BB Artifact — DO NOT "FIX" (2026-08-10)
- **BTC5 runtime BB is NOT a 60s Bollinger.** data_collector appends btc_price_adjusted to
  _recent_prices per Binance @trade message (tens/s), so the 60-item window = 0.6-6s. With RTDS
  alive (1s step prices) bb collapses to quantized artifacts: exactly +-3.84 (1 fresh value in
  window), +-2.69 (2), +-2.18 (3), or None (std=0). Artifact rate 6-9.5% of rows Apr-Aug, None 13-30%.
- **A/B replay verdict (same funnel, only bb swapped, Apr-Aug)**: logged-bb 0.9-2.5 = 220t/$+575/
  PF1.74; true tick-BB (paper_replay formula, 60 btc_ticks rows ~= 25-37s) 0.9-2.5 = 172t/$+482;
  every true-BB band loses in at least one month; no-BB = $+289/PF1.09 (Jul -$65). Baseline
  reproduced documented OOS numbers to the cent (Apr $244/May $201/Jun $119).
- Why the junk works: bb!=None and |bb| in [0.9,2.5) accidentally requires "Chainlink printed
  2+ fresh levels within the last ~1-3s AND trades flowing" = micro-momentum-continuation
  detector. None = flat price (no continuation), +-3.84 = single lone step. Bug became a feature.
- ETH5/BTC15 signal generators load prices from DB ticks (60 rows ~= 30s) — different, saner
  semantics; leave unchanged. paper_replay.py BB differs from runtime BB — KNOWN and ACCEPTED:
  sweeps on signal_cache_log (logged bb) are the source of truth for BTC5 entry tuning.

## New-Strategy Calibration Scan (2026-08-10) — all REJECTED, don't re-test w/o new mechanism
- Method: poly_odds sampled per 15s mark x side x 0.05-ask-bin, Apr-Aug (10.6k BTC5 + 7.4k ETH5
  windows), then one-entry-per-window normalization + ask+3c slippage column. Key lesson: raw
  cell tables produce "+EV all months" mirages via bucket-overlap double counting — always
  normalize to one trade per window before judging.
- **Late favorite buy** (elapsed 240-300s, ask 0.70-0.95): WR == implied everywhere, EV ~ $0.
  The CLOB is perfectly calibrated at window end. ("92% accurate at 10s" is real AND priced.)
- **Final-15s longshot** (ask ~0.05): realized 7.9% vs implied 5.2% — real mispricing, UNEXECUTABLE
  (POST latency, book depth; +3c slip = -$2/t). April-concentrated.
- **Cheap-side fade** (ask 0.20-0.40 any elapsed, both markets): normalized result = April
  bear-regime artifact (ETH5 DOWN Apr +$1,336, May/Jun/Jul all negative; UP mirror -$2,652 —
  directional regime bet, not structure). Slippage-adjusted negative EVERY month.
- **Mid-window favorite** (ask 0.75-0.95, elapsed 75-240s): +$0.13-0.43/t pre-slip, ~$0 after
  +3c slip. Same underreaction the direct trigger already harvests at better prices.
- **BTC->ETH lead-lag entry** (BTC |move|>=0.03-0.08% at 90-240s -> buy ETH side ask<=0.55/0.60):
  negative in ALL configs (April strongly negative). Rejected.
- BTC15: only 3 days of path_r2 data since the 7/04 fix — still untestable, keep collecting.
- Only untested expansion: SOL5 + other alt 5m markets — needs collector support first
  (no SOL ticks/odds collected; liquidity viable per 2026-07-12 scan).
  [SOL5 collection went live later the same day — see Data Collection section.]

## Trade Tape (2026-08-10) — public wallet-level fills, two live leads
- **Infra**: `scripts/tape_harvest.py` (data-api /trades + Gamma resolutions ->
  poly_trades/tape_windows; server-side, uptime-independent, resumable, newest-first;
  14d BTC5 = 4,032 windows / 7.5M prints in ~25min). `scripts/tape_leaderboard.py`
  (recency-decay leaderboard hl=5d + walk-forward mirror + lottery study).
- **LEAD 1 -- late-lottery: REJECTED after fill simulation (adverse selection).**
  Taker-BUY fills at <=0.05 in final 30s DO earn +20.3% ROI (58K fills, p<=0.02 +25.3%)
  -- but that flow is 16.5M shares of taker BUYS vs only 1.16M taker SELLS: it is a
  final-seconds ASK-SNIPING speed game (real-time info vs stale salvage asks), exactly
  what our 0.2-23s POST latency cannot play. The passive version (resting GTC bids
  both sides, price-priority sim on real SELL prints, grid p=0.02-0.05 x S=25-200sh):
  **-44% to -53% ROI at EVERY combination** -- dumps that reach penny bids happen in
  truly-decided windows (filled-share win rate 1.0-2.6% vs 2-5% breakeven). Do not
  revisit either leg without sub-second execution.
- **LEAD 2 — specialist mirror: VALIDATED on paper (30d, 23 walk-forward test days)**.
  Wallet archetypes: HFT grinders (every window, entry ~11s, 10-14 tr/win, $34-50 clips
  — UNMIRRORABLE scalping) vs specialists (10-30 win/day, entry ~120s median, px
  0.36-0.61, $200+ clips — mirrorable at +3-5s). Walk-forward top-5 specialists-only
  (pre-registered filter: <=210 win/7d, med clip>=$100, med entry>=60s, active<=48h;
  selection strictly pre-day, UTC-aligned, +2c slip, $10):
  **720t / 57.2% WR / +$1,116 / EV +$1.55/t (+15.5%)**, 14/23 days positive,
  ALL 4 week-buckets positive (+$206/+$256/+$433/+$222), ~31 signals/day.
  (All-wallets baseline was +$0.26/t — the specialist filter IS the edge.)
  Remaining risk: fill realism (+2c modeled; real = poll data-api ~1.5s -> FAK at ask).
  Next step: PAPER mirror runner (poll tape, log simulated entries, measure real
  latency/slippage vs modeled) before any live deployment.
- Top of tape (7d, real): False-Military +$56K/109win/63.8%, anon 0x0cb0 +$50K (grinder).
- Pitfall log: data-api returns DESC — page cap drops EARLIEST trades of busy windows
  (MAX_PAGES=4). Mint/merge sellers create phantom SELL-only positions -> excluded via
  negative-balance check (113K wallet-windows). Walk-forward day boundaries must be UTC
  (local-tz day_start leaks 9h of test day into selection = lookahead).

## BTC5 Direct Trigger SILENT Post-RTDS-Fix (2026-08-13) — diagnosis COMPLETE
- Replay of 8/10-8/13 signal_cache_log (394 windows, collector up): **0 BTC5 entries**;
  ETH5 (guards-off, matching runtime): 3 entries 2W1L +$4.34 = normal for its 1.5/day rate.
- **The $60 binance_rtds_gap is REAL and HARMLESS — it is the USDT/USD basis, not a bug.**
  Verified externally 8/13: Binance BTCUSDT $63,656 vs BTCUSDC $63,597 = +0.092%;
  Kraken USDT/USD 0.9989; ETH gap identical +0.093% (quote-currency effect, hits all
  USDT pairs equally). April gap $10 = USDT near-peg. Collector's offset calibration
  absorbs it; "divergence guard" is boundary-distance, NOT a gap check — earlier note
  blaming the gap was wrong. Watch: if USDT re-pegs, gap shrinks by itself.
- **Real causes of 0 trades (guard autopsy, production evaluate_market_guards re-run
  offline on 4,140 Aug full-filter survivor rows — reproduces logged 27 pass rows):**
  1. **Dominant: opposite_too_high (~60%+ of fails, opp ask 0.83-0.97).** Crash-regime
     vol -> CLOB prices windows to 0.9+ by 100-240s -> jury value-votes the cheap
     opposite side (edge-vote fades the favorite) -> guard correctly blocks. The edge
     state we harvest (move happened + book still 0.4-0.55 = underreaction) barely
     existed 8/10-8/13. April: opp asks clustered 0.66-0.74, pass rate 1.7%; Aug: 0.5%.
  2. **bb artifact lost selectivity** (RTDS fix): April entry-time rows 99.8% bb=None,
     in-band 0.03% (needle trigger); now 30.3% in-band = passes half of all windows.
     If guards were relaxed the recipe would fire ~50/day UNVALIDATED. Retune needs
     1-2 weeks of post-fix rows.
  3. Guards themselves behave the same as April (autopsy: same code, same thresholds).
- **auto_defense guard is a NO-OP (latent bug)**: it sets result.passed=False but the
  final `result.passed = (six _ok flags)` recomputation OVERWRITES it. Also its trigger
  state is frozen on April's last-3 paper trades. Do not "fix" silently — behavior
  change must be validated; documented here instead.
- Parity fix applied 2026-08-13: oos_replay_signal_cache.py ETH5 REQUIRE_GUARDS default
  1 -> 0 (paper_sim_eth5 runtime skips guards; replay was understating ETH5 trades).

## Specialist Mirror Update (2026-08-13) — re-implemented, extended OOS, PAPER RUNNER LIVE
- **Independent re-implementation** (`scripts/tape_specialist_mirror.py`, spec from docs):
  same 23 test days -> 580t / 52.6% / +$1,156 / EV +$1.99. Fewer trades than the 8/10 run
  (impl detail deltas) but SAME PnL — strategy-level confirmation, not knob-level.
  Baseline (no specialist filter) re-confirmed: 7,465t at EV +$0.10/t = noise.
- **Fresh OOS (8/10-8/13, harvested 8/13): FLAT/NEGATIVE — edge not confirmed on new days.**
  Uncapped: 130t -$164. Root cause of 8/10 (-$180): ALL 96 mirrors that day came from ONE
  wallet (top pick "Satisfied-Peripheral") that broke profile and traded ~every window;
  filter only dropped it the NEXT day. Single-wallet concentration = the structural risk.
- **Wallet-day activity cap 30 adopted** (profile-derived from specialist archetype 10-30
  win/day, not tuned): full-period 603t / EV +$1.75 (vs +$1.54 uncapped), 8/10 loss
  -$180 -> -$61. Cap counts the wallet's observable window entries today (no lookahead).
- **Entry-price bands (full 27d)**: px<0.30 mirrors carry the PnL — 116t / 31.9% WR /
  +$720 / EV +$6.21 (fresh days +$0.54/t on 12t). 0.60+ band NEGATIVE (-$1.03/t).
  Candidate refinement for the runner phase; do NOT hard-code yet (post-hoc risk).
- **ETH5 tape harvested (30d, 8,641 windows, 3.3M prints): NO specialist edge.**
  412t / 76.7% WR / EV -$0.19/t — pool is favorite-buyers, WR==implied, fee eats it.
  BTC5-specific edge. Do not mirror ETH5.
- **`scripts/mirror_paper_runner.py` RUNNING since 2026-08-13** (hidden background proc):
  polls data-api for current window ~1.2s, detects chosen-specialist first BUYs, logs
  simulated entries to `mirror_paper_trades` with REAL detection latency + REAL book ask
  (from poly_odds, <5s fresh) vs modeled px+2c. Re-harvests + re-selects at UTC rollover.
  Startup selection takes ~3-4 min (40d GROUP BY). Log: bot_mirror_paper.log.
  GO/NO-GO after ~1-2 weeks: compare realized sim entry vs modeled +2c, latency
  distribution, and whether EV stays positive at realized fills.

## Current Strategy (2026-04-11)
- **Entry**: judges direction + signal_cache gate_allow + BB/VWAP/drift filters
- **Technical filters (2026-04-11)**:
  - BB Extreme: only enter when |Bollinger Band position| > 0.5 (1s tick, 60-tick window)
  - VWAP Agree: price must be above/below anchored VWAP matching bet direction
  - Ask Drift <= 0.08: skip when CLOB ask rose >8 cents from window start (edge gone)
  - ENTRY_START=100s: parity sweep confirmed 100s optimal (PF2.51 @240h)
  - MAX_ENTRY_PRICE=0.50: sweet spot (PF2.29 @168h, PF2.51 @240h, +$5187)
- **Entry mode**: LIMIT_FAK via Rust binary (fallback Python). Rust v2: correct EIP-712 signing, normal exchange (not neg-risk)
- **Exit**: hold-to-settlement (all early exits disabled)
- **Sizing**: FIXED flat (Paper $100, Live = seed*LIVE_FIXED_SEED_PCT). No mega multiplier.
- **Filters**: MIN_EDGE=0.12, ROI=0.150 (time-weighted: 0.06 at 60s remaining)
- **Price range**: ask 0.35-0.50, spread < 0.20, opposite ask < 0.78
- **Orderbook**: WebSocket primary (wss://ws-subscriptions-clob.polymarket.com/ws/market), REST fallback
- **paper_replay (BB+VWAP+drift0.08+start100+a50, no lag_arb)**: 240h 93t 64.5% PF2.51 +$5,187
- **Gate stability lock**: gate_allow=1 held for 5s in data_collector/paper_sim/live (prevents missing short-lived signals)
- **Lag arb DISABLED**: paper_replay shows +$8.5K ideal but 300ms execution delay = 30% trades fail, WR drops to 51.6%. Not viable for live.

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

## Tested & Rejected: Gap Arb + Volume Ratio (2026-07-04, 3 months of data)

### Binance-RTDS Gap Delta — REJECTED as trigger AND as filter
- Analyzed Apr-Jun signal_cache_log (10,434 windows). gap_delta = gap - rolling_avg_60s.
- Pure direction signal: |gd|>=$8 -> 54-56% WR, |gd|>=$20 -> 58-65% WR (small n)
- **BUT CLOB already prices it in**: avg ask at signal time 0.52-0.61, EV mostly NEGATIVE
- Apparent +EV pocket at T=150s/|gd|>=8 does NOT survive sensitivity: T=165 flips to
  -$1.76/trade in May, T=180 all negative, T=120 June -$1.66. Zigzag = noise, not edge.
- As filter on existing entries: gap-agree trades WR 63.3% vs gap-DISAGREE 65.2% (worse!)
- **Do not revisit without a fundamentally different mechanism (e.g. sub-second execution)**

### Buy/Sell Volume Ratio — REJECTED again
- Standalone signal: bsr>=1.3 at T=90s -> 57.6% WR, BUT EV unstable by month
  (Apr +$0.36, May +$0.52, Jun **-$0.63** per $10 trade)
- As filter on entries: bsr-agree 62.7% WR vs bsr-DISAGREE 66.7% (contrarian confirmed again)
- Matches 2026-04-02 judge-bias rejection (PF 1.71 -> 1.26). **Closed.**

### Maker-First Entry — REJECTED (2026-07-04)
- Simulated on 217 OOS trades (poly_odds 0.1s book): post bid at entry, fill when ask <= limit,
  0% fee if filled, taker fallback at deadline. ALL variants (join/improve/undercut x 15s/30s
  wait) LOSE $150-175 vs pure taker (+$385~403 vs +$560).
- Cause: adverse selection. Winners run away (no fill -> pricier fallback), losers come back
  down and fill us. Momentum entries must TAKE. Fee saving (3%) < selection cost (~30% of PnL).
- Corrects earlier note "maker 0% fee = biggest unrealized opportunity" — FALSE for entries.

### BTC15 Market — NOT DEPLOYABLE yet (2026-07-04)
- Current judge-gate config: 3 trades / 3 months (gate blocks everything, like BTC5 pre-direct)
- BTC5 direct recipe (momentum): Apr 43% WR / PF 0.83 — 15min windows mean-revert, momentum LOSES
- Contrarian (fade overextended BB): unstable, May negative in every variant
- **signal_generator_btc15 never computed path_r2/p_pos (always NULL)** — fixed 2026-07-04,
  need ~1 month of new data before retesting r2-filtered variants. Keep collecting, don't trade.

### Complement Arbitrage (buy-both / mint-sell-both) — REJECTED (2026-07-04)
- Scanned 21.5M poly_odds ticks Apr-Jul: askSum<=0.99 or bidSum>=1.01 happens ~0.1% of ticks
- BUT median event duration = 0.1s (single tick). Events with dur>=2s AND edge>=2c: ~0 per month
- These are stale-quote blips during fast moves; POST /order takes 0.2-23s. Not executable.

### Live Fill Slippage (2026-07-04, 280 matched live trades, stakes $5-$22)
- fill vs signal ask: mean +0.7c, median 0c, 65% at-or-below signal, p90 +5c
- No stake-size effect visible up to $22. LIMIT_FAK cap works.
- Green light for stepwise stake scaling; $50+ still unmeasured — verify at each step.

### ETH5 Volume Expansion (2026-07-04)
- True-chain replay (guards NOT required for ETH5 — earlier 28t figure was over-filtered): 53t/+$210
- `ETH5_MAX_ENTRY_PRICE` 0.55 -> 0.60 + `ETH5_BB_MIN_ABS` 0.7 -> 0.5:
  107t / +$265 / PF 1.5/1.8/2.8 by month (all positive, each knob independently stable)
- Note: ETH5 score filter (ETH5_MIN_ENTRY_SCORE=2) is inert in direct mode — every candidate
  already scores >=2. ETH5 "momentum conflict vs BTC" filter is vacuous in direct mode
  (btc_move_pct column holds the ETH move for ETH5; direction is derived from the same value).

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
- ~~Live direct-mode BB band hardcoded 0.3-1.5 vs paper env 0.9-2.5~~ — FIXED 2026-07-04
  (main.py + live_eth5.py now read PAPER_DIRECT_BB_* / ETH5_BB_* env vars)
- **paper_replay_direct.py caps ask at 0.55 but paper_trade_sim direct mode has NO ask cap**
  (adaptive_max_ask skipped in direct mode) — replay understates sim's trade set
- **Live ETH5_DIRECT_MOVE_THRESHOLD default 0.04 vs env 0.05** — env always set, but defaults differ

## Polymarket API Notes
- GET /book: 1,500 req/10s (150/s)
- POST /order: 3,500 req/10s, response 0.2s-23s
- 425 "service not ready" = market not active yet after window start
- Min order size: 5 shares
- Maker = 0% fee, Taker ~3.15% at 50/50
- Heartbeat opt-in (not required)

## File Reference
### BTC 5min (existing)
- `main.py` — Live trading (reads signal_cache, places orders)
- `paper_trade_sim.py` — Paper trading (reads signal_cache, simulates)
- `data_collector.py` — CLOB polling + Jury + guards → signal_cache + extra markets data
- `paper_replay.py` — Backtester for BTC 5min

### BTC 15min (new, 2026-04-07)
- `signal_generator_btc15.py` — Jury + gate → signal_cache_btc15
- `paper_sim_btc15.py` — Paper trading → paper_trades_btc15
- `live_btc15.py` — Live trading → live_trades_btc15

### ETH 5min (new, 2026-04-07)
- `signal_generator_eth5.py` — Jury + gate → signal_cache_eth5
- `paper_sim_eth5.py` — Paper trading → paper_trades_eth5
- `live_eth5.py` — Live trading → live_trades_eth5

### Shared
- `judges.py` — 3 judges + Jury (asset-agnostic)
- `trade_gate.py` — Entry gate (EV, coinflip, probability)
- `exit_policy.py` — Exit rules (hold-to-expiry, hard_adverse_flush)
- `polymarket_client.py` — CLOB API + Playwright + HTTP patch + PTB scrape
- `market_config.py` — Multi-market definitions (BTC_5M, BTC_15M, ETH_5M)
- `config.py` — Settings via env (loads with override=True!)
- `entry_parity.py` — Adaptive threshold (strictness from loss_streak)
- `env/runtime.public.env` — Runtime knobs (BTC5 + BTC15_ + ETH5_ sections)
- `backtest.py` — Backtester (BTC 5min only)
- `paper_replay_multi.py` — Multi-market backtester (BTC 15min + ETH 5min)
- `db_config.py` — DB schema (MariaDB port 3400)
- `dashboard_server.py` — API + process manager + multi-market + multi-account

## Multi-Market Architecture (2026-04-07)
```
data_collector (single process)
  ├→ BTC WebSocket → btc_ticks
  ├→ ETH WebSocket → eth_ticks
  ├→ CLOB polling → poly_odds (all 3 markets via slug)
  ├→ Playwright PTB scrape → market_windows (all 3 markets, separate tabs)
  ├→ Jury + signal_cache (BTC 5min ONLY - existing)
  └→ Extra markets data collection (BTC 15min + ETH 5min odds)

signal_generator_btc15 (separate process)
  ├→ Reads btc_ticks + poly_odds (btc-updown-15m)
  ├→ Runs Jury → signal_cache_btc15
  └→ Computes BB/VWAP/drift/still/quality

signal_generator_eth5 (separate process)
  ├→ Reads eth_ticks + poly_odds (eth-updown-5m)
  ├→ Runs Jury → signal_cache_eth5
  └→ Computes BB/VWAP/drift/still/quality

Each market has independent: signal_cache → paper_sim → live_trader
Signal generators auto-start when paper/live is started from dashboard.
```

## Multi-Account Trading
- accounts table: per-account API keys, Telegram, seed capital, stake
- Main Account: reads from .env.secrets
- Other accounts: reads from DB accounts table (NOT .env.secrets)
- Each account can trade any market independently
- /api/accounts/start accepts {account_id, market: "btc5"|"btc15"|"eth5"}
- LIVE_ACCOUNT_MARKET_PROCS tracks (account_id, market) -> process

## Database Tables
### Per-market tables (BTC 15min suffix: _btc15, ETH 5min: _eth5)
- signal_cache_btc15 / signal_cache_log_btc15
- signal_cache_eth5 / signal_cache_log_eth5
- paper_trades_btc15 / paper_trades_eth5
- live_trades_btc15 / live_trades_eth5

## Ops: Uptime Automation (2026-07-29)
- **`scripts/watchdog.py`** — self-healing supervisor, one pass per run (`--loop` = every 5 min):
  MariaDB port -> API :8790 (restart if dead, killing orphaned managed children first)
  -> collector (spawn if missing; kill data_collector.py if btc tick age > 180s, wrapper revives)
  -> web :3100 -> paper/live/signal components via dashboard API.
- Config: `scripts/watchdog_config.json` (per-component enable + live stakes).
  Kill switch: create `scripts/watchdog_off.flag`. Log: `logs/watchdog.log`.
- **Live components never start cold**: deferred one pass after their deps (collector/signal
  generator) were (re)started, and only when btc tick age <= 30s — prevents entries off a
  stale signal_cache row after downtime.
- **live_btc5 auto-starts once wallet balance >= stake** (watchdog retries every 5 min;
  dashboard's balance check refuses while $0). Fund the funder wallet -> trading resumes alone.
- **Spawn flags: CREATE_NO_WINDOW only.** DETACHED_PROCESS made every grandchild
  (dashboard's managed procs) open a visible terminal window.
- Autostart at logon: copy `scripts\polybot_watchdog.cmd` into shell:startup (needs user action).
- AC sleep/hibernate disabled via powercfg (2026-07-29).
- `scripts/slippage_report.py` — fill-vs-signal-ask stats per stake bucket; the gate for the
  stake ladder $10 -> $25 -> $50 -> $100 (advance only while p90 slip <= +5c, mean <= +1.5c).

## Database Connection
- **Credentials in `.env.secrets`** (not `env/` dir, project root)
- config.py loads `.env.secrets` via dotenv — must be loaded before DB calls
- Port 3400, password in `.env.secrets` (MARIADB_PASSWORD)
- If `connect_db()` fails with auth_gssapi_client, `.env.secrets` not loaded
