"""
Backfill btc_start_price with official Polymarket PTB for all historical windows.

Gamma API provides eventMetadata.priceToBeat for resolved (closed) windows.
This script:
  1. Reads all windows from DB
  2. Fetches official PTB from Gamma API for each
  3. Updates btc_start_price and re-derives actual_outcome
  4. Reports delta stats (how far off our old prices were)

Usage:
    python backfill_ptb.py          # dry-run (show changes without writing)
    python backfill_ptb.py --apply  # apply changes to DB
"""

import sys
import os
import time
import json
import urllib.request
import argparse

sys.stdout.reconfigure(encoding="utf-8")

# Load env files (same as config.py / dashboard_server.py)
from env_paths import PUBLIC_RUNTIME_ENV_PATH, SECRETS_ENV_PATH
from dotenv import load_dotenv
load_dotenv(SECRETS_ENV_PATH, override=True)
load_dotenv(PUBLIC_RUNTIME_ENV_PATH, override=False)

# Debug: confirm DB env loaded
_p = os.getenv("MARIADB_PORT", "?")
_pw = "set" if os.getenv("MARIADB_PASSWORD") else "EMPTY"
print(f"DB: 127.0.0.1:{_p} pw={_pw}")

from db_config import connect_db, fetch_all_dicts, execute_write


GAMMA_URL = "https://gamma-api.polymarket.com/events"
SLUG_PREFIX = "btc-updown-5m"
BATCH_SIZE = 10  # fetch N slugs then sleep to avoid rate limit
SLEEP_BETWEEN = 1.0  # seconds between batches


def fetch_ptb(slug: str) -> float | None:
    """Fetch PTB from Gamma API for a resolved window."""
    url = f"{GAMMA_URL}?slug={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not data:
            return None
        meta = data[0].get("eventMetadata")
        if not isinstance(meta, dict):
            return None
        ptb = meta.get("priceToBeat")
        if ptb is not None and float(ptb) > 0:
            return float(ptb)
    except Exception as e:
        print(f"  [ERR] {slug}: {e}")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes to DB")
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        print("=== DRY RUN (use --apply to write) ===\n")

    conn = connect_db()

    rows = fetch_all_dicts(conn, """
        SELECT window_start, btc_start_price, btc_end_price, actual_outcome
        FROM market_windows
        ORDER BY window_start ASC
    """)
    print(f"Total windows in DB: {len(rows)}\n")

    updated = 0
    skipped = 0
    failed = 0
    outcome_changed = 0
    deltas = []

    for i, row in enumerate(rows):
        ws = row["window_start"]
        old_start = float(row["btc_start_price"]) if row["btc_start_price"] else None
        end_price = float(row["btc_end_price"]) if row["btc_end_price"] else None
        old_outcome = row["actual_outcome"]

        slug = f"{SLUG_PREFIX}-{ws}"

        # Rate limit
        if i > 0 and i % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN)

        ptb = fetch_ptb(slug)
        if ptb is None:
            failed += 1
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(rows)}] {slug}: PTB unavailable")
            continue

        # Check if already correct
        if old_start is not None and abs(old_start - ptb) < 0.01:
            skipped += 1
            continue

        # Derive new outcome
        new_outcome = old_outcome
        if end_price is not None:
            new_outcome = "UP" if end_price >= ptb else "DOWN"

        delta = abs(ptb - old_start) if old_start else 0
        deltas.append(delta)

        oc_flag = ""
        if new_outcome != old_outcome:
            outcome_changed += 1
            oc_flag = f" *** OUTCOME CHANGED: {old_outcome} -> {new_outcome}"

        if delta > 10 or oc_flag:
            print(
                f"  [{i+1}/{len(rows)}] {slug}: "
                f"${old_start or 0:,.2f} -> ${ptb:,.2f} (delta=${delta:.2f})"
                f"{oc_flag}"
            )

        if not dry_run:
            execute_write(conn, """
                UPDATE market_windows
                SET btc_start_price = ?, actual_outcome = COALESCE(?, actual_outcome)
                WHERE window_start = ?
            """, (ptb, new_outcome, ws))

        updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'=' * 60}")
    print(f"Results:")
    print(f"  Total windows:    {len(rows)}")
    print(f"  Updated:          {updated}")
    print(f"  Already correct:  {skipped}")
    print(f"  Failed (no PTB):  {failed}")
    print(f"  Outcome changed:  {outcome_changed}")
    if deltas:
        avg_delta = sum(deltas) / len(deltas)
        max_delta = max(deltas)
        print(f"\n  Avg |delta|:      ${avg_delta:.2f}")
        print(f"  Max |delta|:      ${max_delta:.2f}")
    if dry_run:
        print(f"\n  *** DRY RUN — no changes written. Use --apply to apply. ***")
    else:
        print(f"\n  Changes committed to DB.")


if __name__ == "__main__":
    main()
