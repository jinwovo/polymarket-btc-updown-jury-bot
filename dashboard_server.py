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
import re
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

from clob_auth import (
    api_credentials_snapshot,
    builder_credentials_snapshot,
    apply_runtime_auth,
    auth_config_status,
    create_authenticated_clob_client,
)
from config import config
from env_paths import PUBLIC_RUNTIME_ENV_PATH, SECRETS_ENV_PATH
from db_config import (
    connect_db,
    execute_write,
    db_label,
    fetch_all_dicts,
    fetch_one,
    fetch_one_dict,
    init_market_schema,
)
from judges import Jury, MarketContext
from trade_gate import evaluate_entry_gate
from telegram_notifier import mask_bot_token, resolve_chat_id, send_telegram_message


BASE_DIR = Path(__file__).parent
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

_ENV_PATH = Path(SECRETS_ENV_PATH)
_PUBLIC_ENV_PATH = Path(PUBLIC_RUNTIME_ENV_PATH)
_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dashboard")

# Keep jury logs quiet for UI polling.
logging.getLogger("judges").setLevel(logging.WARNING)
# Suppress noisy request-level HTTP logs; show only actual errors.
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)
JURY = Jury(threshold=config.trading.jury_threshold)
_LAST_SIGNAL_HISTORY_KEY: Optional[str] = None
_LAST_SIGNAL_HISTORY_TS: dict[str, float] = {}

# Keep UI signal actionability aligned with paper entry core filters.
PAPER_ALIGN_MIN_EXPECTED_ROI = float(os.getenv("PAPER_MIN_EXPECTED_ROI", "0.015"))
PAPER_ALIGN_MIN_SUPPORT_RATIO = float(os.getenv("PAPER_MIN_SUPPORT_RATIO", "0.50"))
PAPER_ALIGN_MIN_TICK_SAMPLES = int(os.getenv("PAPER_MIN_TICK_SAMPLES", "40"))
PAPER_ALIGN_MIN_ODDS_SAMPLES = int(os.getenv("PAPER_MIN_ODDS_SAMPLES", "8"))
PAPER_ALIGN_ENTRY_START_SEC = float(os.getenv("PAPER_ENTRY_START_SEC", "45"))
PAPER_ALIGN_ENTRY_END_SEC = float(os.getenv("PAPER_ENTRY_END_SEC", "270"))
PAPER_ALIGN_MIN_SECONDS_REMAINING = float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))
PAPER_ALIGN_RECENT_MOVE_LOOKBACK_SEC = float(os.getenv("PAPER_RECENT_MOVE_LOOKBACK_SEC", "20"))
PAPER_ALIGN_MIN_RECENT_MOVE_PCT = float(os.getenv("PAPER_MIN_RECENT_MOVE_PCT", "0.0008"))
PAPER_ALIGN_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "0.22"))
PAPER_ALIGN_MAX_ENTRY_PRICE = float(os.getenv("PAPER_MAX_ENTRY_PRICE", "0.52"))


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
LIVE_TRADING_PROC = ManagedProcess("live_trading")  # Legacy single account
LIVE_ACCOUNT_PROCS: dict[int, ManagedProcess] = {}  # Multi-account: {account_id: ManagedProcess}

# -- Multi-market processes --
SIGNAL_BTC15_PROC = ManagedProcess("signal_btc15")
PAPER_BTC15_PROC = ManagedProcess("paper_sim_btc15")
LIVE_BTC15_PROC = ManagedProcess("live_btc15")
SIGNAL_ETH5_PROC = ManagedProcess("signal_eth5")
PAPER_ETH5_PROC = ManagedProcess("paper_sim_eth5")
LIVE_ETH5_PROC = ManagedProcess("live_eth5")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
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


_CLOSE_REASON_EXIT_PX_RE = re.compile(r"(?:^|[|,\s])exit_px=([0-9]*\.?[0-9]+)")
_CLOSE_REASON_FILL_PX_RE = re.compile(r"(?:^|[|,\s])fill_px=([0-9]*\.?[0-9]+)")
_CLOSE_REASON_FILL_NOTIONAL_RE = re.compile(r"(?:^|[|,\s])fill_notional=\$?([0-9]*\.?[0-9]+)")


def _extract_close_reason_prices(close_reason: str | None) -> dict[str, Optional[float]]:
    reason = str(close_reason or "")
    out: dict[str, Optional[float]] = {
        "exit_px": None,
        "fill_px": None,
        "fill_notional": None,
    }
    if not reason:
        return out
    m = _CLOSE_REASON_EXIT_PX_RE.search(reason)
    if m:
        out["exit_px"] = _to_float(m.group(1))
    m = _CLOSE_REASON_FILL_PX_RE.search(reason)
    if m:
        out["fill_px"] = _to_float(m.group(1))
    m = _CLOSE_REASON_FILL_NOTIONAL_RE.search(reason)
    if m:
        out["fill_notional"] = _to_float(m.group(1))
    return out


def _build_exit_snapshot(
    *,
    direction: str,
    status: str,
    won: Optional[int],
    close_reason: str | None,
    odds_close_row: dict | None,
) -> dict[str, Any]:
    prices = _extract_close_reason_prices(close_reason)
    d = str(direction or "NO_TRADE").upper()
    st = str(status or "OPEN").upper()
    reason = str(close_reason or "").lower()

    side_bid = None
    side_mid = None
    if odds_close_row:
        if d == "UP":
            side_bid = _to_float(odds_close_row.get("up_best_bid"))
            side_mid = _to_float(odds_close_row.get("up_mid"))
        elif d == "DOWN":
            side_bid = _to_float(odds_close_row.get("down_best_bid"))
            side_mid = _to_float(odds_close_row.get("down_mid"))
    market_px = side_bid if side_bid is not None else side_mid

    settlement_px = None
    if st == "CLOSED" and ("expiry_settlement" in reason or "recovered_expiry_settlement" in reason):
        if won is not None:
            settlement_px = 1.0 if int(won) == 1 else 0.0

    if st != "CLOSED":
        kind = "open"
    elif settlement_px is not None:
        kind = "settlement"
    elif prices["fill_px"] is not None or prices["exit_px"] is not None:
        kind = "early_exit"
    else:
        kind = "closed"

    return {
        "kind": kind,
        "market_px": market_px,
        "exit_px": prices["exit_px"],
        "fill_px": prices["fill_px"],
        "fill_notional": prices["fill_notional"],
        "settlement_px": settlement_px,
    }


def _normalize_live_position_mode(raw: Any) -> str:
    mode = str(raw or "BOTH").strip().upper()
    if mode in ("UP_ONLY", "DOWN_ONLY", "BOTH"):
        return mode
    return "BOTH"


def _normalize_live_sizing_mode(raw: Any) -> str:
    mode = str(raw or "adaptive").strip().lower()
    if mode in ("adaptive", "adaptive_seed", "fixed"):
        return mode
    return "adaptive"


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


def _normalize_collateral_amount(raw_value: Optional[float], payload: Any) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None

    decimals = _find_numeric_field(
        payload,
        {"decimals", "token_decimals", "asset_decimals", "collateral_decimals"},
    )
    if decimals is not None:
        try:
            dec = int(decimals)
        except (TypeError, ValueError):
            dec = -1
        if 0 <= dec <= 18:
            scale = float(10 ** dec)
            if scale > 1.0 and abs(value - round(value)) < 1e-9 and value >= scale:
                return float(value / scale)

    # Fallback for USDC-style 6 decimals when API omits decimals metadata.
    if abs(value - round(value)) < 1e-9 and value >= 1_000_000:
        return float(value / 1_000_000.0)

    return value


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _set_runtime_var(key: str, value: str | None):
    if value is None or value == "":
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(value)


def _update_env_file(path: Path, updates: dict[str, str | None]):
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = _ENV_KEY_RE.match(line)
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        if key not in updates:
            out.append(line)
            continue
        seen.add(key)
        val = updates[key]
        if val is None or val == "":
            continue
        out.append(f"{key}={val}")

    for key, val in updates.items():
        if key in seen:
            continue
        if val is None or val == "":
            continue
        out.append(f"{key}={val}")

    content = "\n".join(out).rstrip()
    if content:
        content += "\n"
    path.write_text(content, encoding="utf-8")


def _telegram_snapshot() -> dict[str, Any]:
    enabled = bool(getattr(config.trading, "live_telegram_enabled", False))
    token = _clean_str(getattr(config.trading, "live_telegram_bot_token", ""))
    chat_id = _clean_str(getattr(config.trading, "live_telegram_chat_id", ""))
    configured = bool(enabled and token and chat_id)
    return {
        "enabled": enabled,
        "configured": configured,
        "has_token": bool(token),
        "has_chat_id": bool(chat_id),
        "token_masked": mask_bot_token(token),
        "chat_id": chat_id or None,
    }


def _paper_telegram_snapshot() -> dict[str, Any]:
    enabled = bool(getattr(config.trading, "paper_telegram_notify_open", False))
    token = _clean_str(getattr(config.trading, "live_telegram_bot_token", ""))
    chat_id = _clean_str(getattr(config.trading, "live_telegram_chat_id", ""))
    configured = bool(token and chat_id)
    return {
        "enabled": enabled,
        "configured": configured,
        "has_token": bool(token),
        "has_chat_id": bool(chat_id),
        "uses_live_telegram": True,
        "token_masked": mask_bot_token(token),
        "chat_id": chat_id or None,
    }


def _apply_runtime_telegram(*, enabled: bool, bot_token: str, chat_id: str):
    token = _clean_str(bot_token)
    cid = _clean_str(chat_id)
    _set_runtime_var("LIVE_TELEGRAM_ENABLED", "true" if enabled else "false")
    _set_runtime_var("LIVE_TELEGRAM_BOT_TOKEN", token if token else None)
    _set_runtime_var("LIVE_TELEGRAM_CHAT_ID", cid if cid else None)
    config.trading.live_telegram_enabled = bool(enabled)
    config.trading.live_telegram_bot_token = token
    config.trading.live_telegram_chat_id = cid


def _apply_runtime_paper_telegram_notify_open(*, enabled: bool):
    _set_runtime_var("PAPER_TELEGRAM_NOTIFY_OPEN", "true" if enabled else "false")
    config.trading.paper_telegram_notify_open = bool(enabled)


def _default_telegram_test_message() -> str:
    now_utc = datetime.now(timezone.utc).isoformat()
    return (
        "LIVE Telegram test from Future Pulse Trading Station\n"
        f"time={now_utc}"
    )


def control_live_telegram_configure(
    *,
    bot_token: str = "",
    chat_id: str = "",
    enabled: Optional[bool] = None,
    send_test: bool = False,
    test_message: str = "",
) -> dict[str, Any]:
    current_token = _clean_str(getattr(config.trading, "live_telegram_bot_token", ""))
    current_chat = _clean_str(getattr(config.trading, "live_telegram_chat_id", ""))
    current_enabled = bool(getattr(config.trading, "live_telegram_enabled", False))

    token = _clean_str(bot_token) or current_token
    cid = _clean_str(chat_id) or current_chat
    is_enabled = current_enabled if enabled is None else bool(enabled)

    if send_test and token and not cid:
        resolved, err = resolve_chat_id(token, timeout=8.0)
        if resolved:
            cid = resolved
        elif not err:
            err = "chat id auto-resolve failed"
        if err:
            status = build_live_control_status()
            status["ok"] = False
            status["message"] = f"Telegram test failed: {err}"
            status["telegram"] = _telegram_snapshot()
            return status

    _apply_runtime_telegram(enabled=is_enabled, bot_token=token, chat_id=cid)
    _update_env_file(
        _ENV_PATH,
        {
            "LIVE_TELEGRAM_ENABLED": "true" if is_enabled else "false",
            "LIVE_TELEGRAM_BOT_TOKEN": token if token else None,
            "LIVE_TELEGRAM_CHAT_ID": cid if cid else None,
        },
    )

    status = build_live_control_status()
    status["ok"] = True
    status["message"] = (
        "Telegram settings saved. If live process is already running, restart it to apply."
    )

    if send_test:
        text = _clean_str(test_message) or _default_telegram_test_message()
        test_result = send_telegram_message(
            token=token,
            chat_id=cid,
            text=text,
            timeout=8.0,
            auto_resolve_chat=True,
        )
        status["telegram_test"] = {
            "ok": bool(test_result.get("ok")),
            "chat_id": test_result.get("chat_id"),
            "error": test_result.get("error"),
        }
        if bool(test_result.get("ok")):
            resolved_cid = _clean_str(test_result.get("chat_id"))
            if resolved_cid and resolved_cid != _clean_str(getattr(config.trading, "live_telegram_chat_id", "")):
                _apply_runtime_telegram(enabled=is_enabled, bot_token=token, chat_id=resolved_cid)
                _update_env_file(
                    _ENV_PATH,
                    {
                        "LIVE_TELEGRAM_ENABLED": "true" if is_enabled else "false",
                        "LIVE_TELEGRAM_BOT_TOKEN": token if token else None,
                        "LIVE_TELEGRAM_CHAT_ID": resolved_cid,
                    },
                )
            status["message"] = "Telegram settings saved and test message sent."
        else:
            status["ok"] = False
            status["message"] = (
                "Telegram settings saved, but test message failed: "
                f"{test_result.get('error') or 'unknown error'}"
            )

    status["telegram"] = _telegram_snapshot()
    return status


def control_live_telegram_test(
    *,
    bot_token: str = "",
    chat_id: str = "",
    message: str = "",
) -> dict[str, Any]:
    token = _clean_str(bot_token) or _clean_str(getattr(config.trading, "live_telegram_bot_token", ""))
    cid = _clean_str(chat_id) or _clean_str(getattr(config.trading, "live_telegram_chat_id", ""))
    text = _clean_str(message) or _default_telegram_test_message()

    status = build_live_control_status()
    if not token:
        status["ok"] = False
        status["message"] = "Telegram bot token is missing"
        status["telegram"] = _telegram_snapshot()
        return status

    result = send_telegram_message(
        token=token,
        chat_id=cid,
        text=text,
        timeout=8.0,
        auto_resolve_chat=True,
    )
    status["telegram_test"] = {
        "ok": bool(result.get("ok")),
        "chat_id": result.get("chat_id"),
        "error": result.get("error"),
    }
    if bool(result.get("ok")):
        status["ok"] = True
        status["message"] = "Telegram test message sent."
    else:
        status["ok"] = False
        status["message"] = f"Telegram test failed: {result.get('error') or 'unknown'}"
    status["telegram"] = _telegram_snapshot()
    return status


def _fetch_live_account_snapshot() -> dict[str, Any]:
    auth_status = auth_config_status()
    creds_snapshot = api_credentials_snapshot()
    builder_snapshot = builder_credentials_snapshot()
    if not bool(auth_status.get("configured")):
        missing = ", ".join(auth_status.get("missing", [])) or "unknown"
        return {
            "ok": False,
            "configured": False,
            "error": f"Missing required env: {missing}",
            "funder": auth_status.get("funder"),
            "funder_source": auth_status.get("funder_source"),
            "signature_type": auth_status.get("signature_type"),
            "signature_type_source": auth_status.get("signature_type_source"),
            "direct_api_creds": bool(auth_status.get("direct_api_creds")),
            "private_key_set": bool(auth_status.get("private_key_set")),
            "warnings": list(auth_status.get("warnings") or []),
            "api_credentials": creds_snapshot,
            "builder_credentials": builder_snapshot,
            "collateral_balance": None,
            "collateral_allowance": None,
        }

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        client, meta = create_authenticated_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        payload = client.get_balance_allowance(params)

        balance = _find_numeric_field(
            payload,
            {"balance", "available_balance", "asset_balance"},
        )
        allowance = _find_numeric_field(
            payload,
            {"allowance", "available_allowance"},
        )
        balance = _normalize_collateral_amount(balance, payload)
        allowance = _normalize_collateral_amount(allowance, payload)
        return {
            "ok": True,
            "configured": True,
            "error": None,
            "funder": meta.get("funder"),
            "funder_source": meta.get("funder_source"),
            "signature_type": meta.get("signature_type"),
            "signature_type_source": meta.get("signature_type_source"),
            "creds_source": meta.get("creds_source"),
            "private_key_set": bool(meta.get("private_key_set")),
            "warnings": list(meta.get("warnings") or []),
            "api_credentials": api_credentials_snapshot(),
            "builder_credentials": builder_snapshot,
            "collateral_balance": balance,
            "collateral_allowance": allowance,
        }
    except Exception as e:
        return {
            "ok": False,
            "configured": True,
            "error": str(e),
            "funder": auth_status.get("funder"),
            "funder_source": auth_status.get("funder_source"),
            "signature_type": auth_status.get("signature_type"),
            "signature_type_source": auth_status.get("signature_type_source"),
            "direct_api_creds": bool(auth_status.get("direct_api_creds")),
            "private_key_set": bool(auth_status.get("private_key_set")),
            "warnings": list(auth_status.get("warnings") or []),
            "api_credentials": creds_snapshot,
            "builder_credentials": builder_snapshot,
            "collateral_balance": None,
            "collateral_allowance": None,
        }


def _kst_today_start_utc() -> float:
    """Return UTC timestamp for midnight KST (UTC+9) today."""
    KST_OFFSET = 9 * 3600
    kst_now = time.time() + KST_OFFSET
    kst_midnight = (int(kst_now) // 86400) * 86400
    return float(kst_midnight - KST_OFFSET)


def _fetch_live_daily_risk() -> dict[str, Any]:
    """Get today's PnL (KST day) and daily loss limit (40% of Seed Capital)."""
    balance = float(os.getenv("LIVE_EQUITY_SEED_CAPITAL", "40.0"))
    try:
        conn = connect_db()
        init_market_schema(conn)
        conn.commit()
        today_start = _kst_today_start_utc()
        row = fetch_one(
            conn,
            "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM live_trades "
            "WHERE status='CLOSED' AND closed_at >= ?",
            (today_start,),
        )
        conn.close()
        daily_pnl = float(row[0]) if row else 0.0
        daily_trades = int(row[1]) if row else 0
        daily_loss_limit = float(os.getenv("LIVE_DAILY_LOSS_LIMIT", str(max(1.0, balance * 0.40))))
        return {
            "seed_capital": round(balance, 2),
            "daily_pnl": round(daily_pnl, 2),
            "daily_trades": daily_trades,
            "daily_loss_limit": round(daily_loss_limit, 2),
            "daily_loss_remaining": round(max(0, daily_loss_limit + daily_pnl), 2),
        }
    except Exception:
        limit = float(os.getenv("LIVE_DAILY_LOSS_LIMIT", str(max(1.0, balance * 0.40))))
        return {
            "seed_capital": round(balance, 2),
            "daily_pnl": 0.0,
            "daily_trades": 0,
            "daily_loss_limit": round(limit, 2),
            "daily_loss_remaining": round(limit, 2),
        }


def build_live_control_status() -> dict[str, Any]:
    status = LIVE_TRADING_PROC.status()
    status["account"] = _fetch_live_account_snapshot()
    status["telegram"] = _telegram_snapshot()
    status["daily_risk"] = _fetch_live_daily_risk()
    # Persist sizing_mode from env so dropdown shows saved value even when stopped
    saved_sizing = _normalize_live_sizing_mode(os.getenv("LIVE_SIZING_MODE", "adaptive"))
    if not status.get("meta"):
        status["meta"] = {}
    if "sizing_mode" not in (status.get("meta") or {}):
        status["meta"]["sizing_mode"] = saved_sizing
    return status


def build_paper_control_status() -> dict[str, Any]:
    status = PAPER_SIM_PROC.status()
    status["telegram"] = _paper_telegram_snapshot()
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
    """Return official Price to Beat from DB. No Binance tick fallback -- wrong
    start price causes wrong UP/DOWN direction."""
    return _to_float(db_value)


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
        "profit_break_even_prob": gate_result.profit_break_even_prob if gate_result is not None else None,
        "win_prob_floor": gate_result.win_prob_floor if gate_result is not None else None,
        "win_prob_pass": (bool(gate_result.win_prob_pass) if gate_result is not None else None),
        "dispersion": gate_result.dispersion if gate_result is not None else None,
        "aligned_move_pct": gate_result.aligned_move_pct if gate_result is not None else None,
        "boundary_dist_pct": gate_result.boundary_dist_pct if gate_result is not None else None,
        "boundary_sigma_pct": gate_result.boundary_sigma_pct if gate_result is not None else None,
        "alignment_penalty": gate_result.alignment_penalty if gate_result is not None else None,
        "ambiguity_penalty": gate_result.ambiguity_penalty if gate_result is not None else None,
        "up_regime_score": gate_result.up_regime_score if gate_result is not None else None,
        "up_regime_pass": (bool(gate_result.up_regime_pass) if gate_result is not None else None),
        "up_regime_reason": gate_result.up_regime_reason if gate_result is not None else None,
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
        "profit_break_even_prob": gate_result.profit_break_even_prob if gate_result is not None else None,
        "win_prob_floor": gate_result.win_prob_floor if gate_result is not None else None,
        "win_prob_pass": (bool(gate_result.win_prob_pass) if gate_result is not None else None),
        "fair_prob_up": gate_result.fair_prob_up if gate_result is not None else None,
        "dispersion": gate_result.dispersion if gate_result is not None else None,
        "aligned_move_pct": gate_result.aligned_move_pct if gate_result is not None else None,
        "alignment_penalty": gate_result.alignment_penalty if gate_result is not None else None,
        "ambiguity_penalty": gate_result.ambiguity_penalty if gate_result is not None else None,
        "up_regime_score": gate_result.up_regime_score if gate_result is not None else None,
        "up_regime_pass": (bool(gate_result.up_regime_pass) if gate_result is not None else None),
        "up_regime_reason": gate_result.up_regime_reason if gate_result is not None else None,
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
                   WHERE archived_at IS NULL
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
                   WHERE archived_at IS NULL
                   ORDER BY window_start DESC
                   LIMIT ? OFFSET ?""",
                (lim, off),
            )
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"paper_trades unavailable: {e}"}

    try:
        count_row = fetch_one(conn, "SELECT COUNT(*) FROM paper_trades WHERE archived_at IS NULL")
        count = int(count_row[0]) if count_row else len(rows)
        stats_row = fetch_one(
            conn,
            """SELECT
                   SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END),
                   COALESCE(SUM(pnl), 0)
               FROM paper_trades
               WHERE archived_at IS NULL""",
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
                   WHERE initial_capital IS NOT NULL AND archived_at IS NULL
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
                "SELECT stake FROM paper_trades WHERE archived_at IS NULL ORDER BY window_start ASC LIMIT 1",
            )
            initial_capital = float(first_stake_row[0]) if first_stake_row and first_stake_row[0] is not None else 1000.0
        current_equity = initial_capital + total_pnl
        equity_roi_pct = ((total_pnl / initial_capital) * 100.0) if initial_capital > 0 else 0.0
        is_account_busted = current_equity <= 0.0

        closed_rows = fetch_all_dicts(
            conn,
            """SELECT id, pnl
               FROM paper_trades
               WHERE status='CLOSED' AND archived_at IS NULL
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
            closed_at = _to_float(r.get("closed_at"))

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

            odds_close_row = None
            if ws is not None and closed_at is not None:
                odds_close_row = fetch_one_dict(
                    conn,
                    """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
                       FROM poly_odds
                       WHERE window_start = ?
                       ORDER BY ABS(ts - ?) ASC
                       LIMIT 1""",
                    (ws, closed_at),
                )

            direction = str(r.get("direction") or "NO_TRADE")
            entry_price = _to_float(r.get("entry_price"))
            shares = _to_float(r.get("shares"))
            stake = _to_float(r.get("stake"))
            won = _to_int(r.get("won"))
            close_reason = str(r.get("close_reason") or "") if r.get("close_reason") else None
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
                    "close_reason": close_reason,
                    "status": str(r.get("status") or "OPEN"),
                    "opened_at": opened_at,
                    "opened_at_utc": (
                        datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                        if opened_at is not None
                        else None
                    ),
                    "closed_at": closed_at,
                    "actual_outcome": str(r.get("actual_outcome")) if r.get("actual_outcome") else None,
                    "won": won,
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
                    "odds_at_close": {
                        "ts": _to_float(odds_close_row.get("ts")) if odds_close_row else None,
                        "up_mid": _to_float(odds_close_row.get("up_mid")) if odds_close_row else None,
                        "down_mid": _to_float(odds_close_row.get("down_mid")) if odds_close_row else None,
                        "up_bid": _to_float(odds_close_row.get("up_best_bid")) if odds_close_row else None,
                        "up_ask": _to_float(odds_close_row.get("up_best_ask")) if odds_close_row else None,
                        "down_bid": _to_float(odds_close_row.get("down_best_bid")) if odds_close_row else None,
                        "down_ask": _to_float(odds_close_row.get("down_best_ask")) if odds_close_row else None,
                    },
                    "exit": _build_exit_snapshot(
                        direction=direction,
                        status=str(r.get("status") or "OPEN"),
                        won=won,
                        close_reason=close_reason,
                        odds_close_row=odds_close_row,
                    ),
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


def build_live_trade_history(limit: int = 30, offset: int = 0) -> dict:
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
                          potential_win_pnl, signal_confidence, signal_reason, entry_source, close_reason, status,
                          opened_at, closed_at, actual_outcome, won, pnl, roi_pct
                   FROM live_trades
                   ORDER BY window_start DESC
                   LIMIT ? OFFSET ?""",
                (lim, off),
            )
        except Exception:
            rows = fetch_all_dicts(
                conn,
                """SELECT id, window_start, window_end, direction, stake, entry_price, payout_multiple, shares,
                          potential_win_pnl, signal_confidence, signal_reason, status,
                          opened_at, closed_at, actual_outcome, won, pnl, roi_pct
                   FROM live_trades
                   ORDER BY window_start DESC
                   LIMIT ? OFFSET ?""",
                (lim, off),
            )
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"live_trades unavailable: {e}"}

    try:
        count_row = fetch_one(conn, "SELECT COUNT(*) FROM live_trades")
        count = int(count_row[0]) if count_row else len(rows)
        stats_row = fetch_one(
            conn,
            """SELECT
                   SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN won=0 AND status='CLOSED' THEN 1 ELSE 0 END),
                   COALESCE(SUM(pnl), 0),
                   AVG(CASE WHEN status='CLOSED' THEN roi_pct ELSE NULL END),
                   AVG(CASE WHEN status='CLOSED' THEN stake ELSE NULL END)
               FROM live_trades""",
        )
        open_cnt = int(stats_row[0] or 0) if stats_row else 0
        closed_cnt = int(stats_row[1] or 0) if stats_row else 0
        wins = int(stats_row[2] or 0) if stats_row else 0
        losses = int(stats_row[3] or 0) if stats_row else 0
        total_pnl = float(stats_row[4] or 0.0) if stats_row else 0.0
        avg_roi_pct = float(stats_row[5] or 0.0) if stats_row and stats_row[5] is not None else 0.0
        avg_stake = float(stats_row[6] or 0.0) if stats_row and stats_row[6] is not None else 0.0

        items: list[dict[str, Any]] = []
        for r in rows:
            ws = _to_int(r.get("window_start"))
            opened_at = _to_float(r.get("opened_at"))
            closed_at = _to_float(r.get("closed_at"))

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

            odds_close_row = None
            if ws is not None and closed_at is not None:
                odds_close_row = fetch_one_dict(
                    conn,
                    """SELECT ts, up_mid, down_mid, up_best_bid, up_best_ask, down_best_bid, down_best_ask
                       FROM poly_odds
                       WHERE window_start = ?
                       ORDER BY ABS(ts - ?) ASC
                       LIMIT 1""",
                    (ws, closed_at),
                )

            direction = str(r.get("direction") or "NO_TRADE")
            entry_price = _to_float(r.get("entry_price"))
            shares = _to_float(r.get("shares"))
            stake = _to_float(r.get("stake"))
            won = _to_int(r.get("won"))
            close_reason = str(r.get("close_reason") or "") if r.get("close_reason") else None
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
                    "entry_source": str(r.get("entry_source") or "") if r.get("entry_source") else None,
                    "close_reason": close_reason,
                    "status": str(r.get("status") or "OPEN"),
                    "opened_at": opened_at,
                    "opened_at_utc": (
                        datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                        if opened_at is not None
                        else None
                    ),
                    "closed_at": closed_at,
                    "actual_outcome": str(r.get("actual_outcome")) if r.get("actual_outcome") else None,
                    "won": won,
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
                    "odds_at_close": {
                        "ts": _to_float(odds_close_row.get("ts")) if odds_close_row else None,
                        "up_mid": _to_float(odds_close_row.get("up_mid")) if odds_close_row else None,
                        "down_mid": _to_float(odds_close_row.get("down_mid")) if odds_close_row else None,
                        "up_bid": _to_float(odds_close_row.get("up_best_bid")) if odds_close_row else None,
                        "up_ask": _to_float(odds_close_row.get("up_best_ask")) if odds_close_row else None,
                        "down_bid": _to_float(odds_close_row.get("down_best_bid")) if odds_close_row else None,
                        "down_ask": _to_float(odds_close_row.get("down_best_ask")) if odds_close_row else None,
                    },
                    "exit": _build_exit_snapshot(
                        direction=direction,
                        status=str(r.get("status") or "OPEN"),
                        won=won,
                        close_reason=close_reason,
                        odds_close_row=odds_close_row,
                    ),
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
                "avg_roi_pct": avg_roi_pct,
                "avg_stake": avg_stake,
            },
        }
    finally:
        conn.close()


def build_snapshot() -> dict:
    now_ts = time.time()
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

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
                "up_token_id": str(window["up_token_id"]) if window and window.get("up_token_id") else None,
                "down_token_id": str(window["down_token_id"]) if window and window.get("down_token_id") else None,
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
    mode = str(sizing_mode or "fixed").strip().lower()
    if mode not in ("adaptive", "fixed", "all_in_fixed", "all_in_equity"):
        mode = "fixed"
    ok, msg = PAPER_SIM_PROC.start(
        _python_command(
            "paper_trade_sim.py",
            ["--stake", str(stake), "--interval", "0.1", "--sizing-mode", mode],
        ),
        meta={"stake": stake, "interval": interval, "sizing_mode": mode},
    )
    status = build_paper_control_status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_paper_stop() -> dict:
    ok, msg = PAPER_SIM_PROC.stop()
    status = build_paper_control_status()
    status["ok"] = ok
    status["message"] = msg
    return status


def control_paper_reset() -> dict:
    was_running = bool(PAPER_SIM_PROC.status().get("running"))
    stopped_ok = True
    stopped_msg = "already stopped"
    if was_running:
        stopped_ok, stopped_msg = PAPER_SIM_PROC.stop()

    archived = 0
    conn = None
    try:
        conn = _connect_db()
        count_row = fetch_one(conn, "SELECT COUNT(*) FROM paper_trades WHERE archived_at IS NULL")
        archived = int(count_row[0] or 0) if count_row else 0
        execute_write(
            conn,
            "UPDATE paper_trades SET archived_at = ? WHERE archived_at IS NULL",
            (time.time(),),
        )
        conn.commit()
    except Exception as e:
        msg = str(e)
        # If table does not exist yet, treat as already reset.
        if ("no such table" in msg.lower()) or ("doesn't exist" in msg.lower()):
            deleted = 0
        else:
            status = build_paper_control_status()
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

    status = build_paper_control_status()
    status["ok"] = True
    status["message"] = "paper history archived (data preserved for backtesting)"
    status["deleted"] = archived
    status["stopped"] = {"ok": stopped_ok, "message": stopped_msg}
    return status


def control_paper_telegram_configure(*, enabled: Optional[bool] = None) -> dict[str, Any]:
    status = build_paper_control_status()
    running = bool(status.get("running"))
    if running:
        status["ok"] = False
        status["message"] = "Stop paper simulator first to change Telegram notify option."
        status["telegram"] = _paper_telegram_snapshot()
        return status

    if enabled is None:
        desired = bool(getattr(config.trading, "paper_telegram_notify_open", False))
    else:
        desired = bool(enabled)

    _apply_runtime_paper_telegram_notify_open(enabled=desired)
    _update_env_file(
        _PUBLIC_ENV_PATH,
        {
            "PAPER_TELEGRAM_NOTIFY_OPEN": "true" if desired else "false",
        },
    )

    status = build_paper_control_status()
    status["ok"] = True
    status["message"] = (
        "Paper Telegram notify option saved. Uses token/chat configured in Live Telegram setup."
    )
    return status


def control_live_start(
    stake: float,
    position_mode: str = "BOTH",
    sizing_mode: str = "adaptive",
) -> dict:
    requested_stake = float(stake)
    mode = _normalize_live_position_mode(position_mode)
    sizing = _normalize_live_sizing_mode(sizing_mode)
    account = _fetch_live_account_snapshot()

    if not account.get("configured", False):
        status = LIVE_TRADING_PROC.status()
        status["ok"] = False
        status["message"] = account.get("error") or "Polymarket auth is not configured"
        status["account"] = account
        return status

    if not account.get("ok", False):
        status = LIVE_TRADING_PROC.status()
        status["ok"] = False
        status["message"] = account.get("error") or "Failed to fetch collateral balance"
        status["account"] = account
        return status

    balance = _to_float(account.get("collateral_balance"))
    if sizing == "fixed":
        per_trade_usd = max(0.01, requested_stake)
        if balance is not None and per_trade_usd > balance:
            status = LIVE_TRADING_PROC.status()
            status["ok"] = False
            status["message"] = (
                f"Invest amount (${per_trade_usd:.2f}) exceeds collateral balance (${balance:.2f})"
            )
            status["account"] = account
            return status
    else:
        # Adaptive mode uses MAX_BET_SIZE as account-size cap so dynamic sizing
        # in main.py scales proportionally (e.g., 100 -> ~7~12, 1000 -> ~70~120).
        if balance is not None:
            if balance <= 0.0:
                status = LIVE_TRADING_PROC.status()
                status["ok"] = False
                status["message"] = "Collateral balance is zero; cannot start adaptive live mode"
                status["account"] = account
                return status
            per_trade_usd = float(balance)
        else:
            per_trade_usd = max(5.0, requested_stake if requested_stake > 0 else 50.0)

    # Persist sizing mode to env file so it survives restarts
    _set_runtime_var("LIVE_SIZING_MODE", sizing.upper())
    _update_env_file(_PUBLIC_ENV_PATH, {"LIVE_SIZING_MODE": sizing.upper()})
    env_overrides = {
        "DRY_RUN": "false",
        "MAX_BET_SIZE": f"{per_trade_usd}",
        "LIVE_FIXED_STAKE": f"{per_trade_usd}",
        "POSITION_MODE": mode,
        "LIVE_SIZING_MODE": sizing.upper(),
    }
    ok, msg = LIVE_TRADING_PROC.start(
        _python_command("main.py", []),
        meta={
            "sizing_mode": sizing,
            "stake_per_trade": per_trade_usd,
            "requested_stake": requested_stake,
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


def control_live_auth_configure(
    private_key: str,
    funder: str = "",
    signature_type: int = -1,
    builder_api_key: str = "",
    builder_api_secret: str = "",
    builder_api_passphrase: str = "",
) -> dict:
    key = str(private_key or "").strip()
    if not key:
        key = str(getattr(config.polymarket, "private_key", "") or "").strip()
        if key and not key.startswith("0x") and len(key) == 64:
            key = f"0x{key}"
    resolved_funder = str(funder or "").strip()
    if not resolved_funder:
        resolved_funder = str(getattr(config.polymarket, "funder", "") or "").strip()

    if not key:
        status = build_live_control_status()
        status["ok"] = False
        status["message"] = "private_key is required (or keep an existing configured key)"
        return status

    try:
        apply_meta = apply_runtime_auth(
            private_key=key,
            funder=resolved_funder,
            signature_type=int(signature_type),
            builder_api_key=str(builder_api_key or "").strip(),
            builder_api_secret=str(builder_api_secret or "").strip(),
            builder_api_passphrase=str(builder_api_passphrase or "").strip(),
            persist_env=True,
        )
        # Force derive/validate now so user can trade without restarting server.
        account = _fetch_live_account_snapshot()
        status = build_live_control_status()
        status["ok"] = bool(account.get("ok"))
        if status["ok"]:
            if bool(apply_meta.get("builder_api_creds")):
                status["message"] = (
                    "Auth saved (trading auth + Builder creds separated). "
                    "If live process is already running, restart it to apply new auth."
                )
            else:
                status["message"] = (
                    "Auth saved and API credentials derived. "
                    "If live process is already running, restart it to apply new auth."
                )
        else:
            status["message"] = str(account.get("error") or "") or "Auth saved, but credential verification failed"
        status["auth"] = apply_meta
        return status
    except Exception as e:
        status = build_live_control_status()
        status["ok"] = False
        status["message"] = f"auth configure failed: {e}"
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

    def handle(self) -> None:
        """
        Ignore routine client disconnect/reset errors to avoid noisy stack traces.
        Typical case on Windows: WinError 10054 (client closed the socket).
        """
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        except OSError as e:
            if int(getattr(e, "winerror", -1)) in {10053, 10054}:
                return
            raise

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

        if path == "/api/live-trade-history":
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
                self._send_json(build_live_trade_history(limit=limit, offset=offset), code=200)
            except Exception as e:
                logger.exception("live-trade-history error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/paper":
            self._send_json(build_paper_control_status(), code=200)
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

        # Multi-market GET status
        _mm_get_status = {
            "/api/control/btc15/signal": SIGNAL_BTC15_PROC,
            "/api/control/btc15/paper": PAPER_BTC15_PROC,
            "/api/control/btc15/live": LIVE_BTC15_PROC,
            "/api/control/eth5/signal": SIGNAL_ETH5_PROC,
            "/api/control/eth5/paper": PAPER_ETH5_PROC,
            "/api/control/eth5/live": LIVE_ETH5_PROC,
        }
        if path in _mm_get_status:
            self._send_json(_mm_get_status[path].status(), code=200)
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

        if path == "/api/control/paper/telegram-config":
            enabled_raw = payload.get("enabled")
            enabled: Optional[bool]
            if enabled_raw is None:
                enabled = None
            else:
                enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
            try:
                self._send_json(
                    control_paper_telegram_configure(
                        enabled=enabled,
                    ),
                    code=200,
                )
            except Exception as e:
                logger.exception("paper telegram-config error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/start":
            stake_raw = payload.get("stake", 5.0)
            position_mode = str(payload.get("position_mode", "BOTH"))
            sizing_mode = str(payload.get("sizing_mode", "adaptive"))
            try:
                stake = _to_float(stake_raw)
                if stake is None:
                    stake = 0.0
                self._send_json(
                    control_live_start(
                        float(stake),
                        position_mode=position_mode,
                        sizing_mode=sizing_mode,
                    ),
                    code=200,
                )
            except Exception as e:
                logger.exception("live start error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/auth-config":
            private_key = str(payload.get("private_key", ""))
            funder = str(payload.get("funder", ""))
            signature_type_raw = payload.get("signature_type", -1)
            builder_api_key = str(payload.get("builder_api_key", ""))
            builder_api_secret = str(payload.get("builder_api_secret", ""))
            builder_api_passphrase = str(payload.get("builder_api_passphrase", ""))
            try:
                signature_type = int(signature_type_raw)
            except Exception:
                signature_type = -1
            try:
                self._send_json(
                    control_live_auth_configure(
                        private_key=private_key,
                        funder=funder,
                        signature_type=signature_type,
                        builder_api_key=builder_api_key,
                        builder_api_secret=builder_api_secret,
                        builder_api_passphrase=builder_api_passphrase,
                    ),
                    code=200,
                )
            except Exception as e:
                logger.exception("live auth-config error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/telegram-config":
            bot_token = str(payload.get("bot_token", ""))
            chat_id = str(payload.get("chat_id", ""))
            enabled_raw = payload.get("enabled")
            send_test = bool(payload.get("send_test", False))
            test_message = str(payload.get("test_message", ""))
            enabled: Optional[bool]
            if enabled_raw is None:
                enabled = None
            else:
                enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
            try:
                self._send_json(
                    control_live_telegram_configure(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        enabled=enabled,
                        send_test=send_test,
                        test_message=test_message,
                    ),
                    code=200,
                )
            except Exception as e:
                logger.exception("live telegram-config error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/telegram-test":
            bot_token = str(payload.get("bot_token", ""))
            chat_id = str(payload.get("chat_id", ""))
            message = str(payload.get("message", ""))
            try:
                self._send_json(
                    control_live_telegram_test(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        message=message,
                    ),
                    code=200,
                )
            except Exception as e:
                logger.exception("live telegram-test error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/seed-capital":
            try:
                val = float(payload.get("seed_capital", 0))
                if val <= 0:
                    self._send_json({"ok": False, "error": "seed_capital must be > 0"}, code=400)
                    return
                val_str = f"{val:.2f}"
                _set_runtime_var("LIVE_EQUITY_SEED_CAPITAL", val_str)
                _update_env_file(_PUBLIC_ENV_PATH, {"LIVE_EQUITY_SEED_CAPITAL": val_str})
                self._send_json({"ok": True, "seed_capital": val})
            except Exception as e:
                logger.exception("seed-capital save error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/daily-loss-limit":
            try:
                val = float(payload.get("daily_loss_limit", 0))
                if val <= 0:
                    self._send_json({"ok": False, "error": "daily_loss_limit must be > 0"}, code=400)
                    return
                val_str = f"{val:.2f}"
                _set_runtime_var("LIVE_DAILY_LOSS_LIMIT", val_str)
                _update_env_file(_PUBLIC_ENV_PATH, {"LIVE_DAILY_LOSS_LIMIT": val_str})
                self._send_json({"ok": True, "daily_loss_limit": val})
            except Exception as e:
                logger.exception("daily-loss-limit save error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/control/live/stop":
            try:
                self._send_json(control_live_stop(), code=200)
            except Exception as e:
                logger.exception("live stop error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        # ---- Multi-account API ----
        if path == "/api/accounts":
            try:
                self._send_json({"ok": True, "accounts": _get_accounts()})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/accounts/save":
            try:
                self._send_json(_save_account(payload))
            except Exception as e:
                logger.exception("account save error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/accounts/start":
            try:
                aid = int(payload.get("account_id", 0))
                self._send_json(control_account_start(aid))
            except Exception as e:
                logger.exception("account start error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/accounts/stop":
            try:
                aid = int(payload.get("account_id", 0))
                self._send_json(control_account_stop(aid))
            except Exception as e:
                logger.exception("account stop error")
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/accounts/status":
            try:
                aid = int(payload.get("account_id", 0))
                self._send_json(build_account_status(aid))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        if path == "/api/accounts/delete":
            try:
                aid = int(payload.get("account_id", 0))
                if aid in LIVE_ACCOUNT_PROCS and LIVE_ACCOUNT_PROCS[aid].running():
                    LIVE_ACCOUNT_PROCS[aid].stop()
                conn = _connect_db()
                execute_write(conn, "DELETE FROM accounts WHERE id=%s", (aid,))
                conn.commit()
                self._send_json({"ok": True})
            except Exception as e:
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

        # -- Multi-market control endpoints --
        _mm_routes = {
            "/api/control/btc15/signal/start": ("signal_generator_btc15.py", SIGNAL_BTC15_PROC),
            "/api/control/btc15/signal/stop": (None, SIGNAL_BTC15_PROC),
            "/api/control/btc15/paper/start": ("paper_sim_btc15.py", PAPER_BTC15_PROC),
            "/api/control/btc15/paper/stop": (None, PAPER_BTC15_PROC),
            "/api/control/btc15/live/start": ("live_btc15.py", LIVE_BTC15_PROC),
            "/api/control/btc15/live/stop": (None, LIVE_BTC15_PROC),
            "/api/control/eth5/signal/start": ("signal_generator_eth5.py", SIGNAL_ETH5_PROC),
            "/api/control/eth5/signal/stop": (None, SIGNAL_ETH5_PROC),
            "/api/control/eth5/paper/start": ("paper_sim_eth5.py", PAPER_ETH5_PROC),
            "/api/control/eth5/paper/stop": (None, PAPER_ETH5_PROC),
            "/api/control/eth5/live/start": ("live_eth5.py", LIVE_ETH5_PROC),
            "/api/control/eth5/live/stop": (None, LIVE_ETH5_PROC),
        }
        # Auto-start signal generator when paper/live starts
        _mm_signal_map = {
            "/api/control/btc15/paper/start": ("signal_generator_btc15.py", SIGNAL_BTC15_PROC),
            "/api/control/btc15/live/start": ("signal_generator_btc15.py", SIGNAL_BTC15_PROC),
            "/api/control/eth5/paper/start": ("signal_generator_eth5.py", SIGNAL_ETH5_PROC),
            "/api/control/eth5/live/start": ("signal_generator_eth5.py", SIGNAL_ETH5_PROC),
        }

        if path in _mm_routes:
            script, proc = _mm_routes[path]
            try:
                if script is not None:
                    # Auto-start signal generator if not running
                    if path in _mm_signal_map:
                        sig_script, sig_proc = _mm_signal_map[path]
                        if not sig_proc.running():
                            sig_cmd = _python_command(sig_script, [])
                            sig_proc.start(sig_cmd, meta={"auto_started": True})
                            logger.info("Auto-started signal generator: %s", sig_script)

                    # Start
                    stake = payload.get("stake", 100.0)
                    sizing_mode = str(payload.get("sizing_mode", "fixed"))
                    args = ["--stake", str(stake), "--sizing-mode", sizing_mode]
                    if "live" in path and not payload.get("dry_run", True):
                        args.append("--no-dry-run")
                    cmd = _python_command(script, args)
                    proc.start(cmd, meta={"stake": stake, "sizing_mode": sizing_mode})
                    self._send_json({"ok": True, "status": "started", "pid": proc._proc.pid if proc._proc else None})
                else:
                    # Stop
                    proc.stop()
                    self._send_json({"ok": True, "status": "stopped"})
            except Exception as e:
                logger.exception("multi-market control error: %s", path)
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        # Multi-market status endpoints
        _mm_status = {
            "/api/control/btc15/signal": SIGNAL_BTC15_PROC,
            "/api/control/btc15/paper": PAPER_BTC15_PROC,
            "/api/control/btc15/live": LIVE_BTC15_PROC,
            "/api/control/eth5/signal": SIGNAL_ETH5_PROC,
            "/api/control/eth5/paper": PAPER_ETH5_PROC,
            "/api/control/eth5/live": LIVE_ETH5_PROC,
        }
        if path in _mm_status:
            proc = _mm_status[path]
            self._send_json({"ok": True, **proc.status()})
            return

        # Multi-market paper history
        _mm_paper_tables = {
            "/api/btc15/paper-history": "paper_trades_btc15",
            "/api/eth5/paper-history": "paper_trades_eth5",
        }
        if path in _mm_paper_tables:
            try:
                table = _mm_paper_tables[path]
                conn = connect_db()
                rows = fetch_all_dicts(conn, f"SELECT * FROM {table} WHERE archived_at IS NULL ORDER BY opened_at DESC LIMIT 50")
                conn.close()
                self._send_json({"ok": True, "trades": rows})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, code=500)
            return

        # Multi-market live history
        _mm_live_tables = {
            "/api/btc15/live-trade-history": "live_trades_btc15",
            "/api/eth5/live-trade-history": "live_trades_eth5",
        }
        if path in _mm_live_tables:
            try:
                table = _mm_live_tables[path]
                conn = connect_db()
                rows = fetch_all_dicts(conn, f"SELECT * FROM {table} ORDER BY opened_at DESC LIMIT 50")
                conn.close()
                self._send_json({"ok": True, "trades": rows})
            except Exception as e:
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
    except Exception:
        logger.exception("Dashboard server crashed")
    finally:
        server.server_close()


# ==================== Multi-Account Management ====================

def _get_accounts() -> list[dict]:
    """Get all accounts from DB."""
    try:
        conn = _connect_db()
        rows = fetch_all_dicts(conn, "SELECT * FROM accounts ORDER BY id")
        result = []
        for r in rows:
            d = dict(r)
            # Convert datetime to string for JSON serialization
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
            result.append(d)
        return result
    except Exception:
        return []


def _get_account(account_id: int) -> dict | None:
    try:
        conn = _connect_db()
        return fetch_one_dict(conn, "SELECT * FROM accounts WHERE id = %s", (account_id,))
    except Exception:
        return None


def _save_account(data: dict) -> dict:
    """Create or update account."""
    conn = _connect_db()
    acct_id = data.get("id")
    if acct_id:
        # Update
        execute_write(conn, """
            UPDATE accounts SET name=%s, private_key=%s, api_key=%s, api_secret=%s, api_passphrase=%s,
            funder=%s, telegram_token=%s, telegram_chat_id=%s, seed_capital=%s, fixed_stake=%s,
            sizing_mode=%s, position_mode=%s, daily_loss_limit=%s, mega_multiplier=%s,
            mega_min_score=%s, min_entry_score=%s, enabled=%s
            WHERE id=%s
        """, (
            data.get("name",""), data.get("private_key",""), data.get("api_key",""),
            data.get("api_secret",""), data.get("api_passphrase",""), data.get("funder",""),
            data.get("telegram_token",""), data.get("telegram_chat_id",""),
            float(data.get("seed_capital", 100)), float(data.get("fixed_stake", 15)),
            data.get("sizing_mode", "FIXED"), data.get("position_mode", "BOTH"),
            float(data.get("daily_loss_limit", 100)), float(data.get("mega_multiplier", 3)),
            int(data.get("mega_min_score", 6)), int(data.get("min_entry_score", 3)),
            int(data.get("enabled", 1)), acct_id,
        ))
        conn.commit()
        return {"ok": True, "id": acct_id}
    else:
        # Create
        execute_write(conn, """
            INSERT INTO accounts (name, private_key, api_key, api_secret, api_passphrase,
            funder, telegram_token, telegram_chat_id, seed_capital, fixed_stake,
            sizing_mode, position_mode, daily_loss_limit, mega_multiplier, mega_min_score, min_entry_score)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data.get("name",""), data.get("private_key",""), data.get("api_key",""),
            data.get("api_secret",""), data.get("api_passphrase",""), data.get("funder",""),
            data.get("telegram_token",""), data.get("telegram_chat_id",""),
            float(data.get("seed_capital", 100)), float(data.get("fixed_stake", 15)),
            data.get("sizing_mode", "FIXED"), data.get("position_mode", "BOTH"),
            float(data.get("daily_loss_limit", 100)), float(data.get("mega_multiplier", 3)),
            int(data.get("mega_min_score", 6)), int(data.get("min_entry_score", 3)),
        ))
        conn.commit()
        new_id = fetch_one(conn, "SELECT LAST_INSERT_ID()")[0]
        return {"ok": True, "id": int(new_id)}


def control_account_start(account_id: int) -> dict:
    """Start live trading for a specific account."""
    acct = _get_account(account_id)
    if not acct:
        return {"ok": False, "error": f"Account {account_id} not found"}
    if not acct.get("api_key") or not acct.get("private_key"):
        return {"ok": False, "error": "API key or private key not configured"}

    # Stop if already running
    if account_id in LIVE_ACCOUNT_PROCS and LIVE_ACCOUNT_PROCS[account_id].running():
        LIVE_ACCOUNT_PROCS[account_id].stop()

    proc = ManagedProcess(f"live_account_{account_id}")
    env_overrides = {
        "DRY_RUN": "false",
        "ACCOUNT_ID": str(account_id),
        "POLYMARKET_PRIVATE_KEY": str(acct.get("private_key", "")),
        "POLYMARKET_API_KEY": str(acct.get("api_key", "")),
        "POLYMARKET_API_SECRET": str(acct.get("api_secret", "")),
        "POLYMARKET_API_PASSPHRASE": str(acct.get("api_passphrase", "")),
        "POLYMARKET_FUNDER": str(acct.get("funder", "")),
        "LIVE_TELEGRAM_BOT_TOKEN": str(acct.get("telegram_token", "")),
        "LIVE_TELEGRAM_CHAT_ID": str(acct.get("telegram_chat_id", "")),
        "LIVE_EQUITY_SEED_CAPITAL": str(acct.get("seed_capital", 100)),
        "LIVE_FIXED_STAKE": str(acct.get("fixed_stake", 15)),
        "LIVE_SIZING_MODE": str(acct.get("sizing_mode", "FIXED")),
        "POSITION_MODE": str(acct.get("position_mode", "BOTH")),
        "LIVE_DAILY_LOSS_LIMIT": str(acct.get("daily_loss_limit", 100)),
        "LIVE_MEGA_MULTIPLIER": str(acct.get("mega_multiplier", 3)),
        "LIVE_MEGA_MIN_SCORE": str(acct.get("mega_min_score", 6)),
        "LIVE_MIN_ENTRY_SCORE": str(acct.get("min_entry_score", 3)),
        "MAX_BET_SIZE": str(acct.get("fixed_stake", 15)),
    }

    ok, msg = proc.start(
        _python_command("main.py", []),
        meta={"account_id": account_id, "account_name": acct.get("name","")},
        env_overrides=env_overrides,
    )
    LIVE_ACCOUNT_PROCS[account_id] = proc

    # Update DB status
    try:
        conn = _connect_db()
        execute_write(conn, "UPDATE accounts SET status='RUNNING', pid=%s WHERE id=%s",
                     (proc._process.pid if proc._process else None, account_id))
        conn.commit()
    except Exception:
        pass

    return {"ok": ok, "message": msg, "account_id": account_id}


def control_account_stop(account_id: int) -> dict:
    """Stop live trading for a specific account."""
    proc = LIVE_ACCOUNT_PROCS.get(account_id)
    if proc:
        ok, msg = proc.stop()
    else:
        ok, msg = False, "No process found"

    try:
        conn = _connect_db()
        execute_write(conn, "UPDATE accounts SET status='STOPPED', pid=NULL WHERE id=%s", (account_id,))
        conn.commit()
    except Exception:
        pass

    return {"ok": ok, "message": msg, "account_id": account_id}


def build_account_status(account_id: int) -> dict:
    """Get status for a specific account."""
    acct = _get_account(account_id) or {}
    proc = LIVE_ACCOUNT_PROCS.get(account_id)
    running = proc.running() if proc else False

    # Get account PnL from live_trades
    pnl = 0.0
    trade_count = 0
    try:
        conn = _connect_db()
        row = fetch_one_dict(conn, """
            SELECT COALESCE(SUM(pnl),0) as total_pnl, COUNT(*) as cnt
            FROM live_trades WHERE account_id = %s AND DATE(FROM_UNIXTIME(opened_at)) = CURDATE()
        """, (account_id,))
        if row:
            pnl = float(row.get("total_pnl", 0))
            trade_count = int(row.get("cnt", 0))
    except Exception:
        pass

    return {
        "account": acct,
        "running": running,
        "pid": proc._process.pid if proc and proc._process else None,
        "today_pnl": pnl,
        "today_trades": trade_count,
        "log_lines": list(proc._output_lines) if proc else [],
    }


if __name__ == "__main__":
    main()
