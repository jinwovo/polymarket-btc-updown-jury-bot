"""
Minimal Telegram Bot API helpers for live trade notifications.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


def _clean(value: Any) -> str:
    return str(value or "").strip()


def mask_bot_token(token: str) -> str:
    s = _clean(token)
    if not s:
        return "--"
    if len(s) <= 12:
        return f"{s[:3]}***{s[-2:]}"
    return f"{s[:8]}...{s[-4:]}"


def _api_post(token: str, method: str, params: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    tok = _clean(token)
    if not tok:
        return {"ok": False, "error": "bot token missing"}
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(
        url=f"https://api.telegram.org/bot{tok}/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(data)
        if isinstance(payload, dict):
            return payload
        return {"ok": False, "error": "invalid telegram response shape"}
    except urllib.error.HTTPError as e:
        try:
            body_raw = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(body_raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"ok": False, "error": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resolve_chat_id(token: str, timeout: float = 10.0) -> tuple[Optional[str], Optional[str]]:
    payload = _api_post(token, "getUpdates", {"limit": 20, "timeout": 0}, timeout=timeout)
    if not bool(payload.get("ok")):
        return None, str(payload.get("description") or payload.get("error") or "getUpdates failed")

    rows = payload.get("result")
    if not isinstance(rows, list) or len(rows) == 0:
        return None, "no updates found (send /start to the bot first)"

    def _extract_chat_id(item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        paths = [
            ("message", "chat", "id"),
            ("edited_message", "chat", "id"),
            ("channel_post", "chat", "id"),
            ("callback_query", "message", "chat", "id"),
            ("my_chat_member", "chat", "id"),
            ("chat_member", "chat", "id"),
        ]
        for p in paths:
            cur: Any = item
            ok = True
            for k in p:
                if not isinstance(cur, dict) or k not in cur:
                    ok = False
                    break
                cur = cur[k]
            if ok and cur is not None:
                return str(cur).strip()
        return None

    for item in reversed(rows):
        cid = _extract_chat_id(item)
        if cid:
            return cid, None
    return None, "chat_id not found in updates"


def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "",
    timeout: float = 10.0,
    auto_resolve_chat: bool = True,
) -> dict[str, Any]:
    tok = _clean(token)
    cid = _clean(chat_id)
    msg = str(text or "").strip()
    if not tok:
        return {"ok": False, "error": "bot token missing"}
    if not msg:
        return {"ok": False, "error": "message text missing"}

    resolved_chat_id = cid
    if not resolved_chat_id and auto_resolve_chat:
        resolved_chat_id, err = resolve_chat_id(tok, timeout=timeout)
        if not resolved_chat_id:
            return {"ok": False, "error": err or "chat id resolve failed"}
    if not resolved_chat_id:
        return {"ok": False, "error": "chat id missing"}

    params: dict[str, Any] = {
        "chat_id": resolved_chat_id,
        "text": msg,
        "disable_web_page_preview": "true",
    }
    pm = _clean(parse_mode).upper()
    if pm in {"MARKDOWN", "MARKDOWNV2", "HTML"}:
        params["parse_mode"] = pm

    payload = _api_post(tok, "sendMessage", params, timeout=timeout)
    if bool(payload.get("ok")):
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        return {
            "ok": True,
            "chat_id": resolved_chat_id,
            "message_id": result.get("message_id"),
            "raw": payload,
        }

    return {
        "ok": False,
        "chat_id": resolved_chat_id,
        "error": str(payload.get("description") or payload.get("error") or "sendMessage failed"),
        "raw": payload,
    }

