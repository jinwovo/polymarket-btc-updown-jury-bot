# Polymarket BTC Up/Down 5-Minute Trading Bot

Automated trading bot for Polymarket's BTC 5-minute Up/Down prediction markets. Uses real-time Chainlink price feeds, 3-judge jury system, and Binance-Polymarket lag arbitrage.

## Dashboard

![Dashboard Preview](example.png)

## Architecture

```
data_collector (single process)
  ├─ RTDS WebSocket (Chainlink real-time price, 1s)
  ├─ Binance WebSocket (BTC trades + buy/sell volume)
  ├─ CLOB API polling (orderbook odds, 0.1s)
  ├─ Playwright (PTB scrape, Final price backfill)
  ├─ 3-Judge Jury evaluation
  ├─ Price guards (divergence, momentum, trend)
  └─ signal_cache DB (direction, prices, odds, gate_allow)
         │
    ┌────┴────┐
  Paper      Live
  reads signal_cache    reads signal_cache
  simulated trades      MAKER_FIRST / FOK orders
```

## Strategy

- **Edge**: Binance price leads Polymarket (Chainlink) by 1-3 seconds
- **Jury**: 3 independent judges (Statistical, Arbitrage, Orderbook) vote UP/DOWN
- **Entry**: 2/3 majority, no opposing votes, entry at 150-240s into 5-min window
- **Exit**: Hold to settlement (binary market, $1.00 payout on win)
- **Maker orders**: 0% fee (vs 3% taker), FOK fallback

## Performance (48h backtest)

| Metric | Value |
|--------|-------|
| Trades | ~80-100 |
| Win rate | 65-75% |
| Profit factor | 2.0-3.0 |
| Entry price range | 0.30-0.70 |

## Files

| File | Description |
|------|-------------|
| `data_collector.py` | RTDS + Binance + CLOB + Jury → signal_cache |
| `main.py` | Live trading (reads signal_cache, places orders) |
| `paper_trade_sim.py` | Paper trading (reads signal_cache, simulates) |
| `judges.py` | 3 judges + Jury deliberation |
| `polymarket_client.py` | CLOB API + Playwright + MAKER_FIRST orders |
| `trade_gate.py` | Entry gate (EV, coinflip, probability) |
| `backtest.py` | Backtester with parameter sweep |
| `dashboard_server.py` | API server + process manager |
| `config.py` | Settings via env vars |
| `env/runtime.public.env` | Runtime parameters (safe to commit) |

## Quick Start

```bash
# Install
pip install -r requirements.txt
npm install

# Run (starts collector + API + dashboard)
npm run start
```

Open `http://localhost:3100`

Then click **Paper Start** and/or **Start Live** in the dashboard.

## Configuration

Secrets in `.env.secrets` (git-ignored):
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`
- `MARIADB_PASSWORD`

Key parameters in `env/runtime.public.env`:
- `JURY_THRESHOLD=2` (2/3 majority)
- `MIN_EDGE=0.08` (minimum judge edge)
- `PAPER_MIN_EXPECTED_ROI=0.030` (3% min EV)
- `PAPER_ENTRY_START_SEC=150` (enter after 2:30)
- `PAPER_DOWN_MIN_ENTRY_PRICE=0.30` (no extreme odds)
- `PAPER_MAX_ENTRY_PRICE=0.70`
- `ENTRY_ORDER_MODE=MAKER_FIRST` (0% fee maker → FOK fallback)

## Database

MariaDB (port 3400). Tables: `btc_ticks`, `poly_odds`, `market_windows`, `signal_cache`, `paper_trades`, `live_trades`.

## Backtest

```bash
python backtest.py --last-hours 24
python backtest.py --last-hours 48 --smart-exit  # with mid-trade exit
```

## License

MIT
