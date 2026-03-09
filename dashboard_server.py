"""
Lightweight dashboard server for live BTC/Polymarket monitoring.

Usage:
    python dashboard_server.py
    python dashboard_server.py --host 0.0.0.0 --port 8080
"""
import argparse
import os
import json
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from config import config
from db_config import (
    connect_db,
    execute_write,
    db_label,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
    is_sqlite_backend,
    sqlite_db_path,
)
from judges import Jury, MarketContext
from trade_gate import evaluate_entry_gate


BASE_DIR = Path(__file__).parent
DB_PATH = sqlite_db_path()
DASHBOARD_DIR = BASE_DIR / "dashboard"

STATIC_ROUTES = {
    "/": DASHBOARD_DIR / "index.html",
    "/index.html": DASHBOARD_DIR / "index.html",
    "/app.js": DASHBOARD_DIR / "app.js",
    "/styles.css": DASHBOARD_DIR / "styles.css",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dashboard")

# Keep jury logs quiet for UI polling.
logging.getLogger("judges").setLevel(logging.WARNING)
JURY = Jury(threshold=config.trading.jury_threshold)
_LAST_SIGNAL_HISTORY_KEY: Optional[str] = None
_LAST_SIGNAL_HISTORY_TS: dict[str, float] = {}

# Keep UI signal actionability aligned with paper entry core filters.
PAPER_ALIGN_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.040"))
PAPER_ALIGN_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.70"))
PAPER_ALIGN_MIN_TICK_SAMPLES = int(os.getenv("PAPER_MIN_TICK_SAMPLES", "150"))
PAPER_ALIGN_MIN_ODDS_SAMPLES = int(os.getenv("PAPER_MIN_ODDS_SAMPLES", "24"))
PAPER_ALIGN_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "60"))
PAPER_ALIGN_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "255"))
PAPER_ALIGN_MIN_SECONDS_REMAINING = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "45"))
PAPER_ALIGN_RECENT_MOVE_LOOKBACK_SEC = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
PAPER_ALIGN_MIN_RECENT_MOVE_PCT = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.0045"))
PAPER_ALIGN_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.35"))
PAPER_ALIGN_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.58"))


class ManagedProcess:
    def __init__(self, name: str, max_lines: int = 400):
        self.name = name
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._output = deque(maxlen=max_lines)
        self._command: list[str] = []
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._meta: dict[str, Any] = {}

    def _pump_output(self, proc: subprocess.Popen):
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        with self._lock:
                            self._output.append(line)
        except Exception as e:
            with self._lock:
                self._output.append(f"[manager:{self.name}] output reader error: {e}")
        finally:
            code = proc.wait()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                self._exit_code = int(code)
                self._ended_at = time.time()

    def start(
        self,
        command: list[str],
        meta: Optional[dict[str, Any]] = None,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> tuple[bool, str]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False, f"{self.name} already running"

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            for k, v in (env_overrides or {}).items():
                env[str(k)] = str(v)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
            except Exception as e:
                return False, str(e)

            self._proc = proc
            self._command = list(command)
            self._meta = dict(meta or {})
            self._started_at = time.time()
            self._ended_at = None
            self._exit_code = None
            self._output.clear()
            self._output.append(f"[manager:{self.name}] started pid={proc.pid}")

            th = threading.Thread(target=self._pump_output, args=(proc,), daemon=True)
            th.start()

        return True, "started"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False, f"{self.name} not running"

        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return True, "stopped"

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            pid = self._proc.pid if running and self._proc is not None else None
            return {
                "ok": True,
                "name": self.name,
                "running": running,
                "pid": pid,
                "command": self._command,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "exit_code": self._exit_code,
                "meta": self._meta,
                "output_tail": list(self._output)[-80:],
            }


PAPER_SIM_PROC = ManagedProcess("paper_trade_sim")
BACKTEST_PROC = ManagedProcess("backtest")
LIVE_TRADING_PROC = ManagedProcess("live_trading")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_live_position_mode(raw: Any) -> str:
    mode = str(raw or "BOTH").strip().upper()
    if mode in ("UP_ONLY", "DOWN_ONLY", "BOTH"):
        return mode
    return "BOTH"


def _find_numeric_field(payload: Any, candidates: set[str]) -> Optional[float]:
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                lk = str(key).lower()
                if lk in candidates:
                    num = _to_float(val)
                    if num is not None:
                        return num
                if isinstance(val, (dict, list, tuple)):
                    stack.append(val)
        elif isinstance(cur, (list, tuple)):
            for item in cur:
                if isinstance(item, (dict, list, tuple)):
                    stack.append(item)
    return None


def _fetch_live_account_snapshot() -> dict[str, Any]:
    api_key = str(config.polymarket.api_key or "").strip()
    api_secret = str(config.polymarket.api_secret or "").strip()
    api_passphrase = str(config.polymarket.api_passphrase or "").strip()
    funder = str(config.polymarket.funder or "").strip()
    configured = bool(api_key and api_secret and api_passphrase and funder)

    if not configured:
        return {
            "ok": False,
            "configured": False,
            "error": "Missing API credentials or funder address in .env",
            "funder": funder or None,
            "collateral_balance": None,
            "collateral_allowance": None,
        }

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        client = ClobClient(
            config.polymarket.clob_url,
            chain_id=137,
            creds=creds,
            funder=funder,
        )
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        payload = client.get_balance_allowance(params)

        balance = _find_numeric_field(
            payload,
            {"balance", "available_balance", "asset_balance", "available"},
        )
        allowance = _find_numeric_field(
            payload,
            {"allowance", "available_allowance"},
        )
        return {
            "ok": True,
            "configured": True,
            "error": None,
            "funder": funder,
            "collateral_balance": balance,
            "collateral_allowance": allowance,
        }
    except Exception as e:
        return {
            "ok": False,
            "configured": True,
            "error": str(e),
            "funder": funder,
            "collateral_balance": None,
            "collateral_allowance": None,
        }


def build_live_control_status() -> dict[str, Any]:
    status = LIVE_TRADING_PROC.status()
    status["account"] = _fetch_live_account_snapshot()
    return status


def _downsample(rows: list[dict], max_points: int = 320) -> list[dict]:
    if len(rows) <= max_points:
        return rows
    step = (len(rows) + max_points - 1) // max_points
    return rows[::step]


def _connect_db():
    conn = connect_db()
    init_market_schema(conn)
    conn.commit()
    return conn


def _get_latest_tick(conn) -> Optional[dict]:
    return fetch_one_dict(
        conn,
        "SELECT ts, price FROM btc_ticks ORDER BY ts DESC LIMIT 1"
    )


def _get_latest_window(conn, now_ts: float) -> Optional[dict]:
    row = fetch_one_dict(
        conn,
        """SELECT * FROM market_windows
           WHERE window_start <= ? AND window_end > ?
           ORDER BY window_start DESC
           LIMIT 1""",
        (int(now_ts), int(now_ts)),
    )
    if row:
        return row
    return fetch_one_dict(
        conn,
        "SELECT * FROM market_windows ORDER BY window_start DESC LIMIT 1"
    )


def _get_latest_odds_for_window(
    conn, window_start: Optional[int]
) -> Optional[dict]:
    if window_start is None:
        return fetch_one_dict(
            conn,
            "SELECT * FROM poly_odds ORDER BY ts DESC LIMIT 1"
        )
    return fetch_one_dict(
        conn,
        """SELECT * FROM poly_odds
           WHERE window_start = ?
           ORDER BY ts DESC
           LIMIT 1""",
        (window_start,),
    )


def _get_market_start_price(
    conn,
    window_start: int,
    window_end: int,
    db_value: Any,
) -> Optional[float]:
    start_price = _to_float(db_value)
    if start_price is not None:
        return start_price

    row = fetch_one_dict(
        conn,
        """SELECT price FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC
           LIMIT 1""",
        (float(window_start), float(window_end)),
    )
    if not row:
        return None
    return _to_float(row["price"])


def _get_recent_results(conn, limit: int = 20) -> list[str]:
    rows = fetch_all_dicts(
        conn,
        """SELECT actual_outcome FROM (
               SELECT window_start, actual_outcome
               FROM market_windows
               WHERE actual_outcome IN ('UP', 'DOWN')
               ORDER BY window_start DESC
               LIMIT ?
           ) t
           ORDER BY window_start ASC""",
        (limit,),
    )
    return [str(r["actual_outcome"]) for r in rows if r["actual_outcome"]]


def _window_sample_counts(conn, window_start: int, now_ts: float) -> tuple[int, int]:
    tick_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?""",
        (float(window_start), float(now_ts)),
    )
    odds_row = fetch_one(
        conn,
        """SELECT COUNT(*)
           FROM poly_odds
           WHERE window_start = ? AND ts <= ?""",
        (int(window_start), float(now_ts)),
    )
    tick_cnt = int(tick_row[0] or 0) if tick_row else 0
    odds_cnt = int(odds_row[0] or 0) if odds_row else 0
    return tick_cnt, odds_cnt


def _recent_move_pct(conn, window_start: int, now_ts: float, lookback_sec: float) -> Optional[float]:
    lo_ts = max(float(window_start), float(now_ts) - max(1.0, float(lookback_sec)))
    hi_ts = float(now_ts)
    first_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    last_row = fetch_one(
        conn,
        """SELECT price
           FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts DESC
           LIMIT 1""",
        (lo_ts, hi_ts),
    )
    if not first_row or not last_row:
        return None
    try:
        p0 = float(first_row[0])
        p1 = float(last_row[0])
        if p0 <= 0.0:
            return None
        return ((p1 - p0) / p0) * 100.0
    except Exception:
        return None


def _get_recent_btc(
    conn,
    start_ts: float,
    end_ts: float,
) -> tuple[list[float], list[float]]:
    rows = fetch_all_dicts(
        conn,
        """SELECT ts, price FROM btc_ticks
           WHERE ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (start_ts, end_ts),
    )
    ts = [_to_float(r["ts"]) for r in rows]
    px = [_to_float(r["price"]) for r in rows]
    ts_f = [t for t in ts if t is not None]
    px_f = [p for p in px if p is not None]
    # Keep lists in sync for judge context.
    n = min(len(ts_f), len(px_f))
    return ts_f[:n], px_f[:n]


def _window_rows(conn, limit: int = 12) -> list[dict]:
    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, window_end, slug, btc_start_price, btc_end_price, actual_outcome
           FROM market_windows
           ORDER BY window_start DESC
           LIMIT ?""",
        (limit,),
    )

    out: list[dict] = []
    for r in rows:
        sp = _to_float(r["btc_start_price"])
        ep = _to_float(r["btc_end_price"])
        if sp and ep:
            change_pct = ((ep - sp) / sp) * 100.0
        else:
            change_pct = None
        out.append(
            {
                "window_start": _to_int(r["window_start"]),
                "window_end": _to_int(r["window_end"]),
                "slug": str(r["slug"]) if r["slug"] else "",
                "btc_start_price": sp,
                "btc_end_price": ep,
                "actual_outcome": str(r["actual_outcome"]) if r["actual_outcome"] else None,
                "change_pct": change_pct,
            }
        )
    return out


def _stats(conn) -> dict:
    tick_count = fetch_one(conn, "SELECT COUNT(*) FROM btc_ticks")[0]
    odds_count = fetch_one(conn, "SELECT COUNT(*) FROM poly_odds")[0]
    window_count = fetch_one(conn, "SELECT COUNT(*) FROM market_windows")[0]
    resolved_count = fetch_one(
        conn,
        "SELECT COUNT(*) FROM market_windows WHERE actual_outcome IS NOT NULL",
    )[0]
    return {
        "ticks": int(tick_count),
        "odds": int(odds_count),
        "windows": int(window_count),
        "resolved_windows": int(resolved_count),
    }


def _get_last_actionable_signal(conn, now_ts: float) -> Optional[dict]:
    row = fetch_one_dict(
        conn,
        """SELECT ts, ts_utc, window_start, window_end, slug,
                  direction, avg_confidence, reason
           FROM signal_history
           WHERE history_type = 'accepted'
           ORDER BY ts DESC
           LIMIT 1""",
    )
    if not row:
        return None
    ts = _to_float(row.get("ts"))
    age_sec = (now_ts - ts) if ts is not None else None
    return {
        "ts": ts,
        "ts_utc": str(row.get("ts_utc") or ""),
        "window_start": _to_int(row.get("window_start")),
        "window_end": _to_int(row.get("window_end")),
        "slug": str(row.get("slug")) if row.get("slug") else None,
        "direction": str(row.get("direction") or "NO_TRADE"),
        "avg_confidence": _to_float(row.get("avg_confidence")) or 0.0,
        "reason": str(row.get("reason") or ""),
        "age_sec": age_sec,
    }


def _build_signal(
    conn,
    now_ts: float,
    window: Optional[dict],
    latest_tick: Optional[dict],
    latest_odds: Optional[dict],
) -> dict:
    if not window or not latest_tick or not latest_odds:
        return {
            "direction": "NO_TRADE",
            "actionable": False,
            "action_label": "WAIT",
            "avg_confidence": 0.0,
            "threshold": config.trading.min_edge,
            "jury_threshold": JURY.threshold,
            "jury_size": JURY.size,
            "unanimous": False,
            "reason": "Not enough live data yet.",
            "judges": [],
        }

    ws = _to_int(window["window_start"])
    we = _to_int(window["window_end"])
    btc_now = _to_float(latest_tick["price"])
    up_mid = _to_float(latest_odds["up_mid"])
    down_mid = _to_float(latest_odds["down_mid"])

    if ws is None or we is None or btc_now is None or up_mid is None or down_mid is None:
        return {
            "direction": "NO_TRADE",
            "actionable": False,
            "action_label": "WAIT",
            "avg_confidence": 0.0,
            "threshold": config.trading.min_edge,
            "jury_threshold": JURY.threshold,
            "jury_size": JURY.size,
            "unanimous": False,
            "reason": "Missing required fields for signal.",
            "judges": [],
        }

    start_price = _get_market_start_price(conn, ws, we, window["btc_start_price"])
    if start_price is None or start_price <= 0:
        return {
            "direction": "NO_TRADE",
            "actionable": False,
            "action_label": "WAIT",
            "avg_confidence": 0.0,
            "threshold": config.trading.min_edge,
            "jury_threshold": JURY.threshold,
            "jury_size": JURY.size,
            "unanimous": False,
            "reason": "Waiting for market start BTC price.",
            "judges": [],
        }

    seconds_elapsed = max(0.0, now_ts - float(ws))
    seconds_remaining = max(0.0, float(we) - now_ts)

    ts_list, price_list = _get_recent_btc(conn, now_ts - 600.0, now_ts)
    recent_results = _get_recent_results(conn, limit=20)

    if len(price_list) < 10:
        return {
            "direction": "NO_TRADE",
            "actionable": False,
            "action_label": "WAIT",
            "avg_confidence": 0.0,
            "threshold": config.trading.min_edge,
            "jury_threshold": JURY.threshold,
            "jury_size": JURY.size,
            "unanimous": False,
            "reason": "Insufficient BTC lookback for judges.",
            "judges": [],
        }

    ctx = MarketContext(
        current_binance_price=btc_now,
        market_start_price=start_price,
        recent_prices=price_list,
        recent_timestamps=ts_list,
        poly_up_price=up_mid,
        poly_down_price=down_mid,
        seconds_elapsed=seconds_elapsed,
        seconds_remaining=seconds_remaining,
        poly_up_bid=_to_float(latest_odds.get("up_best_bid")),
        poly_up_ask=_to_float(latest_odds.get("up_best_ask")),
        poly_down_bid=_to_float(latest_odds.get("down_best_bid")),
        poly_down_ask=_to_float(latest_odds.get("down_best_ask")),
        recent_results=recent_results,
    )

    decision = JURY.deliberate(ctx)

    vote_counts = {"UP": 0, "DOWN": 0, "ABSTAIN": 0}
    judge_rows = []
    for v in decision.verdicts:
        vote_counts[v.vote.value] = vote_counts.get(v.vote.value, 0) + 1
        judge_rows.append(
            {
                "name": v.judge_name,
                "vote": v.vote.value,
                "confidence": v.confidence,
                "reason": v.reason,
            }
        )

    jury_size = len(decision.verdicts)
    support_votes = vote_counts.get(decision.direction, 0) if decision.direction in ("UP", "DOWN") else 0
    support_ratio = (support_votes / float(jury_size)) if jury_size > 0 else 0.0
    base_actionable = (
        decision.direction in ("UP", "DOWN")
        and decision.avg_confidence >= config.trading.min_edge
        and seconds_remaining > config.trading.cutoff_before_close_seconds
    )

    gate_result = None
    entry_price = None
    if decision.direction == "UP":
        entry_price = _to_float(latest_odds.get("up_best_ask")) or up_mid
    elif decision.direction == "DOWN":
        entry_price = _to_float(latest_odds.get("down_best_ask")) or down_mid

    if (
        base_actionable
        and decision.direction in ("UP", "DOWN")
        and entry_price is not None
        and 0.0 < entry_price < 1.0
        and jury_size > 0
    ):
        gate_result = evaluate_entry_gate(
            direction=decision.direction,
            entry_price=float(entry_price),
            current_price=btc_now,
            start_price=start_price,
            seconds_elapsed=seconds_elapsed,
            jury_confidence=decision.avg_confidence,
            support_ratio=support_ratio,
            seconds_remaining=seconds_remaining,
            recent_prices=price_list,
            recent_timestamps=ts_list,
            poly_up_ask=_to_float(latest_odds.get("up_best_ask")),
            poly_down_ask=_to_float(latest_odds.get("down_best_ask")),
            recent_results=recent_results,
        )

    paper_filter_ok = True
    paper_filter_reason = ""
    if base_actionable and gate_result is not None and decision.direction in ("UP", "DOWN"):
        if seconds_elapsed < PAPER_ALIGN_ENTRY_START_SEC or seconds_elapsed > PAPER_ALIGN_ENTRY_END_SEC:
            paper_filter_ok = False
            paper_filter_reason = (
                f"paper timing gate: elapsed={seconds_elapsed:.0f}s not in "
                f"[{PAPER_ALIGN_ENTRY_START_SEC:.0f},{PAPER_ALIGN_ENTRY_END_SEC:.0f}]"
            )
        elif seconds_remaining < PAPER_ALIGN_MIN_SECONDS_REMAINING:
            paper_filter_ok = False
            paper_filter_reason = (
                f"paper timing gate: remaining={seconds_remaining:.0f}s < {PAPER_ALIGN_MIN_SECONDS_REMAINING:.0f}s"
            )
        else:
            tick_samples, odds_samples = _window_sample_counts(conn, ws, now_ts)
            if tick_samples < PAPER_ALIGN_MIN_TICK_SAMPLES:
                paper_filter_ok = False
                paper_filter_reason = (
                    f"paper sample gate: ticks={tick_samples} < {PAPER_ALIGN_MIN_TICK_SAMPLES}"
                )
            elif odds_samples < PAPER_ALIGN_MIN_ODDS_SAMPLES:
                paper_filter_ok = False
                paper_filter_reason = (
                    f"paper sample gate: odds={odds_samples} < {PAPER_ALIGN_MIN_ODDS_SAMPLES}"
                )
            elif support_ratio < PAPER_ALIGN_MIN_SUPPORT_RATIO:
                paper_filter_ok = False
                paper_filter_reason = (
                    f"paper jury gate: support={support_ratio:.1%} < {PAPER_ALIGN_MIN_SUPPORT_RATIO:.1%}"
                )
            elif gate_result.expected_roi < PAPER_ALIGN_MIN_EXPECTED_ROI:
                paper_filter_ok = False
                paper_filter_reason = (
                    f"paper EV gate: net_ev={gate_result.expected_roi:+.3%} < {PAPER_ALIGN_MIN_EXPECTED_ROI:.3%}"
                )
            else:
                short_move = _recent_move_pct(
                    conn,
                    ws,
                    now_ts=now_ts,
                    lookback_sec=PAPER_ALIGN_RECENT_MOVE_LOOKBACK_SEC,
                )
                if short_move is None:
                    paper_filter_ok = False
                    paper_filter_reason = "paper momentum gate: insufficient short-term ticks"
                elif decision.direction == "UP" and short_move < PAPER_ALIGN_MIN_RECENT_MOVE_PCT:
                    paper_filter_ok = False
                    paper_filter_reason = (
                        f"paper momentum gate: move={short_move:+.4f}% < +{PAPER_ALIGN_MIN_RECENT_MOVE_PCT:.4f}%"
                    )
                elif decision.direction == "DOWN" and short_move > -PAPER_ALIGN_MIN_RECENT_MOVE_PCT:
                    paper_filter_ok = False
                    paper_filter_reason = (
                        f"paper momentum gate: move={short_move:+.4f}% > -{PAPER_ALIGN_MIN_RECENT_MOVE_PCT:.4f}%"
                    )
        if paper_filter_ok and decision.avg_confidence < PAPER_ALIGN_MIN_CONFIDENCE:
            paper_filter_ok = False
            paper_filter_reason = (
                f"paper confidence gate: conf={decision.avg_confidence:.3f} < {PAPER_ALIGN_MIN_CONFIDENCE:.3f}"
            )
        if paper_filter_ok and entry_price is not None and entry_price > PAPER_ALIGN_MAX_ENTRY_PRICE:
            paper_filter_ok = False
            paper_filter_reason = (
                f"paper ask gate: ask={entry_price:.3f} > {PAPER_ALIGN_MAX_ENTRY_PRICE:.3f}"
            )

    actionable = base_actionable and gate_result is not None and gate_result.allow and paper_filter_ok
    action_label = f"BUY {decision.direction}" if actionable else "WAIT"

    blocked_by = "none"
    blocked_reason = ""
    if base_actionable and gate_result is None:
        blocked_by = "invalid_entry_price"
        blocked_reason = "skip invalid entry price for current orderbook"
    elif base_actionable and gate_result is not None and not gate_result.allow:
        blocked_by = "entry_gate"
        blocked_reason = gate_result.reason
    elif base_actionable and gate_result is not None and gate_result.allow and not paper_filter_ok:
        blocked_by = "paper_filter"
        blocked_reason = paper_filter_reason
    elif not base_actionable:
        blocked_by = "jury_or_timing"
        blocked_reason = (
            f"votes UP={vote_counts['UP']} DOWN={vote_counts['DOWN']} "
            f"ABSTAIN={vote_counts['ABSTAIN']}"
        )

    if actionable and gate_result is not None:
        summary = (
            f"{vote_counts[decision.direction]}/{jury_size} {decision.direction} votes | "
            f"{gate_result.reason}"
        )
    elif base_actionable and gate_result is not None and not paper_filter_ok:
        summary = paper_filter_reason
    elif base_actionable and gate_result is not None:
        summary = gate_result.reason
    elif base_actionable and gate_result is None:
        summary = "skip invalid entry price for current orderbook"
    else:
        summary = (
            f"votes UP={vote_counts['UP']} DOWN={vote_counts['DOWN']} "
            f"ABSTAIN={vote_counts['ABSTAIN']}"
        )

    gate_payload = {
        "evaluated": bool(gate_result is not None),
        "allow": (bool(gate_result.allow) if gate_result is not None else None),
        "reason": gate_result.reason if gate_result is not None else None,
        "expected_roi": gate_result.expected_roi if gate_result is not None else None,
        "model_prob": gate_result.model_prob if gate_result is not None else None,
        "fair_prob_up": gate_result.fair_prob_up if gate_result is not None else None,
        "break_even_prob": gate_result.break_even_prob if gate_result is not None else None,
        "dispersion": gate_result.dispersion if gate_result is not None else None,
        "entry_price": entry_price,
        "per_judge_probs": (
            ({str(k): float(v) for k, v in (gate_result.per_judge_probs or {}).items()} if gate_result is not None else {})
        ),
        "blocked_by": blocked_by,
        "blocked_reason": blocked_reason if blocked_reason else None,
    }

    return {
        "direction": decision.direction,
        "actionable": actionable,
        "action_label": action_label,
        "avg_confidence": decision.avg_confidence,
        "threshold": config.trading.min_edge,
        "jury_threshold": JURY.threshold,
        "jury_size": jury_size,
        "unanimous": decision.unanimous,
        "reason": summary,
        "entry_price": entry_price,
        "expected_roi": gate_result.expected_roi if gate_result is not None else None,
        "model_prob": gate_result.model_prob if gate_result is not None else None,
        "break_even_prob": gate_result.break_even_prob if gate_result is not None else None,
        "fair_prob_up": gate_result.fair_prob_up if gate_result is not None else None,
        "dispersion": gate_result.dispersion if gate_result is not None else None,
        "gate": gate_payload,
        "judges": judge_rows,
    }


def _record_signal_history(conn, now_ts: float, window: Optional[dict], market: dict, signal: dict) -> bool:
    global _LAST_SIGNAL_HISTORY_KEY

    judges = signal.get("judges") or []
    if not judges:
        return False

    up_votes = sum(1 for j in judges if str(j.get("vote")) == "UP")
    down_votes = sum(1 for j in judges if str(j.get("vote")) == "DOWN")
    support_votes = max(up_votes, down_votes)
    if up_votes > down_votes:
        support_direction = "UP"
    elif down_votes > up_votes:
        support_direction = "DOWN"
    else:
        support_direction = "NONE"

    actionable = bool(signal.get("actionable"))
    if actionable:
        history_type = "accepted"
    else:
        # Rejected history is stored only if >=3 judges supported one side.
        if support_votes < 3 or support_direction not in ("UP", "DOWN"):
            return False
        history_type = "rejected"

    ws = _to_int(window.get("window_start")) if window else None
    we = _to_int(window.get("window_end")) if window else None
    slug = str(window.get("slug")) if window and window.get("slug") else None
    direction = str(signal.get("direction") or "NO_TRADE")
    reason = str(signal.get("reason") or "")
    avg_conf = round(float(signal.get("avg_confidence") or 0.0), 3)
    vote_sig = ",".join(
        f"{j.get('name','?')}:{j.get('vote','ABSTAIN')}"
        for j in judges
    )
    elapsed_bucket = 0
    if ws is not None:
        elapsed_bucket = int(max(0.0, now_ts - float(ws)) // 30)
    btc_change = _to_float(market.get("btc_change_pct"))
    btc_bucket = round((btc_change or 0.0) / 0.05) * 0.05
    conf_bucket = round(avg_conf / 0.05) * 0.05
    key = (
        f"{ws}|{history_type}|{support_direction}|{support_votes}|{direction}|"
        f"{conf_bucket:.2f}|{btc_bucket:.2f}|{elapsed_bucket}|{vote_sig}"
    )
    if key == _LAST_SIGNAL_HISTORY_KEY:
        return False
    last_ts = _LAST_SIGNAL_HISTORY_TS.get(key)
    if last_ts is not None and (now_ts - last_ts) < 20.0:
        return False
    _LAST_SIGNAL_HISTORY_KEY = key
    _LAST_SIGNAL_HISTORY_TS[key] = now_ts

    ts_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
    try:
        execute_write(
            conn,
            """INSERT INTO signal_history
               (ts, ts_utc, window_start, window_end, slug, history_type, support_direction, support_votes,
                direction, avg_confidence, threshold,
                reason, btc_change_pct, up_mid, down_mid, judges_json, gate_json, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                float(now_ts),
                ts_utc,
                ws,
                we,
                slug,
                history_type,
                support_direction,
                int(support_votes),
                direction,
                avg_conf,
                float(signal.get("threshold") or 0.0),
                reason,
                btc_change,
                _to_float(market.get("up_mid")),
                _to_float(market.get("down_mid")),
                json.dumps(judges, ensure_ascii=False),
                json.dumps(signal.get("gate") or {}, ensure_ascii=False),
                key,
            ),
        )
        return True
    except Exception as e:
        # Ignore duplicate-key inserts from highly similar snapshots.
        msg = str(e)
        if ("UNIQUE constraint failed" in msg) or ("Duplicate entry" in msg):
            return False
        logger.debug("signal_history insert error: %s", e)
        return False


def build_signal_history(limit: int = 40, offset: int = 0, history_type: str = "all") -> dict:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    h_type = (history_type or "all").strip().lower()
    if h_type not in ("all", "accepted", "rejected"):
        h_type = "all"
    try:
        conn = _connect_db()
    except Exception as e:
        return {"ok": False, "error": f"Database connection error ({db_label()}): {e}"}

    try:
        where = ""
        params: list[Any] = []
        if h_type in ("accepted", "rejected"):
            where = "WHERE history_type = ?"
            params.append(h_type)

        rows = fetch_all_dicts(
            conn,
            f"""SELECT ts, ts_utc, window_start, window_end, slug, history_type,
                      support_direction, support_votes, direction, avg_confidence,
                      threshold, reason, btc_change_pct, up_mid, down_mid, judges_json, gate_json
               FROM signal_history
               {where}
               ORDER BY ts DESC
               LIMIT ? OFFSET ?""",
            tuple(params + [lim, off]),
        )
        items = []
        for r in rows:
            judges_json = r.get("judges_json")
            try:
                judges = json.loads(judges_json) if judges_json else []
            except Exception:
                judges = []
            gate_json = r.get("gate_json")
            try:
                gate = json.loads(gate_json) if gate_json else {}
            except Exception:
                gate = {}
            items.append(
                {
                    "ts": _to_float(r.get("ts")),
                    "ts_utc": str(r.get("ts_utc") or ""),
                    "window_start": _to_int(r.get("window_start")),
                    "window_end": _to_int(r.get("window_end")),
                    "slug": str(r.get("slug")) if r.get("slug") else None,
                    "history_type": str(r.get("history_type") or "rejected"),
                    "support_direction": str(r.get("support_direction") or "NONE"),
                    "support_votes": _to_int(r.get("support_votes")) or 0,
                    "direction": str(r.get("direction") or "NO_TRADE"),
                    "avg_confidence": _to_float(r.get("avg_confidence")) or 0.0,
                    "threshold": _to_float(r.get("threshold")) or 0.0,
                    "reason": str(r.get("reason") or ""),
                    "market": {
                        "btc_change_pct": _to_float(r.get("btc_change_pct")),
                        "up_mid": _to_float(r.get("up_mid")),
                        "down_mid": _to_float(r.get("down_mid")),
                    },
                    "gate": gate if isinstance(gate, dict) else {},
                    "judges": judges,
                }
            )
        cnt_where = ""
        cnt_params: list[Any] = []
        if h_type in ("accepted", "rejected"):
            cnt_where = "WHERE history_type = ?"
            cnt_params.append(h_type)
        cnt_row = fetch_one(conn, f"SELECT COUNT(*) FROM signal_history {cnt_where}", tuple(cnt_params))
        count = int(cnt_row[0]) if cnt_row else len(items)
        return {
            "ok": True,
            "items": items,
            "count": count,
            "limit": lim,
            "offset": off,
            "history_type": h_type,
        }
    finally:
        conn.close()


def build_paper_trade_history(limit: int = 30, offset: int = 0) -> dict:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    try:
        conn = _connect_db()
    except Exception as e:
        return {"ok": False, "error": f"Database connection error ({db_label()}): {e}"}

    try:
        try:
            rows = fetch_all_dicts(
                conn,
                """SELECT id, window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
                          potential_win_pnl, signal_confidence, signal_reason, close_reason, status,
                          opened_at, closed_at, actual_outcome, won, pnl, roi_pct
                   FROM paper_trades
                   ORDER BY window_start DESC
                   LIMIT ? OFFSET ?""",
                (lim, off),
            )
        except Exception:
            # Backward compatibility: old schema without close_reason column.
            rows = fetch_all_dicts(
                conn,
                """SELECT id, window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
                          potential_win_pnl, signal_confidence, signal_reason, status,
                          opened_at, closed_at, actual_outcome, won, pnl, roi_pct
                   FROM paper_trades
                   ORDER BY window_start DESC
                   LIMIT ? OFFSET ?""",
                (lim, off),
            )
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"paper_trades unavailable: {e}"}

    try:
        count_row = fetch_one(conn, "SELECT COUNT(*) FROM paper_trades")
        count = int(count_row[0]) if count_row else len(rows)
        stats_row = fetch_one(
            conn,
            """SELECT
                   SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END),
                   COALESCE(SUM(pnl), 0)
               FROM paper_trades""",
        )
        open_cnt = int(stats_row[0] or 0) if stats_row else 0
        closed_cnt = int(stats_row[1] or 0) if stats_row else 0
        wins = int(stats_row[2] or 0) if stats_row else 0
        losses = int(stats_row[3] or 0) if stats_row else 0
        total_pnl = float(stats_row[4] or 0.0) if stats_row else 0.0

        initial_capital = None
        try:
            first_cap_row = fetch_one(
                conn,
                """SELECT initial_capital
                   FROM paper_trades
                   WHERE initial_capital IS NOT NULL
                   ORDER BY window_start ASC
                   LIMIT 1""",
            )
            if first_cap_row and first_cap_row[0] is not None:
                initial_capital = float(first_cap_row[0])
        except Exception:
            initial_capital = None

        if initial_capital is None:
            first_stake_row = fetch_one(
                conn,
                "SELECT stake FROM paper_trades ORDER BY window_start ASC LIMIT 1",
            )
            initial_capital = float(first_stake_row[0]) if first_stake_row and first_stake_row[0] is not None else 1000.0
        current_equity = initial_capital + total_pnl
        equity_roi_pct = ((total_pnl / initial_capital) * 100.0) if initial_capital > 0 else 0.0
        is_account_busted = current_equity <= 0.0

        closed_rows = fetch_all_dicts(
            conn,
            """SELECT id, pnl
               FROM paper_trades
               WHERE status='CLOSED'
               ORDER BY
                 CASE
                   WHEN closed_at IS NOT NULL THEN closed_at
                   ELSE window_end
                 END ASC,
                 id ASC""",
        )

        bust_count = 0
        max_drawdown_pct = 0.0
        max_consecutive_losses = 0
        running_loss_streak = 0
        peak_equity = initial_capital
        equity_cursor = initial_capital
        prev_equity = initial_capital

        for row in closed_rows:
            pnl = _to_float(row.get("pnl")) or 0.0
            equity_cursor += pnl
            if prev_equity > 0.0 and equity_cursor <= 0.0:
                bust_count += 1
            prev_equity = equity_cursor

            if pnl < 0.0:
                running_loss_streak += 1
                if running_loss_streak > max_consecutive_losses:
                    max_consecutive_losses = running_loss_streak
            else:
                running_loss_streak = 0

            if equity_cursor > peak_equity:
                peak_equity = equity_cursor
            if peak_equity > 0.0:
                dd_pct = ((peak_equity - equity_cursor) / peak_equity) * 100.0
                if dd_pct > max_drawdown_pct:
                    max_drawdown_pct = dd_pct

        items: list[dict[str, Any]] = []
        for r in rows:
            ws = _to_int(r.get("window_start"))
            opened_at = _to_float(r.get("opened_at"))

            window_row = None
            if ws is not None:
                window_row = fetch_one_dict(
                    conn,
                    """SELECT slug, btc_start_price, btc_end_price, actual_outcome
                       FROM market_windows
                       WHERE window_start = ?
                       LIMIT 1""",
                    (ws,),
                )

            odds_row = None
            if ws is not None and opened_at is not None:
                odds_row = fetch_one_dict(
                    conn,
                    """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
                       FROM poly_odds
                       WHERE window_start = ?
                       ORDER BY ABS(ts - ?) ASC
                       LIMIT 1""",
                    (ws, opened_at),
                )

            direction = str(r.get("direction") or "NO_TRADE")
            entry_price = _to_float(r.get("entry_price"))
            shares = _to_float(r.get("shares"))
            stake = _to_float(r.get("stake"))
            to_win_total = shares
            to_win_pnl = _to_float(r.get("potential_win_pnl"))
            if to_win_pnl is None and shares is not None and stake is not None:
                to_win_pnl = shares - stake

            entry_side_price = None
            if odds_row:
                if direction == "UP":
                    entry_side_price = _to_float(odds_row.get("up_best_ask")) or _to_float(odds_row.get("up_mid"))
                elif direction == "DOWN":
                    entry_side_price = _to_float(odds_row.get("down_best_ask")) or _to_float(odds_row.get("down_mid"))

            items.append(
                {
                    "id": _to_int(r.get("id")),
                    "window_start": ws,
                    "window_end": _to_int(r.get("window_end")),
                    "direction": direction,
                    "stake": stake,
                    "entry_price": entry_price,
                    "entry_side_price_at_signal": entry_side_price,
                    "payout_multiple": _to_float(r.get("payout_multiple")),
                    "shares": shares,
                    "to_win_total": to_win_total,
                    "to_win_pnl": to_win_pnl,
                    "signal_confidence": _to_float(r.get("signal_confidence")),
                    "signal_reason": str(r.get("signal_reason") or ""),
                    "close_reason": str(r.get("close_reason") or "") if r.get("close_reason") else None,
                    "status": str(r.get("status") or "OPEN"),
                    "opened_at": opened_at,
                    "opened_at_utc": (
                        datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                        if opened_at is not None
                        else None
                    ),
                    "closed_at": _to_float(r.get("closed_at")),
                    "actual_outcome": str(r.get("actual_outcome")) if r.get("actual_outcome") else None,
                    "won": _to_int(r.get("won")),
                    "pnl": _to_float(r.get("pnl")),
                    "roi_pct": _to_float(r.get("roi_pct")),
                    "window": {
                        "slug": str(window_row.get("slug")) if window_row and window_row.get("slug") else None,
                        "btc_start_price": _to_float(window_row.get("btc_start_price")) if window_row else None,
                        "btc_end_price": _to_float(window_row.get("btc_end_price")) if window_row else None,
                        "actual_outcome": (
                            str(window_row.get("actual_outcome"))
                            if window_row and window_row.get("actual_outcome")
                            else None
                        ),
                    },
                    "odds_at_entry": {
                        "ts": _to_float(odds_row.get("ts")) if odds_row else None,
                        "up_mid": _to_float(odds_row.get("up_mid")) if odds_row else None,
                        "down_mid": _to_float(odds_row.get("down_mid")) if odds_row else None,
                        "up_bid": _to_float(odds_row.get("up_best_bid")) if odds_row else None,
                        "up_ask": _to_float(odds_row.get("up_best_ask")) if odds_row else None,
                        "down_bid": _to_float(odds_row.get("down_best_bid")) if odds_row else None,
                        "down_ask": _to_float(odds_row.get("down_best_ask")) if odds_row else None,
                    },
                }
            )

        return {
            "ok": True,
            "items": items,
            "count": count,
            "limit": lim,
            "offset": off,
            "summary": {
                "open": open_cnt,
                "closed": closed_cnt,
                "wins": wins,
                "losses": losses,
                "win_rate": (wins / closed_cnt) if closed_cnt > 0 else 0.0,
                "total_pnl": total_pnl,
                "initial_capital": initial_capital,
                "current_equity": current_equity,
                "equity_roi_pct": equity_roi_pct,
                "bust_count": bust_count,
                "is_account_busted": is_account_busted,
                "max_drawdown_pct": max_drawdown_pct,
                "max_consecutive_losses": max_consecutive_losses,
            },
        }
    finally:
        conn.close()


def build_snapshot() -> dict:
    now_ts = time.time()
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    if is_sqlite_backend() and not DB_PATH.exists():
        return {
            "ok": False,
            "error": f"Database not found: {DB_PATH}",
            "server_time": now_ts,
            "server_time_utc": now_iso,
        }

    try:
        conn = _connect_db()
    except Exception as e:
        return {
            "ok": False,
            "error": f"Database connection error ({db_label()}): {e}",
            "server_time": now_ts,
            "server_time_utc": now_iso,
        }
    try:
        latest_tick = _get_latest_tick(conn)
        window = _get_latest_window(conn, now_ts)
        ws = _to_int(window["window_start"]) if window else None
        latest_odds = _get_latest_odds_for_window(conn, ws)

        tick_ts = _to_float(latest_tick["ts"]) if latest_tick else None
        odds_ts = _to_float(latest_odds["ts"]) if latest_odds else None

        last_tick_age = (now_ts - tick_ts) if tick_ts is not None else None
        last_odds_age = (now_ts - odds_ts) if odds_ts is not None else None

        if window:
            window_start = _to_int(window["window_start"])
            window_end = _to_int(window["window_end"])
            if window_start is not None and window_end is not None:
                elapsed = max(0.0, now_ts - float(window_start))
                remain = max(0.0, float(window_end) - now_ts)
                progress = min(100.0, max(0.0, (elapsed / max(window_end - window_start, 1)) * 100.0))
            else:
                elapsed = remain = progress = 0.0
        else:
            window_start = window_end = None
            elapsed = remain = progress = 0.0

        start_price = (
            _get_market_start_price(conn, window_start, window_end, window["btc_start_price"])
            if window and window_start is not None and window_end is not None
            else None
        )
        btc_price = _to_float(latest_tick["price"]) if latest_tick else None
        btc_change_pct = (
            ((btc_price - start_price) / start_price) * 100.0
            if btc_price is not None and start_price is not None and start_price > 0
            else None
        )

        signal = _build_signal(conn, now_ts, window, latest_tick, latest_odds)
        last_actionable_signal = _get_last_actionable_signal(conn, now_ts)
        recent_windows = _window_rows(conn, limit=12)
        market_obj = {
            "btc_price": btc_price,
            "btc_start_price": start_price,
            "btc_change_pct": btc_change_pct,
            "up_mid": _to_float(latest_odds["up_mid"]) if latest_odds else None,
            "down_mid": _to_float(latest_odds["down_mid"]) if latest_odds else None,
            "up_bid": _to_float(latest_odds["up_best_bid"]) if latest_odds else None,
            "up_ask": _to_float(latest_odds["up_best_ask"]) if latest_odds else None,
            "down_bid": _to_float(latest_odds["down_best_bid"]) if latest_odds else None,
            "down_ask": _to_float(latest_odds["down_best_ask"]) if latest_odds else None,
        }
        inserted = _record_signal_history(conn, now_ts, window, market_obj, signal)
        if inserted:
            conn.commit()

        snapshot = {
            "ok": True,
            "server_time": now_ts,
            "server_time_utc": now_iso,
            "collector": {
                "running": (
                    (last_tick_age is not None and last_tick_age <= 5.0)
                    and (last_odds_age is not None and last_odds_age <= 10.0)
                ),
                "last_tick_age_sec": last_tick_age,
                "last_odds_age_sec": last_odds_age,
            },
            "window": {
                "slug": str(window["slug"]) if window and window["slug"] else None,
                "window_start": window_start,
                "window_end": window_end,
                "seconds_elapsed": elapsed,
                "seconds_remaining": remain,
                "progress_pct": progress,
            },
            "market": market_obj,
            "signal": signal,
            "last_actionable_signal": last_actionable_signal,
            "stats": _stats(conn),
            "recent_windows": recent_windows,
        }
        return snapshot
    finally:
        conn.close()


def build_history(minutes: int = 30) -> dict:
    now_ts = time.time()
    if is_sqlite_backend() and not DB_PATH.exists():
        return {"ok": False, "error": "Database not found."}

    minutes = max(5, min(minutes, 240))
    start_ts = now_ts - minutes * 60.0

    try:
        conn = _connect_db()
    except Exception as e:
        return {"ok": False, "error": f"Database connection error ({db_label()}): {e}"}
    try:
        btc_rows = fetch_all_dicts(
            conn,
            """SELECT ts, price FROM btc_ticks
               WHERE ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (start_ts, now_ts),
        )
        odds_rows = fetch_all_dicts(
            conn,
            """SELECT ts, up_mid, down_mid FROM poly_odds
               WHERE ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (start_ts, now_ts),
        )

        btc = _downsample(
            [
                {"ts": _to_float(r["ts"]), "value": _to_float(r["price"])}
                for r in btc_rows
                if _to_float(r["ts"]) is not None and _to_float(r["price"]) is not None
            ],
            max_points=320,
        )
        up = _downsample(
            [
                {"ts": _to_float(r["ts"]), "value": _to_float(r["up_mid"])}
                for r in odds_rows
                if _to_float(r["ts"]) is not None and _to_float(r["up_mid"]) is not None
            ],
            max_points=320,
        )
        down = _downsample(
            [
                {"ts": _to_float(r["ts"]), "value": _to_float(r["down_mid"])}
                for r in odds_rows
                if _to_float(r["ts"]) is not None and _to_float(r["down_mid"]) is not None
            ],
            max_points=320,
        )

        return {
            "ok": True,
            "minutes": minutes,
            "btc": btc,
            "up": up,
            "down": down,
        }
    finally:
        conn.close()


def _python_command(script_name: str, args: list[str]) -> list[str]:
    script_path = BASE_DIR / script_name
    return [sys.executable, str(script_path), *args]


def control_paper_start(stake: float, interval: float, sizing_mode: str = "adaptive") -> dict:
    stake = max(1.0, float(stake))
    interval = max(0.5, float(interval))
    mode = str(sizing_mode or "adaptive").strip().lower()
    if mode not in ("adaptive", "all_in_fixed", "all_in_equity"):
        mode = "adaptive"
    ok, msg = PAPER_SIM_PROC.start(
        _python_command(
            "paper_trade_sim.py",
            ["--stake", str(stake), "--interval", str(interval), "--sizing-mode", mode],
        ),
        meta={"stake": stake, "interval": interval, "sizing_mode": mode},
    )
    status = PAPER_SIM_PROC.status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_paper_stop() -> dict:
    ok, msg = PAPER_SIM_PROC.stop()
    status = PAPER_SIM_PROC.status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_paper_reset() -> dict:
    was_running = bool(PAPER_SIM_PROC.status().get("running"))
    stopped_ok = True
    stopped_msg = "already stopped"
    if was_running:
        stopped_ok, stopped_msg = PAPER_SIM_PROC.stop()

    deleted = 0
    conn = None
    try:
        conn = _connect_db()
        count_row = fetch_one(conn, "SELECT COUNT(*) FROM paper_trades")
        deleted = int(count_row[0] or 0) if count_row else 0
        execute_write(conn, "DELETE FROM paper_trades")
        if is_sqlite_backend():
            try:
                execute_write(conn, "DELETE FROM sqlite_sequence WHERE name='paper_trades'")
            except Exception:
                pass
        else:
            try:
                execute_write(conn, "ALTER TABLE paper_trades AUTO_INCREMENT = 1")
            except Exception:
                pass
        conn.commit()
    except Exception as e:
        msg = str(e)
        # If table does not exist yet, treat as already reset.
        if ("no such table" in msg.lower()) or ("doesn't exist" in msg.lower()):
            deleted = 0
        else:
            status = PAPER_SIM_PROC.status()
            status["ok"] = False
            status["message"] = f"reset failed: {msg}"
            status["deleted"] = 0
            status["stopped"] = {"ok": stopped_ok, "message": stopped_msg}
            return status
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    status = PAPER_SIM_PROC.status()
    status["ok"] = True
    status["message"] = "paper history reset"
    status["deleted"] = deleted
    status["stopped"] = {"ok": stopped_ok, "message": stopped_msg}
    return status


def control_live_start(stake: float, position_mode: str = "BOTH") -> dict:
    per_trade_usd = max(0.01, float(stake))
    mode = _normalize_live_position_mode(position_mode)
    account = _fetch_live_account_snapshot()

    if not account.get("configured", False):
        status = LIVE_TRADING_PROC.status()
        status["ok"] = False
        status["message"] = account.get("error") or "API credentials are not configured"
        status["account"] = account
        return status

    if not account.get("ok", False):
        status = LIVE_TRADING_PROC.status()
        status["ok"] = False
        status["message"] = account.get("error") or "Failed to fetch collateral balance"
        status["account"] = account
        return status

    balance = _to_float(account.get("collateral_balance"))
    if balance is not None and per_trade_usd > balance:
        status = LIVE_TRADING_PROC.status()
        status["ok"] = False
        status["message"] = (
            f"Invest amount (${per_trade_usd:.2f}) exceeds collateral balance (${balance:.2f})"
        )
        status["account"] = account
        return status

    env_overrides = {
        "DRY_RUN": "false",
        "MAX_BET_SIZE": f"{per_trade_usd}",
        "POSITION_MODE": mode,
    }
    ok, msg = LIVE_TRADING_PROC.start(
        _python_command("main.py", []),
        meta={
            "stake_per_trade": per_trade_usd,
            "position_mode": mode,
            "dry_run": False,
        },
        env_overrides=env_overrides,
    )
    status = build_live_control_status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_live_stop() -> dict:
    ok, msg = LIVE_TRADING_PROC.stop()
    status = build_live_control_status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_backtest_run(payload: dict[str, Any]) -> dict:
    action_mode = str(payload.get("mode", "single")).strip().lower()
    args: list[str] = []

    last_hours = payload.get("last_hours")
    if last_hours is not None and str(last_hours).strip():
        try:
            args.extend(["--last-hours", str(float(last_hours))])
        except ValueError:
            return {"ok": False, "error": f"Invalid last_hours: {last_hours}"}

    if action_mode == "auto_sweep":
        args.append("--auto-sweep")
        edge_grid = str(payload.get("edge_grid", "0.04,0.06,0.08,0.10,0.12,0.15")).strip()
        jury_grid = str(payload.get("jury_grid", "2,3,4,5")).strip()
        min_trades = int(payload.get("min_trades", 10))
        top = int(payload.get("top", 10))
        json_out = str(payload.get("json_out", "sweep_best.json")).strip()
        args.extend(
            [
                "--edge-grid",
                edge_grid,
                "--jury-grid",
                jury_grid,
                "--min-trades",
                str(max(1, min_trades)),
                "--top",
                str(max(1, top)),
                "--json-out",
                json_out,
            ]
        )
    else:
        min_edge = payload.get("min_edge")
        if min_edge is not None and str(min_edge).strip():
            try:
                args.extend(["--min-edge", str(float(min_edge))])
            except ValueError:
                return {"ok": False, "error": f"Invalid min_edge: {min_edge}"}

        jury_threshold = payload.get("jury_threshold")
        if jury_threshold is not None and str(jury_threshold).strip():
            try:
                args.extend(["--jury-threshold", str(int(jury_threshold))])
            except ValueError:
                return {"ok": False, "error": f"Invalid jury_threshold: {jury_threshold}"}

    ok, msg = BACKTEST_PROC.start(
        _python_command("backtest.py", args),
        meta={
            "mode": action_mode,
            "args": args,
        },
    )
    status = BACKTEST_PROC.status()
    status["message"] = msg
    status["ok"] = ok
    return status


def control_backtest_stop() -> dict:
    ok, msg = BACKTEST_PROC.stop()
    status = BACKTEST_PROC.status()
    status["message"] = msg
    status["ok"] = ok
    return status


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Silence routine HTTP request logs; keep explicit exception logs only.
        return

    def _send_json(self, payload: dict, code: int = 200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path):
        if not file_path.exists():
            self.send_error(404, "Not found")
            return
        content = file_path.read_bytes()
        ctype = CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
            return {}
        except Exception:
            return {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in STATIC_ROUTES:
            self._send_file(STATIC_ROUTES[path])
            return

        if path == "/api/snapshot":
            try:
                self._send_json(build_snapshot(), code=200)
            except Exception as e:
                logger.exception("snapshot error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/history":
            qs = parse_qs(parsed.query)
            try:
                minutes = int(qs.get("minutes", ["30"])[0])
            except ValueError:
                minutes = 30
            try:
                self._send_json(build_history(minutes=minutes), code=200)
            except Exception as e:
                logger.exception("history error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/signal-history":
            qs = parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["40"])[0])
            except ValueError:
                limit = 40
            try:
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            history_type = str(qs.get("type", ["all"])[0] or "all")
            try:
                self._send_json(
                    build_signal_history(limit=limit, offset=offset, history_type=history_type),
                    code=200,
                )
            except Exception as e:
                logger.exception("signal-history error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/paper-history":
            qs = parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            try:
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            try:
                self._send_json(build_paper_trade_history(limit=limit, offset=offset), code=200)
            except Exception as e:
                logger.exception("paper-history error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/paper":
            self._send_json(PAPER_SIM_PROC.status(), code=200)
            return

        if path == "/api/control/live":
            self._send_json(build_live_control_status(), code=200)
            return

        if path == "/api/control/backtest":
            self._send_json(BACKTEST_PROC.status(), code=200)
            return

        if path == "/healthz":
            self._send_json({"ok": True, "time": time.time()})
            return

        self.send_error(404, "Not found")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()

        if path == "/api/control/paper/start":
            stake = payload.get("stake", 1000.0)
            interval = payload.get("interval", 2.0)
            sizing_mode = str(payload.get("sizing_mode", "adaptive"))
            try:
                resp = control_paper_start(float(stake), float(interval), sizing_mode=sizing_mode)
                self._send_json(resp, code=200)
            except Exception as e:
                logger.exception("paper start error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/paper/stop":
            try:
                self._send_json(control_paper_stop(), code=200)
            except Exception as e:
                logger.exception("paper stop error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/paper/reset":
            try:
                self._send_json(control_paper_reset(), code=200)
            except Exception as e:
                logger.exception("paper reset error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/start":
            stake = payload.get("stake", 5.0)
            position_mode = str(payload.get("position_mode", "BOTH"))
            try:
                self._send_json(control_live_start(float(stake), position_mode=position_mode), code=200)
            except Exception as e:
                logger.exception("live start error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/stop":
            try:
                self._send_json(control_live_stop(), code=200)
            except Exception as e:
                logger.exception("live stop error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/backtest/run":
            try:
                self._send_json(control_backtest_run(payload), code=200)
            except Exception as e:
                logger.exception("backtest run error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/backtest/stop":
            try:
                self._send_json(control_backtest_stop(), code=200)
            except Exception as e:
                logger.exception("backtest stop error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        self.send_error(404, "Not found")


def main():
    parser = argparse.ArgumentParser(description="Live dashboard server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8787, help="Bind port")
    args = parser.parse_args()

    if not DASHBOARD_DIR.exists():
        raise RuntimeError(f"Dashboard assets not found: {DASHBOARD_DIR}")

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    logger.info("Dashboard server running at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
