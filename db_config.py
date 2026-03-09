"""
Database backend configuration and helpers.

Supported backends:
- sqlite (default)
- mariadb
"""
import os
import sqlite3
import time
import logging
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)

_MARIADB_RETRY_ERRNOS = {1020, 1205, 1213}
_MARIADB_MAX_RETRIES = 4
_MARIADB_RETRY_BASE_SLEEP_SEC = 0.05


def db_backend() -> str:
    raw = os.getenv("DB_BACKEND", "sqlite").strip().lower()
    if raw in {"mariadb", "mysql"}:
        return "mariadb"
    return "sqlite"


def is_sqlite_backend() -> bool:
    return db_backend() == "sqlite"


def is_mariadb_backend() -> bool:
    return db_backend() == "mariadb"


def sqlite_db_path() -> Path:
    raw = os.getenv("SQLITE_DB_PATH", str(BASE_DIR / "market_data.db"))
    return Path(raw)


def _mariadb_host() -> str:
    return os.getenv("MARIADB_HOST", "127.0.0.1")


def _mariadb_port() -> int:
    try:
        return int(os.getenv("MARIADB_PORT", "3306"))
    except ValueError:
        return 3306


def _mariadb_user() -> str:
    return os.getenv("MARIADB_USER", "root")


def _mariadb_password() -> str:
    return os.getenv("MARIADB_PASSWORD", "")


def _mariadb_database() -> str:
    return os.getenv("MARIADB_DATABASE", "future_prediction")


def db_label() -> str:
    if is_sqlite_backend():
        return str(sqlite_db_path())
    return (
        f"mariadb://{_mariadb_user()}@{_mariadb_host()}:{_mariadb_port()}/"
        f"{_mariadb_database()}"
    )


def _adapt_query(query: str) -> str:
    if is_mariadb_backend():
        return query.replace("?", "%s")
    return query


def _mariadb_errno(exc: Exception) -> int | None:
    try:
        return int(getattr(exc, "args", [None])[0])
    except Exception:
        return None


def _is_retryable_mariadb_error(exc: Exception) -> bool:
    errno = _mariadb_errno(exc)
    return errno in _MARIADB_RETRY_ERRNOS


def _run_mariadb_with_retry(conn, op_name: str, fn):
    last_exc = None
    for attempt in range(1, _MARIADB_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if (not _is_retryable_mariadb_error(e)) or attempt >= _MARIADB_MAX_RETRIES:
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            sleep_sec = _MARIADB_RETRY_BASE_SLEEP_SEC * attempt
            logger.warning(
                "Transient MariaDB error in %s (errno=%s, attempt=%s/%s). Retrying in %.2fs.",
                op_name,
                _mariadb_errno(e),
                attempt,
                _MARIADB_MAX_RETRIES,
                sleep_sec,
            )
            time.sleep(sleep_sec)
    if last_exc is not None:
        raise last_exc


def connect_db():
    if is_sqlite_backend():
        return sqlite3.connect(str(sqlite_db_path()))

    try:
        import pymysql
    except ImportError as e:
        raise RuntimeError(
            "DB_BACKEND=mariadb requires `pymysql`. Run `pip install -r requirements.txt`."
        ) from e

    # Ensure target schema exists before connecting to it.
    ensure_mariadb_database()

    return pymysql.connect(
        host=_mariadb_host(),
        port=_mariadb_port(),
        user=_mariadb_user(),
        password=_mariadb_password(),
        database=_mariadb_database(),
        charset="utf8mb4",
        autocommit=False,
        init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
    )


def ensure_mariadb_database():
    if not is_mariadb_backend():
        return

    try:
        import pymysql
    except ImportError as e:
        raise RuntimeError(
            "DB_BACKEND=mariadb requires `pymysql`. Run `pip install -r requirements.txt`."
        ) from e

    conn = pymysql.connect(
        host=_mariadb_host(),
        port=_mariadb_port(),
        user=_mariadb_user(),
        password=_mariadb_password(),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{_mariadb_database()}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()


def execute_write(conn, query: str, params: tuple | list = ()):
    q = _adapt_query(query)
    if is_sqlite_backend():
        conn.execute(q, params)
        return
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
    _run_mariadb_with_retry(conn, "execute_write", _op)


def executemany_write(conn, query: str, param_rows: list[tuple]):
    if not param_rows:
        return
    q = _adapt_query(query)
    if is_sqlite_backend():
        conn.executemany(q, param_rows)
        return
    def _op():
        with conn.cursor() as cur:
            cur.executemany(q, param_rows)
    _run_mariadb_with_retry(conn, "executemany_write", _op)


def fetch_one(conn, query: str, params: tuple | list = ()) -> Any:
    q = _adapt_query(query)
    if is_sqlite_backend():
        cur = conn.execute(q, params)
        return cur.fetchone()
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchone()
    return _run_mariadb_with_retry(conn, "fetch_one", _op)


def fetch_all(conn, query: str, params: tuple | list = ()) -> list[Any]:
    q = _adapt_query(query)
    if is_sqlite_backend():
        cur = conn.execute(q, params)
        return cur.fetchall()
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
            return list(cur.fetchall())
    return _run_mariadb_with_retry(conn, "fetch_all", _op)


def fetch_all_dicts(conn, query: str, params: tuple | list = ()) -> list[dict]:
    q = _adapt_query(query)
    if is_sqlite_backend():
        cur = conn.execute(q, params)
        rows = cur.fetchall()
        cols = [c[0] for c in (cur.description or [])]
    else:
        def _op():
            with conn.cursor() as cur:
                cur.execute(q, params)
                rows = cur.fetchall()
                cols = [c[0] for c in (cur.description or [])]
                return rows, cols
        rows, cols = _run_mariadb_with_retry(conn, "fetch_all_dicts", _op)
    return [dict(zip(cols, row)) for row in rows]


def fetch_one_dict(conn, query: str, params: tuple | list = ()) -> dict | None:
    rows = fetch_all_dicts(conn, query, params)
    return rows[0] if rows else None


def init_market_schema(conn):
    if is_sqlite_backend():
        execute_write(conn, "PRAGMA journal_mode=WAL")
        execute_write(conn, "PRAGMA synchronous=NORMAL")

        sqlite_statements = [
            """
            CREATE TABLE IF NOT EXISTS btc_ticks (
                ts REAL PRIMARY KEY,
                price REAL NOT NULL,
                volume REAL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS poly_odds (
                ts REAL NOT NULL,
                window_start INTEGER NOT NULL,
                slug TEXT NOT NULL,
                up_mid REAL,
                down_mid REAL,
                up_best_bid REAL,
                up_best_ask REAL,
                down_best_bid REAL,
                down_best_ask REAL,
                PRIMARY KEY (ts, window_start)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_windows (
                window_start INTEGER PRIMARY KEY,
                window_end INTEGER NOT NULL,
                slug TEXT NOT NULL,
                btc_start_price REAL,
                btc_end_price REAL,
                actual_outcome TEXT,
                condition_id TEXT,
                up_token_id TEXT,
                down_token_id TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_utc TEXT NOT NULL,
                window_start INTEGER,
                window_end INTEGER,
                slug TEXT,
                history_type TEXT NOT NULL DEFAULT 'rejected',
                support_direction TEXT,
                support_votes INTEGER,
                direction TEXT NOT NULL,
                avg_confidence REAL NOT NULL,
                threshold REAL NOT NULL,
                reason TEXT NOT NULL,
                btc_change_pct REAL,
                up_mid REAL,
                down_mid REAL,
                judges_json TEXT NOT NULL,
                gate_json TEXT,
                dedupe_key TEXT NOT NULL UNIQUE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_btc_ts ON btc_ticks(ts)",
            "CREATE INDEX IF NOT EXISTS idx_odds_window ON poly_odds(window_start, ts)",
            "CREATE INDEX IF NOT EXISTS idx_signal_ts ON signal_history(ts)",
        ]
        for stmt in sqlite_statements:
            execute_write(conn, stmt)
        # lightweight schema migration for existing DB files
        for alter_sql in [
            "ALTER TABLE signal_history ADD COLUMN history_type TEXT NOT NULL DEFAULT 'rejected'",
            "ALTER TABLE signal_history ADD COLUMN support_direction TEXT",
            "ALTER TABLE signal_history ADD COLUMN support_votes INTEGER",
            "ALTER TABLE signal_history ADD COLUMN gate_json TEXT",
        ]:
            try:
                execute_write(conn, alter_sql)
            except Exception:
                pass
        # Create after migration so old DBs without history_type do not crash.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_signal_type_ts ON signal_history(history_type, ts)",
        ]:
            try:
                execute_write(conn, idx_sql)
            except Exception:
                pass
        return

    ensure_mariadb_database()
    mariadb_statements = [
        """
        CREATE TABLE IF NOT EXISTS btc_ticks (
            ts DOUBLE PRIMARY KEY,
            price DOUBLE NOT NULL,
            volume DOUBLE DEFAULT 0
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS poly_odds (
            ts DOUBLE NOT NULL,
            window_start BIGINT NOT NULL,
            slug VARCHAR(191) NOT NULL,
            up_mid DOUBLE NULL,
            down_mid DOUBLE NULL,
            up_best_bid DOUBLE NULL,
            up_best_ask DOUBLE NULL,
            down_best_bid DOUBLE NULL,
            down_best_ask DOUBLE NULL,
            PRIMARY KEY (ts, window_start),
            INDEX idx_odds_window (window_start, ts)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS market_windows (
            window_start BIGINT PRIMARY KEY,
            window_end BIGINT NOT NULL,
            slug VARCHAR(191) NOT NULL,
            btc_start_price DOUBLE NULL,
            btc_end_price DOUBLE NULL,
            actual_outcome VARCHAR(16) NULL,
            condition_id VARCHAR(191) NULL,
            up_token_id VARCHAR(255) NULL,
            down_token_id VARCHAR(255) NULL,
            INDEX idx_window_end (window_end),
            INDEX idx_outcome (actual_outcome)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS signal_history (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts DOUBLE NOT NULL,
            ts_utc VARCHAR(64) NOT NULL,
            window_start BIGINT NULL,
            window_end BIGINT NULL,
            slug VARCHAR(191) NULL,
            history_type VARCHAR(16) NOT NULL DEFAULT 'rejected',
            support_direction VARCHAR(16) NULL,
            support_votes INT NULL,
            direction VARCHAR(16) NOT NULL,
            avg_confidence DOUBLE NOT NULL,
            threshold DOUBLE NOT NULL,
            reason TEXT NOT NULL,
            btc_change_pct DOUBLE NULL,
            up_mid DOUBLE NULL,
            down_mid DOUBLE NULL,
            judges_json LONGTEXT NOT NULL,
            gate_json LONGTEXT NULL,
            dedupe_key VARCHAR(512) NOT NULL,
            UNIQUE KEY uq_signal_dedupe (dedupe_key),
            INDEX idx_signal_ts (ts),
            INDEX idx_signal_type_ts (history_type, ts)
        ) ENGINE=InnoDB
        """,
        "CREATE INDEX idx_btc_ts ON btc_ticks(ts)",
    ]
    for stmt in mariadb_statements:
        try:
            execute_write(conn, stmt)
        except Exception as e:
            # idx_btc_ts may already exist in MariaDB without IF NOT EXISTS support.
            if "Duplicate key name" in str(e):
                continue
            raise
    # lightweight schema migration for existing MariaDB tables
    for alter_sql in [
        "ALTER TABLE signal_history ADD COLUMN history_type VARCHAR(16) NOT NULL DEFAULT 'rejected'",
        "ALTER TABLE signal_history ADD COLUMN support_direction VARCHAR(16) NULL",
        "ALTER TABLE signal_history ADD COLUMN support_votes INT NULL",
        "ALTER TABLE signal_history ADD COLUMN gate_json LONGTEXT NULL",
        "CREATE INDEX idx_signal_type_ts ON signal_history(history_type, ts)",
    ]:
        try:
            execute_write(conn, alter_sql)
        except Exception:
            pass


def upsert_btc_ticks_sql() -> str:
    if is_sqlite_backend():
        return "INSERT OR REPLACE INTO btc_ticks (ts, price, volume) VALUES (?, ?, ?)"
    return (
        "INSERT INTO btc_ticks (ts, price, volume) VALUES (?, ?, ?) "
        "ON DUPLICATE KEY UPDATE price=VALUES(price), volume=VALUES(volume)"
    )


def upsert_poly_odds_sql() -> str:
    if is_sqlite_backend():
        return (
            "INSERT OR REPLACE INTO poly_odds "
            "(ts, window_start, slug, up_mid, down_mid, "
            "up_best_bid, up_best_ask, down_best_bid, down_best_ask) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
    return (
        "INSERT INTO poly_odds "
        "(ts, window_start, slug, up_mid, down_mid, "
        "up_best_bid, up_best_ask, down_best_bid, down_best_ask) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE "
        "slug=VALUES(slug), up_mid=VALUES(up_mid), down_mid=VALUES(down_mid), "
        "up_best_bid=VALUES(up_best_bid), up_best_ask=VALUES(up_best_ask), "
        "down_best_bid=VALUES(down_best_bid), down_best_ask=VALUES(down_best_ask)"
    )


def upsert_market_window_sql() -> str:
    if is_sqlite_backend():
        return (
            "INSERT OR REPLACE INTO market_windows "
            "(window_start, window_end, slug, btc_start_price, "
            "condition_id, up_token_id, down_token_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
    return (
        "INSERT INTO market_windows "
        "(window_start, window_end, slug, btc_start_price, "
        "condition_id, up_token_id, down_token_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE "
        "window_end=VALUES(window_end), slug=VALUES(slug), "
        "btc_start_price=COALESCE(VALUES(btc_start_price), btc_start_price), "
        "condition_id=VALUES(condition_id), up_token_id=VALUES(up_token_id), "
        "down_token_id=VALUES(down_token_id)"
    )
