"""Dump column names of trading tables (dev utility)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402,F401  (loads .env.secrets before any DB call)
from db_config import connect_db  # noqa: E402

TABLES = [
    "live_trades", "live_trades_eth5", "live_trades_btc15",
    "paper_trades", "signal_cache_log", "signal_cache_log_eth5",
]

conn = connect_db()
cur = conn.cursor()
for t in TABLES:
    try:
        cur.execute(f"DESCRIBE {t}")
        cols = [r[0] for r in cur.fetchall()]
        print(t, "->", ", ".join(cols))
    except Exception as e:
        print(t, "-> ERROR:", e)
conn.close()
