"""
Helpers for Polymarket CLOB authenticated client creation.

Supports:
- Explicit API creds in env (API key/secret/passphrase)
- Auto-deriving API creds from wallet private key at runtime
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from config import config
from env_paths import (
    GENERATED_POLYMARKET_ENV_PATH,
    SECRETS_ENV_PATH,
)

logger = logging.getLogger(__name__)

_DERIVED_CREDS_CACHE: dict[str, Any] = {}
_ENV_PATH = Path(SECRETS_ENV_PATH)
_GENERATED_ENV_PATH = Path(GENERATED_POLYMARKET_ENV_PATH)
_MANAGED_ENV_KEYS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_FUNDER",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_API_SECRET",
    "POLY_BUILDER_API_PASSPHRASE",
)
_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _clean(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    placeholder_values = {
        "your_wallet_private_key_here",
        "your_wallet_or_proxy_address_here",
        "your_wallet_address_here",
        "your_api_key_here",
        "your_api_secret_here",
        "your_passphrase_here",
        "your_builder_api_key_here",
        "your_builder_api_secret_here",
        "your_builder_passphrase_here",
    }
    if low in placeholder_values:
        return ""
    return s


def _normalize_private_key(raw: Any) -> str:
    pk = _clean(raw)
    if not pk:
        return ""
    if pk.startswith("0x"):
        return pk
    if len(pk) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in pk):
        return f"0x{pk}"
    return pk


def _normalize_signature_type(raw: Any) -> int:
    try:
        val = int(raw)
    except Exception:
        return -1
    return val if val in (-1, 0, 1, 2) else -1


def _has_direct_api_creds() -> bool:
    return bool(
        _clean(config.polymarket.api_key)
        and _clean(config.polymarket.api_secret)
        and _clean(config.polymarket.api_passphrase)
    )


def invalidate_cached_auth():
    _DERIVED_CREDS_CACHE.clear()


def _set_runtime_var(key: str, value: str | None):
    if value is None or value == "":
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(value)


def _update_runtime_config(
    private_key: str,
    funder: str,
    signature_type: int,
    builder_api_key: str,
    builder_api_secret: str,
    builder_api_passphrase: str,
):
    config.polymarket.private_key = private_key
    config.polymarket.funder = funder
    config.polymarket.signature_type = int(signature_type)
    # Trading auth should keep using runtime-derived creds from private key.
    config.polymarket.api_key = ""
    config.polymarket.api_secret = ""
    config.polymarket.api_passphrase = ""
    config.polymarket.builder_api_key = builder_api_key
    config.polymarket.builder_api_secret = builder_api_secret
    config.polymarket.builder_api_passphrase = builder_api_passphrase


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


def _read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return data
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, val = s.split("=", 1)
        k = str(key).strip()
        if not k:
            continue
        data[k] = str(val).strip()
    return data


def clear_generated_credentials_file():
    try:
        if _GENERATED_ENV_PATH.exists():
            _GENERATED_ENV_PATH.unlink()
    except Exception as e:
        logger.warning("Could not remove %s: %s", _GENERATED_ENV_PATH, e)


def api_credentials_snapshot() -> dict[str, Any]:
    # Prefer in-memory config first.
    api_key = _clean(config.polymarket.api_key)
    api_secret = _clean(config.polymarket.api_secret)
    api_passphrase = _clean(config.polymarket.api_passphrase)
    if api_key and api_secret and api_passphrase:
        return {
            "exists": True,
            "source": "runtime_env",
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
            "path": None,
        }

    generated = _read_env_file(_GENERATED_ENV_PATH)
    api_key = _clean(generated.get("POLYMARKET_API_KEY"))
    api_secret = _clean(generated.get("POLYMARKET_API_SECRET"))
    api_passphrase = _clean(generated.get("POLYMARKET_API_PASSPHRASE"))
    if api_key and api_secret and api_passphrase:
        return {
            "exists": True,
            "source": "generated_file",
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
            "path": str(_GENERATED_ENV_PATH),
        }

    return {
        "exists": False,
        "source": "none",
        "api_key": None,
        "api_secret": None,
        "api_passphrase": None,
        "path": str(_GENERATED_ENV_PATH),
    }


def builder_credentials_snapshot() -> dict[str, Any]:
    api_key = _clean(config.polymarket.builder_api_key)
    api_secret = _clean(config.polymarket.builder_api_secret)
    api_passphrase = _clean(config.polymarket.builder_api_passphrase)
    if api_key and api_secret and api_passphrase:
        return {
            "exists": True,
            "source": "runtime_env",
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
        }

    for path, source in ((_ENV_PATH, "secrets_env_file"),):
        env_data = _read_env_file(path)
        api_key = _clean(env_data.get("POLY_BUILDER_API_KEY"))
        api_secret = _clean(env_data.get("POLY_BUILDER_API_SECRET"))
        api_passphrase = _clean(env_data.get("POLY_BUILDER_API_PASSPHRASE"))
        if api_key and api_secret and api_passphrase:
            return {
                "exists": True,
                "source": source,
                "api_key": api_key,
                "api_secret": api_secret,
                "api_passphrase": api_passphrase,
            }

    return {
        "exists": False,
        "source": "none",
        "api_key": None,
        "api_secret": None,
        "api_passphrase": None,
    }


def apply_runtime_auth(
    private_key: str,
    funder: str = "",
    signature_type: int = -1,
    builder_api_key: str = "",
    builder_api_secret: str = "",
    builder_api_passphrase: str = "",
    persist_env: bool = True,
) -> dict[str, Any]:
    """
    Apply auth inputs immediately in current process and optionally persist to .env.secrets.
    """
    pk = _normalize_private_key(private_key)
    fd = _clean(funder)
    sig = _normalize_signature_type(signature_type)
    existing_builder = builder_credentials_snapshot()
    raw_builder_api_key = _clean(builder_api_key)
    raw_builder_api_secret = _clean(builder_api_secret)
    raw_builder_api_passphrase = _clean(builder_api_passphrase)
    has_builder_update = bool(raw_builder_api_key or raw_builder_api_secret or raw_builder_api_passphrase)

    resolved_builder_api_key = raw_builder_api_key or _clean(existing_builder.get("api_key"))
    resolved_builder_api_secret = raw_builder_api_secret or _clean(existing_builder.get("api_secret"))
    resolved_builder_api_passphrase = raw_builder_api_passphrase or _clean(existing_builder.get("api_passphrase"))
    has_full_builder = bool(
        resolved_builder_api_key and resolved_builder_api_secret and resolved_builder_api_passphrase
    )

    if not pk:
        raise ValueError("POLYMARKET_PRIVATE_KEY is required")
    if has_builder_update and not has_full_builder:
        raise ValueError(
            "Builder credentials update requires full api key/secret/passphrase (new or existing merge)"
        )

    _set_runtime_var("POLYMARKET_PRIVATE_KEY", pk)
    _set_runtime_var("POLYMARKET_FUNDER", fd if fd else None)
    _set_runtime_var("POLYMARKET_SIGNATURE_TYPE", str(sig))
    # Trading auth: always force private-key derive flow (no manual CLOB key override from this modal).
    _set_runtime_var("POLYMARKET_API_KEY", None)
    _set_runtime_var("POLYMARKET_API_SECRET", None)
    _set_runtime_var("POLYMARKET_API_PASSPHRASE", None)
    if has_full_builder:
        _set_runtime_var("POLY_BUILDER_API_KEY", resolved_builder_api_key)
        _set_runtime_var("POLY_BUILDER_API_SECRET", resolved_builder_api_secret)
        _set_runtime_var("POLY_BUILDER_API_PASSPHRASE", resolved_builder_api_passphrase)
    else:
        _set_runtime_var("POLY_BUILDER_API_KEY", None)
        _set_runtime_var("POLY_BUILDER_API_SECRET", None)
        _set_runtime_var("POLY_BUILDER_API_PASSPHRASE", None)

    _update_runtime_config(
        private_key=pk,
        funder=fd,
        signature_type=sig,
        builder_api_key=resolved_builder_api_key if has_full_builder else "",
        builder_api_secret=resolved_builder_api_secret if has_full_builder else "",
        builder_api_passphrase=resolved_builder_api_passphrase if has_full_builder else "",
    )
    invalidate_cached_auth()
    clear_generated_credentials_file()

    env_path = _ENV_PATH
    if persist_env:
        updates = {
            "POLYMARKET_PRIVATE_KEY": pk,
            "POLYMARKET_FUNDER": fd if fd else None,
            "POLYMARKET_SIGNATURE_TYPE": str(sig),
            # Trading auth should remain derive-based from private key.
            "POLYMARKET_API_KEY": None,
            "POLYMARKET_API_SECRET": None,
            "POLYMARKET_API_PASSPHRASE": None,
            "POLY_BUILDER_API_KEY": resolved_builder_api_key if has_full_builder else None,
            "POLY_BUILDER_API_SECRET": resolved_builder_api_secret if has_full_builder else None,
            "POLY_BUILDER_API_PASSPHRASE": resolved_builder_api_passphrase if has_full_builder else None,
        }
        _update_env_file(env_path, updates)

    return {
        "ok": True,
        "persisted_env": str(env_path),
        "generated_env": str(_GENERATED_ENV_PATH),
        "builder_api_creds": bool(has_full_builder),
    }


def _cred_cache_key(private_key: str, funder: str) -> str:
    raw = f"{private_key}|{funder}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _signer_address(private_key: str) -> str:
    if not private_key:
        return ""
    try:
        from py_clob_client.signer import Signer

        addr = Signer(private_key, 137).address()
        return _clean(addr)
    except Exception:
        pass
    try:
        from eth_account import Account

        return _clean(Account.from_key(private_key).address)
    except Exception:
        return ""


def _resolve_funder(private_key: str) -> tuple[str, str]:
    configured = _clean(config.polymarket.funder)
    if configured:
        return configured, "env"
    signer = _signer_address(private_key)
    if signer:
        return signer, "derived_from_private_key"
    return "", "missing"


def _resolve_signature_type(private_key: str, funder: str) -> tuple[int, str]:
    configured = _normalize_signature_type(config.polymarket.signature_type)
    if configured in (0, 1, 2):
        return configured, "env"

    signer = _signer_address(private_key)
    if signer and funder and signer.lower() != funder.lower():
        # Most common for Polymarket email/google accounts.
        return 1, "auto_proxy"
    return 0, "auto_eoa"


def _write_generated_env(creds: Any, funder: str):
    api_key = _clean(getattr(creds, "api_key", ""))
    api_secret = _clean(getattr(creds, "api_secret", ""))
    api_passphrase = _clean(getattr(creds, "api_passphrase", ""))
    if not (api_key and api_secret and api_passphrase and funder):
        return

    body = (
        "# Auto-generated from POLYMARKET_PRIVATE_KEY at runtime.\n"
        "# Keep this local and never commit.\n"
        f"POLYMARKET_API_KEY={api_key}\n"
        f"POLYMARKET_API_SECRET={api_secret}\n"
        f"POLYMARKET_API_PASSPHRASE={api_passphrase}\n"
        f"POLYMARKET_FUNDER={funder}\n"
    )
    try:
        if _GENERATED_ENV_PATH.exists():
            existing = _GENERATED_ENV_PATH.read_text(encoding="utf-8")
            if existing == body:
                return
        _GENERATED_ENV_PATH.write_text(body, encoding="utf-8")
    except Exception as e:
        logger.warning("Could not write %s: %s", _GENERATED_ENV_PATH, e)


def auth_config_status() -> dict[str, Any]:
    private_key = _normalize_private_key(config.polymarket.private_key)
    funder, funder_source = _resolve_funder(private_key)
    signature_type, sig_source = _resolve_signature_type(private_key, funder)
    direct_creds = _has_direct_api_creds()

    missing: list[str] = []
    warnings: list[str] = []
    configured = True
    if not private_key:
        configured = False
        missing.append("POLYMARKET_PRIVATE_KEY")

    if signature_type in (1, 2) and not funder:
        configured = False
        missing.append("POLYMARKET_FUNDER")
    elif not funder:
        warnings.append("POLYMARKET_FUNDER missing; defaulting to signer address")

    return {
        "configured": configured,
        "missing": missing,
        "warnings": warnings,
        "funder": funder or None,
        "funder_source": funder_source,
        "signature_type": int(signature_type),
        "signature_type_source": sig_source,
        "direct_api_creds": bool(direct_creds),
        "private_key_set": bool(private_key),
    }


def _build_api_creds(private_key: str, funder: str):
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    if _has_direct_api_creds():
        creds = ApiCreds(
            api_key=_clean(config.polymarket.api_key),
            api_secret=_clean(config.polymarket.api_secret),
            api_passphrase=_clean(config.polymarket.api_passphrase),
        )
        return creds, "env_api_creds"

    cache_key = _cred_cache_key(private_key, funder)
    cached = _DERIVED_CREDS_CACHE.get(cache_key)
    if cached is not None:
        _write_generated_env(cached, funder)
        return cached, "derived_from_private_key(cache)"

    level1 = ClobClient(
        config.polymarket.clob_url,
        chain_id=137,
        key=private_key,
        signature_type=_resolve_signature_type(private_key, funder)[0],
        funder=(funder or None),
    )
    creds = level1.create_or_derive_api_creds()
    if creds is None:
        raise RuntimeError("Failed to derive Polymarket API credentials from private key")

    _DERIVED_CREDS_CACHE.clear()
    _DERIVED_CREDS_CACHE[cache_key] = creds
    _write_generated_env(creds, funder)
    return creds, "derived_from_private_key"


def create_authenticated_clob_client():
    """
    Build a level-2 authenticated CLOB client.

    Returns:
        tuple[ClobClient, dict[str, Any]]
    Raises:
        ValueError: when required env values are missing.
        RuntimeError / Exception: when derivation or client init fails.
    """
    from py_clob_client.client import ClobClient

    status = auth_config_status()
    if not bool(status["configured"]):
        missing = ", ".join(status["missing"]) or "unknown"
        raise ValueError(f"Missing required env: {missing}")

    private_key = _normalize_private_key(config.polymarket.private_key)
    funder, funder_source = _resolve_funder(private_key)
    signature_type, sig_source = _resolve_signature_type(private_key, funder)
    creds, source = _build_api_creds(private_key=private_key, funder=funder)

    client = ClobClient(
        config.polymarket.clob_url,
        chain_id=137,
        key=private_key,
        creds=creds,
        signature_type=signature_type,
        funder=(funder or None),
    )
    return client, {
        "funder": funder or None,
        "funder_source": funder_source,
        "signature_type": int(signature_type),
        "signature_type_source": sig_source,
        "creds_source": source,
        "private_key_set": bool(private_key),
        "warnings": list(status.get("warnings") or []),
    }
