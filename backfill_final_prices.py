"""
One-time backfill: scrape Final price from Polymarket for recent windows,
correct btc_end_price and actual_outcome in DB, recalculate paper/live PnL.

Usage:
    python backfill_final_prices.py [--hours 24] [--dry-run]
"""
import argparse
import json
import logging
import os
import re
import sys
import time

from dotenv import load_dotenv

# Load env before other imports
from env_paths import PUBLIC_RUNTIME_ENV_PATH, SECRETS_ENV_PATH

load_dotenv(SECRETS_ENV_PATH, override=True)
load_dotenv(PUBLIC_RUNTIME_ENV_PATH, override=True)

from config import config
from db_config import (
    connect_db,
    execute_write,
    fetch_all_dicts,
    fetch_one,
    init_market_schema,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_fp")


def scrape_final_price_standalone(slug: str) -> float | None:
    """Scrape Final price from Polymarket using Playwright (standalone, no PolyClient)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright not installed")
        return None

    url = f"https://polymarket.com/event/{slug}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(3)

            text = page.evaluate("""() => {
                const body = document.body.innerText;
                const result = {};
                const fpIdx = body.indexOf('Final price');
                if (fpIdx !== -1) {
                    result.fp = body.substring(fpIdx + 11, fpIdx + 200);
                }
                const ptbIdx = body.indexOf('Price to beat');
                if (ptbIdx !== -1) {
                    result.ptb = body.substring(ptbIdx + 13, ptbIdx + 200);
                }
                return JSON.stringify(result);
            }""")

            page.close()
            browser.close()

            if not text:
                return None

            data = json.loads(text)
            fp_text = data.get("fp", "")
            if fp_text:
                m = re.search(r'\$?([\d,]+\.\d{2})', fp_text)
                if m:
                    val = float(m.group(1).replace(",", ""))
                    if val > 10000:
                        return val
    except Exception as e:
        logger.debug("Scrape failed for %s: %s", slug, e)
    return None


def scrape_ptb_and_final(slug: str) -> tuple[float | None, float | None]:
    """Scrape both PTB and Final price from resolved page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None

    url = f"https://polymarket.com/event/{slug}"
    ptb_val = None
    fp_val = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(3)

            text = page.evaluate("""() => {
                const body = document.body.innerText;
                const result = {};
                const fpIdx = body.indexOf('Final price');
                if (fpIdx !== -1) {
                    result.fp = body.substring(fpIdx + 11, fpIdx + 200);
                }
                const ptbIdx = body.indexOf('Price to beat');
                if (ptbIdx !== -1) {
                    result.ptb = body.substring(ptbIdx + 13, ptbIdx + 200);
                }
                return JSON.stringify(result);
            }""")

            page.close()
            browser.close()

            if not text:
                return None, None

            data = json.loads(text)

            fp_text = data.get("fp", "")
            if fp_text:
                m = re.search(r'\$?([\d,]+\.\d{2})', fp_text)
                if m:
                    v = float(m.group(1).replace(",", ""))
                    if v > 10000:
                        fp_val = v

            ptb_text = data.get("ptb", "")
            if ptb_text:
                m = re.search(r'\$?([\d,]+\.\d{2})', ptb_text)
                if m:
                    v = float(m.group(1).replace(",", ""))
                    if v > 10000:
                        ptb_val = v

    except Exception as e:
        logger.debug("Scrape failed for %s: %s", slug, e)
    return ptb_val, fp_val


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect_db()
    init_market_schema(conn)
    conn.commit()

    cutoff = int(time.time()) - args.hours * 3600
    rows = fetch_all_dicts(
        conn,
        """SELECT window_start, slug, btc_start_price, btc_end_price, actual_outcome
           FROM market_windows
           WHERE window_start >= ? AND actual_outcome IS NOT NULL
           ORDER BY window_start ASC""",
        (cutoff,),
    )

    logger.info("Found %d windows in last %d hours to check", len(rows), args.hours)

    corrections = []
    checked = 0
    skipped = 0

    for row in rows:
        ws = int(row["window_start"])
        slug = str(row["slug"])
        db_start = float(row["btc_start_price"]) if row["btc_start_price"] else None
        db_end = float(row["btc_end_price"]) if row["btc_end_price"] else None
        db_outcome = str(row["actual_outcome"]) if row["actual_outcome"] else None

        # Skip very recent windows (final price not yet available)
        if ws > time.time() - 900:
            skipped += 1
            continue

        checked += 1
        logger.info("[%d/%d] Checking %s ...", checked, len(rows) - skipped, slug)

        ptb_scraped, fp_scraped = scrape_ptb_and_final(slug)

        if fp_scraped is None:
            logger.info("  No Final price found, skipping")
            time.sleep(1)
            continue

        # Use scraped PTB if available, else DB start price
        ptb = ptb_scraped if ptb_scraped is not None else db_start

        # Determine correct outcome from final price vs PTB
        if ptb is not None and ptb > 0:
            correct_outcome = "UP" if fp_scraped >= ptb else "DOWN"
        else:
            correct_outcome = db_outcome

        end_changed = db_end is not None and abs(fp_scraped - db_end) > 0.50
        ptb_changed = ptb_scraped is not None and db_start is not None and abs(ptb_scraped - db_start) > 0.50
        outcome_changed = correct_outcome != db_outcome

        if not end_changed and not outcome_changed and not ptb_changed:
            logger.info("  OK: end=$%.2f, outcome=%s (matches)", fp_scraped, correct_outcome)
            time.sleep(0.5)
            continue

        info = {
            "slug": slug,
            "window_start": ws,
            "old_end": db_end,
            "new_end": fp_scraped,
            "old_ptb": db_start,
            "new_ptb": ptb_scraped,
            "old_outcome": db_outcome,
            "new_outcome": correct_outcome,
            "end_changed": end_changed,
            "ptb_changed": ptb_changed,
            "outcome_changed": outcome_changed,
        }

        logger.warning(
            "  MISMATCH: end $%.2f→$%.2f | ptb $%.2f→$%.2f | outcome %s→%s",
            db_end or 0, fp_scraped,
            db_start or 0, ptb_scraped or db_start or 0,
            db_outcome, correct_outcome,
        )

        if args.dry_run:
            corrections.append(info)
            time.sleep(0.5)
            continue

        # ── Update market_windows ──
        update_fields = ["btc_end_price = ?"]
        update_params = [fp_scraped]
        if ptb_scraped is not None:
            update_fields.append("btc_start_price = ?")
            update_params.append(ptb_scraped)
        if outcome_changed:
            update_fields.append("actual_outcome = ?")
            update_params.append(correct_outcome)
        update_params.append(ws)
        execute_write(
            conn,
            f"UPDATE market_windows SET {', '.join(update_fields)} WHERE window_start = ?",
            tuple(update_params),
        )

        # ── Correct paper_trades if outcome changed ──
        trade_corrections = []
        if outcome_changed:
            paper_rows = fetch_all_dicts(
                conn,
                """SELECT id, direction, stake, entry_price, pnl
                   FROM paper_trades
                   WHERE window_start = ? AND archived_at IS NULL""",
                (ws,),
            )
            for pt in paper_rows:
                direction = str(pt.get("direction", ""))
                stake = float(pt.get("stake") or 0)
                entry_price = float(pt.get("entry_price") or 0)
                old_pnl = float(pt.get("pnl") or 0)
                if not direction or stake <= 0 or entry_price <= 0:
                    continue
                won = direction == correct_outcome
                shares = stake / entry_price
                new_pnl = (shares * 1.0 - stake) if won else -stake
                execute_write(
                    conn,
                    """UPDATE paper_trades
                       SET pnl = ?, exit_price = ?,
                           close_type = CONCAT(COALESCE(close_type,''), ' [adj: %s→%s]')
                       WHERE id = ?""" % (db_outcome, correct_outcome),
                    (new_pnl, 1.0 if won else 0.0, pt["id"]),
                )
                trade_corrections.append(
                    f"    Paper #{pt['id']} {direction}: ${old_pnl:+.2f} → ${new_pnl:+.2f}"
                )

            # ── Correct live_trades ──
            for table in ("trades", "live_trades"):
                try:
                    live_rows = fetch_all_dicts(
                        conn,
                        f"""SELECT id, direction, amount, price, pnl
                            FROM {table}
                            WHERE window_start = ? AND status = 'CLOSED'""",
                        (ws,),
                    )
                except Exception:
                    continue
                for lt in live_rows:
                    direction = str(lt.get("direction", ""))
                    stake = float(lt.get("amount") or 0)
                    entry_price = float(lt.get("price") or 0)
                    old_pnl = float(lt.get("pnl") or 0)
                    if not direction or stake <= 0 or entry_price <= 0:
                        continue
                    won = direction == correct_outcome
                    shares = stake / entry_price
                    new_pnl = (shares * 1.0 - stake) if won else -stake
                    execute_write(
                        conn,
                        f"""UPDATE {table}
                            SET actual_outcome = ?, won = ?, pnl = ?,
                                close_reason = CONCAT(COALESCE(close_reason,''), ' [adj: {db_outcome}→{correct_outcome}]')
                            WHERE id = ?""",
                        (correct_outcome, 1 if won else 0, new_pnl, lt["id"]),
                    )
                    trade_corrections.append(
                        f"    Live #{lt['id']} {direction}: ${old_pnl:+.2f} → ${new_pnl:+.2f}"
                    )

        conn.commit()
        info["trade_corrections"] = trade_corrections
        corrections.append(info)

        if trade_corrections:
            for tc in trade_corrections:
                logger.warning(tc)

        time.sleep(1)

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f" BACKFILL SUMMARY ({args.hours}h)")
    print("=" * 60)
    print(f"  Windows checked:    {checked}")
    print(f"  Corrections:        {len(corrections)}")

    end_fixes = sum(1 for c in corrections if c["end_changed"])
    ptb_fixes = sum(1 for c in corrections if c["ptb_changed"])
    outcome_fixes = sum(1 for c in corrections if c["outcome_changed"])
    print(f"    End price fixed:  {end_fixes}")
    print(f"    PTB fixed:        {ptb_fixes}")
    print(f"    Outcome flipped:  {outcome_fixes}")

    if outcome_fixes > 0:
        print("\n  Outcome changes:")
        for c in corrections:
            if c["outcome_changed"]:
                print(f"    {c['slug']}: {c['old_outcome']} → {c['new_outcome']}")
                for tc in c.get("trade_corrections", []):
                    print(f"  {tc}")

    if args.dry_run:
        print("\n  *** DRY RUN — no changes written ***")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
