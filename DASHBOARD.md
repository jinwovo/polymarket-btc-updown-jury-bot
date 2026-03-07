# Live Dashboard (Next.js + shadcn/ui style)

This project includes:

- Python live API server: `dashboard_server.py` (`/api/snapshot`, `/api/history`)
- Next.js dashboard frontend (React + Tailwind + shadcn-style components)

## One-command start

Install dependencies once:

```bash
pip install -r requirements.txt
npm.cmd install
```

Start everything with one command:

```bash
npm.cmd run start
```

This single command runs:

- Python API server on `http://127.0.0.1:8790`
- Next.js web app on `http://127.0.0.1:3100`

Open:

```text
http://127.0.0.1:3100
```

## MariaDB example (PowerShell)

```powershell
$env:DB_BACKEND='mariadb'
$env:MARIADB_HOST='127.0.0.1'
$env:MARIADB_PORT='3400'
$env:MARIADB_USER='root'
$env:MARIADB_PASSWORD='your_password'
$env:MARIADB_DATABASE='future_prediction_live'
npm.cmd run start
```

If your MariaDB port is different, change `MARIADB_PORT`.

## Live Paper Trading (virtual $1000)

When an actionable signal appears (`BUY UP` / `BUY DOWN`), you can run virtual entries using real ask prices and resolve by actual 5m outcome:

```bash
python paper_trade_sim.py --stake 1000 --interval 2
```

Check results:

```bash
python paper_trade_sim.py --status
```

## Strategy knobs

The signal engine now runs a 5-judge jury by default.

- `JURY_THRESHOLD=3` (default): minimum same-direction votes to allow entry
- Higher threshold (e.g. `4`) is more selective, fewer trades
- Lower threshold (e.g. `2`) is more aggressive, more trades

## In-dashboard execution

With `npm.cmd run start`, you can now launch these from the web UI:

- `paper_trade_sim.py` start/stop
- `backtest.py` single run
- `backtest.py --auto-sweep` (JURY_THRESHOLD x MIN_EDGE grid)
