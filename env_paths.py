"""
Shared environment file paths.

Policy:
- `env/runtime.public.env` is tracked in git (non-secret runtime knobs).
- `.env.secrets` is local-only (sensitive keys/passwords/tokens).
- `.env.polymarket.generated` is local-only auto-derived CLOB creds.
"""
from pathlib import Path


BASE_DIR = Path(__file__).parent

# Tracked non-secret runtime knobs.
PUBLIC_RUNTIME_ENV_PATH = BASE_DIR / "env" / "runtime.public.env"

# Local secret files (must never be committed).
SECRETS_ENV_PATH = BASE_DIR / ".env.secrets"
GENERATED_POLYMARKET_ENV_PATH = BASE_DIR / ".env.polymarket.generated"
