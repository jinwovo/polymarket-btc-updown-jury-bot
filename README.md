# Algorithmic-Trading

A real-time BTC/Polymarket signal platform with:
- live data collection,
- 5-judge decision engine,
- fee-aware entry gate,
- paper trading,
- backtest + auto sweep,
- Next.js dashboard controls.

## Why this project

Most retail bots overfit on fake odds or ignore execution quality.
This project is built to test **real collected orderbook data** and block low-quality entries even when judges agree.

## Core features

- Real data pipeline (Binance trades + Polymarket orderbook)
- 5-judge consensus engine (technical, arb, statistical, trend persistence, orderbook quality)
- Fee-aware entry gate
  - skip trade when expected net ROI is below threshold
  - avoid "100% confidence but no real payout" traps
- Paper simulation from dashboard (start/stop + history popup)
- Backtest + auto sweep for `JURY_THRESHOLD` and `MIN_EDGE`
- Signal history (accepted/rejected) with DB persistence and paging

## Tech stack

- Python: collector, strategy, simulation, backtest, API server
- SQLite or MariaDB backend
- Next.js + Tailwind + shadcn-style UI components

## Repository structure

```text
app/                         # Next.js app router
components/dashboard/        # Dashboard UI
config.py                    # Runtime configuration
data_collector.py            # Live data collector
dashboard_server.py          # Python API for dashboard + process control
judges.py                    # 5-judge decision engine
trade_gate.py                # Fee-aware entry gate logic
paper_trade_sim.py           # Paper trading simulator
backtest.py                  # Backtest + auto sweep
main.py                      # Live trading loop
```

## Quick start (one command)

### 1) Install

```bash
pip install -r requirements.txt
npm install
```

### 2) Run full stack

```bash
npm run start
```

Open: `http://127.0.0.1:3100`

This starts:
- web dashboard,
- python API server,
- data collector.

## Dashboard controls

- `Paper Sim Control`
  - start/stop paper simulation from UI
- `Trade History` popup
  - entry price
  - 5m BTC start/end
  - UP/DOWN odds at entry
  - to-win total and to-win pnl
  - realized pnl / roi / outcome
- `Backtest/Sweep Control`
  - single run or auto-sweep from UI

## Strategy safety model

### Entry requirements (high level)

1. Jury direction must be `UP` or `DOWN`
2. Confidence must pass `MIN_EDGE`
3. Time-to-close must pass cutoff
4. **Net expected ROI gate must pass**:
   - includes configured fee/slippage drag
   - blocks low-EV entries

### Important payout note

On Polymarket, UI `To win` is generally a gross payout target.
Your effective result is:

`net pnl = payout - stake - fees/slippage`

This project uses fee-aware filtering and fee-adjusted pnl in simulation/backtest.

## Configuration

Create `.env` from `.env.example`, then edit values.

Key parameters:

- `MIN_EDGE` (default: `0.08`)
- `JURY_THRESHOLD` (default: `3`)
- `TRADE_FEE_RATE` (default: `0.010`)
- `MIN_EXPECTED_ROI` (default: `0.003`)
- `MAX_BET_SIZE` (default: `5.0`)
- `DAILY_LOSS_LIMIT` (default: `50.0`)

## Useful commands

```bash
# Collector status
python data_collector.py --status

# Paper sim status
python paper_trade_sim.py --status

# Backtest
python backtest.py --last-hours 24

# Auto sweep
python backtest.py --auto-sweep --edge-grid "0.04,0.06,0.08,0.10" --jury-grid "2,3,4,5"
```

## Database reset (fresh start)

If you want to restart from clean history, clear:
- `btc_ticks`
- `poly_odds`
- `market_windows`
- `signal_history`
- `paper_trades`

## Roadmap

- Better execution modeling (partial fills and slippage curve)
- More robust market-regime features
- CI checks + benchmark snapshots
- Public demo video + release cadence

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please read [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
