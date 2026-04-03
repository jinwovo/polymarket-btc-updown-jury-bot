"""
Database configuration and helpers (MariaDB).
"""
import os
import time
import logging
from typing import Any

import pymysql

logger = logging.getLogger(__name__)

_RETRY_ERRNOS = {1020, 1205, 1213}
_MAX_RETRIES = 4
_RETRY_BASE_SLEEP_SEC = 0.05


def _host() -> str:
    return os.getenv("MARIADB_HOST", "127.0.0.1")


def _port() -> int:
    try:
        return int(os.getenv("MARIADB_PORT", "3306"))
    except ValueError:
        return 3306


def _user() -> str:
    return os.getenv("MARIADB_USER", "root")


def _password() -> str:
    return os.getenv("MARIADB_PASSWORD", "")


def _database() -> str:
    return os.getenv("MARIADB_DATABASE", "polymarket_btc_updown")


def db_label() -> str:
    return f"mariadb://{_user()}@{_host()}:{_port()}/{_database()}"


def _adapt_query(query: str) -> str:
    return query.replace("?", "%s")


def _errno(exc: Exception) -> int | None:
    try:
        return int(getattr(exc, "args", [None])[0])
    except Exception:
        return None


def _is_retryable(exc: Exception) -> bool:
    return _errno(exc) in _RETRY_ERRNOS


def _run_with_retry(conn, op_name: str, fn):
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if (not _is_retryable(e)) or attempt >= _MAX_RETRIES:
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            sleep_sec = _RETRY_BASE_SLEEP_SEC * attempt
            logger.warning(
                "Transient MariaDB error in %s (errno=%s, attempt=%s/%s). Retrying in %.2fs.",
                op_name,
                _errno(e),
                attempt,
                _MAX_RETRIES,
                sleep_sec,
            )
            time.sleep(sleep_sec)
    if last_exc is not None:
        raise last_exc


def ensure_database():
    conn = pymysql.connect(
        host=_host(),
        port=_port(),
        user=_user(),
        password=_password(),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{_database()}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()


def connect_db():
    ensure_database()
    return pymysql.connect(
        host=_host(),
        port=_port(),
        user=_user(),
        password=_password(),
        database=_database(),
        charset="utf8mb4",
        autocommit=False,
        init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
    )


def execute_write(conn, query: str, params: tuple | list = ()):
    q = _adapt_query(query)
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
    _run_with_retry(conn, "execute_write", _op)


def executemany_write(conn, query: str, param_rows: list[tuple]):
    if not param_rows:
        return
    q = _adapt_query(query)
    def _op():
        with conn.cursor() as cur:
            cur.executemany(q, param_rows)
    _run_with_retry(conn, "executemany_write", _op)


def fetch_one(conn, query: str, params: tuple | list = ()) -> Any:
    q = _adapt_query(query)
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchone()
    return _run_with_retry(conn, "fetch_one", _op)


def fetch_all(conn, query: str, params: tuple | list = ()) -> list[Any]:
    q = _adapt_query(query)
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
            return list(cur.fetchall())
    return _run_with_retry(conn, "fetch_all", _op)


def fetch_all_dicts(conn, query: str, params: tuple | list = ()) -> list[dict]:
    q = _adapt_query(query)
    def _op():
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()
            cols = [c[0] for c in (cur.description or [])]
            return rows, cols
    rows, cols = _run_with_retry(conn, "fetch_all_dicts", _op)
    return [dict(zip(cols, row)) for row in rows]


def fetch_one_dict(conn, query: str, params: tuple | list = ()) -> dict | None:
    rows = fetch_all_dicts(conn, query, params)
    return rows[0] if rows else None


def init_market_schema(conn):
    ensure_database()
    statements = [
        """
        CREATE TABLE IF NOT EXISTS btc_ticks (
            ts DOUBLE PRIMARY KEY,
            price DOUBLE NOT NULL,
            volume DOUBLE DEFAULT 0,
            buy_volume DOUBLE DEFAULT 0,
            sell_volume DOUBLE DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS feature_1s (
            ts_sec BIGINT NOT NULL,
            ts DOUBLE NOT NULL,
            window_start BIGINT NOT NULL,
            slug VARCHAR(191) NULL,
            seconds_elapsed DOUBLE NULL,
            seconds_remaining DOUBLE NULL,
            btc_start_price DOUBLE NULL,
            btc_price DOUBLE NULL,
            btc_move_pct DOUBLE NULL,
            up_best_ask DOUBLE NULL,
            down_best_ask DOUBLE NULL,
            up_mid DOUBLE NULL,
            down_mid DOUBLE NULL,
            PRIMARY KEY (ts_sec, window_start),
            INDEX idx_feature_ts (ts_sec),
            INDEX idx_feature_window_ts (window_start, ts_sec)
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
        """
        CREATE TABLE IF NOT EXISTS signal_cache (
            id TINYINT PRIMARY KEY DEFAULT 1,
            ts DOUBLE NOT NULL,
            window_start BIGINT NOT NULL,
            direction VARCHAR(16) NOT NULL,
            avg_confidence DOUBLE NOT NULL,
            max_edge DOUBLE NOT NULL,
            unanimous TINYINT NOT NULL DEFAULT 0,
            judges_json LONGTEXT NOT NULL,
            up_ask DOUBLE NULL,
            down_ask DOUBLE NULL,
            btc_price DOUBLE NULL,
            start_price DOUBLE NULL,
            seconds_elapsed DOUBLE NULL,
            seconds_remaining DOUBLE NULL,
            btc_move_pct DOUBLE NULL,
            recent_move_pct DOUBLE NULL,
            trend_move_pct DOUBLE NULL,
            guards_passed TINYINT NOT NULL DEFAULT 0,
            buy_sell_ratio DOUBLE NULL,
            gate_allow TINYINT NOT NULL DEFAULT 0,
            gate_ev DOUBLE NULL,
            gate_reason TEXT NULL,
            binance_rtds_gap DOUBLE NULL
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS signal_cache_log (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts DOUBLE NOT NULL,
            window_start BIGINT NOT NULL,
            direction VARCHAR(16) NOT NULL,
            avg_confidence DOUBLE NOT NULL,
            max_edge DOUBLE NOT NULL,
            unanimous TINYINT NOT NULL DEFAULT 0,
            judges_json LONGTEXT NOT NULL,
            up_ask DOUBLE NULL,
            down_ask DOUBLE NULL,
            btc_price DOUBLE NULL,
            start_price DOUBLE NULL,
            seconds_elapsed DOUBLE NULL,
            seconds_remaining DOUBLE NULL,
            btc_move_pct DOUBLE NULL,
            recent_move_pct DOUBLE NULL,
            trend_move_pct DOUBLE NULL,
            guards_passed TINYINT NOT NULL DEFAULT 0,
            buy_sell_ratio DOUBLE NULL,
            gate_allow TINYINT NOT NULL DEFAULT 0,
            gate_ev DOUBLE NULL,
            gate_reason TEXT NULL,
            binance_rtds_gap DOUBLE NULL,
            INDEX idx_scl_ws_ts (window_start, ts),
            INDEX idx_scl_ws_gate (window_start, gate_allow)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            enabled TINYINT NOT NULL DEFAULT 1,
            private_key TEXT NULL,
            api_key VARCHAR(256) NULL,
            api_secret VARCHAR(256) NULL,
            api_passphrase VARCHAR(256) NULL,
            funder VARCHAR(128) NULL,
            telegram_token VARCHAR(256) NULL,
            telegram_chat_id VARCHAR(64) NULL,
            seed_capital DOUBLE NOT NULL DEFAULT 100,
            fixed_stake DOUBLE NOT NULL DEFAULT 15,
            sizing_mode VARCHAR(32) NOT NULL DEFAULT 'FIXED',
            position_mode VARCHAR(16) NOT NULL DEFAULT 'BOTH',
            daily_loss_limit DOUBLE NOT NULL DEFAULT 100,
            mega_multiplier DOUBLE NOT NULL DEFAULT 3.0,
            mega_min_score INT NOT NULL DEFAULT 6,
            min_entry_score INT NOT NULL DEFAULT 3,
            status VARCHAR(16) NOT NULL DEFAULT 'STOPPED',
            pid INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_account_name (name)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_runtime_state (
            id TINYINT PRIMARY KEY,
            updated_at DOUBLE NOT NULL,
            risk_json LONGTEXT NOT NULL,
            trade_json LONGTEXT NULL,
            kill_switch TINYINT NOT NULL DEFAULT 0,
            kill_reason TEXT NULL
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS live_trades (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            window_start BIGINT NOT NULL UNIQUE,
            window_end BIGINT NOT NULL,
            direction VARCHAR(16) NOT NULL,
            stake DOUBLE NOT NULL,
            entry_price DOUBLE NOT NULL,
            payout_multiple DOUBLE NOT NULL,
            shares DOUBLE NOT NULL,
            potential_win_pnl DOUBLE NOT NULL,
            signal_confidence DOUBLE NULL,
            signal_reason TEXT NULL,
            entry_source VARCHAR(32) NULL,
            close_reason TEXT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            opened_at DOUBLE NOT NULL,
            closed_at DOUBLE NULL,
            actual_outcome VARCHAR(16) NULL,
            won TINYINT NULL,
            pnl DOUBLE NULL,
            roi_pct DOUBLE NULL,
            INDEX idx_live_status_opened (status, opened_at),
            INDEX idx_live_window (window_start)
        ) ENGINE=InnoDB
        """,
        "CREATE INDEX idx_btc_ts ON btc_ticks(ts)",
    ]
    for stmt in statements:
        try:
            execute_write(conn, stmt)
        except Exception as e:
            if "Duplicate key name" in str(e):
                continue
            raise
    # lightweight schema migration for existing tables
    for alter_sql in [
        "ALTER TABLE signal_history ADD COLUMN history_type VARCHAR(16) NOT NULL DEFAULT 'rejected'",
        "ALTER TABLE signal_history ADD COLUMN support_direction VARCHAR(16) NULL",
        "ALTER TABLE signal_history ADD COLUMN support_votes INT NULL",
        "ALTER TABLE signal_history ADD COLUMN gate_json LONGTEXT NULL",
        "CREATE INDEX idx_signal_type_ts ON signal_history(history_type, ts)",
        "ALTER TABLE bot_runtime_state ADD COLUMN kill_switch TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE bot_runtime_state ADD COLUMN kill_reason TEXT NULL",
        "ALTER TABLE live_trades ADD COLUMN signal_confidence DOUBLE NULL",
        "ALTER TABLE live_trades ADD COLUMN signal_reason TEXT NULL",
        "ALTER TABLE live_trades ADD COLUMN entry_source VARCHAR(32) NULL",
        "ALTER TABLE live_trades ADD COLUMN close_reason TEXT NULL",
        "CREATE INDEX idx_live_status_opened ON live_trades(status, opened_at)",
        "CREATE INDEX idx_live_window ON live_trades(window_start)",
        # -- Backtesting enrichment: spread + diffusion columns --
        "ALTER TABLE poly_odds ADD COLUMN spread_up DOUBLE NULL",
        "ALTER TABLE poly_odds ADD COLUMN spread_down DOUBLE NULL",
        "ALTER TABLE poly_odds ADD COLUMN overround DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN up_best_bid DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN down_best_bid DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN spread_up DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN spread_down DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN overround DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN sigma_est DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN p_up_diffusion DOUBLE NULL",
        "ALTER TABLE feature_1s ADD COLUMN lag_freshness DOUBLE NULL",
        # Buy/sell volume split for momentum analysis
        "ALTER TABLE btc_ticks ADD COLUMN buy_volume DOUBLE DEFAULT 0",
        "ALTER TABLE btc_ticks ADD COLUMN sell_volume DOUBLE DEFAULT 0",
        "ALTER TABLE signal_cache ADD COLUMN btc_move_pct DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN recent_move_pct DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN trend_move_pct DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN guards_passed TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE signal_cache ADD COLUMN buy_sell_ratio DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN gate_allow TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE signal_cache ADD COLUMN gate_ev DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN gate_reason TEXT NULL",
        "ALTER TABLE signal_cache ADD COLUMN binance_rtds_gap DOUBLE NULL",
        # Pre-computed score signals for fast entry (no extra DB queries)
        "ALTER TABLE signal_cache ADD COLUMN prev_outcome VARCHAR(16) NULL",
        "ALTER TABLE signal_cache ADD COLUMN odds_velocity DOUBLE NULL",
        "ALTER TABLE signal_cache ADD COLUMN btc_accel_ok TINYINT NULL",
        "ALTER TABLE signal_cache_log ADD COLUMN prev_outcome VARCHAR(16) NULL",
        "ALTER TABLE signal_cache_log ADD COLUMN odds_velocity DOUBLE NULL",
        "ALTER TABLE signal_cache_log ADD COLUMN btc_accel_ok TINYINT NULL",
        "ALTER TABLE live_trades ADD COLUMN account_id INT NULL",
        # Lag arb signal: BTC moved 0.04%+ but CLOB hasn't repriced
        "ALTER TABLE signal_cache ADD COLUMN lag_arb_allow TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE signal_cache ADD COLUMN lag_arb_direction VARCHAR(16) NULL",
        "ALTER TABLE signal_cache ADD COLUMN lag_arb_entry_price DOUBLE NULL",
        "ALTER TABLE signal_cache_log ADD COLUMN lag_arb_allow TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE signal_cache_log ADD COLUMN lag_arb_direction VARCHAR(16) NULL",
        "ALTER TABLE signal_cache_log ADD COLUMN lag_arb_entry_price DOUBLE NULL",
    ]:
        try:
            execute_write(conn, alter_sql)
        except Exception:
            pass


def upsert_btc_ticks_sql() -> str:
    return (
        "INSERT INTO btc_ticks (ts, price, volume, buy_volume, sell_volume) VALUES (?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE price=VALUES(price), volume=VALUES(volume), "
        "buy_volume=VALUES(buy_volume), sell_volume=VALUES(sell_volume)"
    )


def upsert_poly_odds_sql() -> str:
    cols = (
        "ts, window_start, slug, up_mid, down_mid, "
        "up_best_bid, up_best_ask, down_best_bid, down_best_ask, "
        "spread_up, spread_down, overround"
    )
    placeholders = ", ".join(["?"] * 12)
    return (
        f"INSERT INTO poly_odds ({cols}) VALUES ({placeholders}) "
        "ON DUPLICATE KEY UPDATE "
        "slug=VALUES(slug), up_mid=VALUES(up_mid), down_mid=VALUES(down_mid), "
        "up_best_bid=VALUES(up_best_bid), up_best_ask=VALUES(up_best_ask), "
        "down_best_bid=VALUES(down_best_bid), down_best_ask=VALUES(down_best_ask), "
        "spread_up=VALUES(spread_up), spread_down=VALUES(spread_down), "
        "overround=VALUES(overround)"
    )


def upsert_feature_1s_sql() -> str:
    cols = (
        "ts_sec, ts, window_start, slug, seconds_elapsed, seconds_remaining, "
        "btc_start_price, btc_price, btc_move_pct, up_best_ask, down_best_ask, up_mid, down_mid, "
        "up_best_bid, down_best_bid, spread_up, spread_down, overround, "
        "sigma_est, p_up_diffusion, lag_freshness"
    )
    placeholders = ", ".join(["?"] * 21)
    return (
        f"INSERT INTO feature_1s ({cols}) VALUES ({placeholders}) "
        "ON DUPLICATE KEY UPDATE "
        "ts=VALUES(ts), slug=VALUES(slug), "
        "seconds_elapsed=VALUES(seconds_elapsed), seconds_remaining=VALUES(seconds_remaining), "
        "btc_start_price=VALUES(btc_start_price), btc_price=VALUES(btc_price), "
        "btc_move_pct=VALUES(btc_move_pct), "
        "up_best_ask=VALUES(up_best_ask), down_best_ask=VALUES(down_best_ask), "
        "up_mid=VALUES(up_mid), down_mid=VALUES(down_mid), "
        "up_best_bid=VALUES(up_best_bid), down_best_bid=VALUES(down_best_bid), "
        "spread_up=VALUES(spread_up), spread_down=VALUES(spread_down), "
        "overround=VALUES(overround), sigma_est=VALUES(sigma_est), "
        "p_up_diffusion=VALUES(p_up_diffusion), lag_freshness=VALUES(lag_freshness)"
    )


def upsert_market_window_sql() -> str:
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
