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
    db_label,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
    is_sqlite_backend,
    sqlite_db_path,
)
from judges import Jury, MarketContext


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

    def start(self, command: list[str], meta: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False, f"{self.name} already running"

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
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

    actionable = (
        decision.direction in ("UP", "DOWN")
        and decision.avg_confidence >= config.trading.min_edge
        and seconds_remaining > config.trading.cutoff_before_close_seconds
    )
    action_label = f"BUY {decision.direction}" if actionable else "WAIT"

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
    if actionable:
        summary = (
            f"{vote_counts['UP']}/{jury_size} UP votes" if decision.direction == "UP"
            else f"{vote_counts['DOWN']}/{jury_size} DOWN votes"
        )
    else:
        summary = (
            f"votes UP={vote_counts['UP']} DOWN={vote_counts['DOWN']} "
            f"ABSTAIN={vote_counts['ABSTAIN']}"
        )

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
        "judges": judge_rows,
    }


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
        recent_windows = _window_rows(conn, limit=12)

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
            "market": {
                "btc_price": btc_price,
                "btc_start_price": start_price,
                "btc_change_pct": btc_change_pct,
                "up_mid": _to_float(latest_odds["up_mid"]) if latest_odds else None,
                "down_mid": _to_float(latest_odds["down_mid"]) if latest_odds else None,
                "up_bid": _to_float(latest_odds["up_best_bid"]) if latest_odds else None,
                "up_ask": _to_float(latest_odds["up_best_ask"]) if latest_odds else None,
                "down_bid": _to_float(latest_odds["down_best_bid"]) if latest_odds else None,
                "down_ask": _to_float(latest_odds["down_best_ask"]) if latest_odds else None,
            },
            "signal": signal,
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


def control_paper_start(stake: float, interval: float) -> dict:
    stake = max(1.0, float(stake))
    interval = max(0.5, float(interval))
    ok, msg = PAPER_SIM_PROC.start(
        _python_command(
            "paper_trade_sim.py",
            ["--stake", str(stake), "--interval", str(interval)],
        ),
        meta={"stake": stake, "interval": interval},
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
        logger.info("%s - %s", self.address_string(), fmt % args)

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

        if path == "/api/control/paper":
            self._send_json(PAPER_SIM_PROC.status(), code=200)
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
            try:
                resp = control_paper_start(float(stake), float(interval))
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
