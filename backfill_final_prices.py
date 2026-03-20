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

# ── Persistent browser ──
_browser = None
_pw_ctx = None


def _get_browser():
    global _browser, _pw_ctx
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw_ctx = sync_playwright().start()
        _browser = _pw_ctx.chromium.launch(headless=True)
    return _browser


def _close_browser():
    global _browser, _pw_ctx
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw_ctx:
        try:
            _pw_ctx.stop()
        except Exception:
            pass
        _pw_ctx = None


def scrape_ptb_and_final(slug: str) -> tuple[float | None, float | None]:
    """Scrape PTB and Final price using persistent browser (single tab, reused)."""
    url = f"https://polymarket.com/event/{slug}"
    ptb_val = None
    fp_val = None
    page = None
    try:
        browser = _get_browser()
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=12000)
        time.sleep(2)

        text = page.evaluate("""() => {
            const body = document.body.innerText;
            const result = {};
            const fpIdx = body.indexOf('Final price');
            if (fpIdx !== -1) result.fp = body.substring(fpIdx + 11, fpIdx + 200);
            const ptbIdx = body.indexOf('Price to beat');
            if (ptbIdx !== -1) result.ptb = body.substring(ptbIdx + 13, ptbIdx + 200);
            return JSON.stringify(result);
        }""")

        if text:
            data = json.loads(text)
            for key, target in [("fp", "fp_val"), ("ptb", "ptb_val")]:
                raw = data.get(key, "")
                if raw:
                    m = re.search(r'\$?([\d,]+\.\d{2})', raw)
                    if m:
                        v = float(m.group(1).replace(",", ""))
                        if v > 10000:
                            if key == "fp":
                                fp_val = v
                            else:
                                ptb_val = v
    except Exception as e:
        logger.debug("Scrape failed for %s: %s", slug, e)
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
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

    total = len(rows)
    logger.info("Found %d windows in last %d hours to check", total, args.hours)

    corrections = []
    checked = 0
    skipped = 0

    try:
        for row in rows:
            ws = int(row["window_start"])
            slug = str(row["slug"])
            db_start = float(row["btc_start_price"]) if row["btc_start_price"] else None
            db_end = float(row["btc_end_price"]) if row["btc_end_price"] else None
            db_outcome = str(row["actual_outcome"]) if row["actual_outcome"] else None

            if ws > time.time() - 900:
                skipped += 1
                continue

            checked += 1
            if checked % 20 == 1:
                logger.info("[%d/%d] Checking %s ...", checked, total - skipped, slug)

            try:
                ptb_scraped, fp_scraped = scrape_ptb_and_final(slug)
            except Exception as e:
                logger.warning("  Scrape error, skipping: %s", e)
                continue

            if fp_scraped is None:
                continue

            ptb = ptb_scraped if ptb_scraped is not None else db_start
            if ptb is not None and ptb > 0:
                correct_outcome = "UP" if fp_scraped >= ptb else "DOWN"
            else:
                correct_outcome = db_outcome

            end_changed = db_end is not None and abs(fp_scraped - db_end) > 0.50
            ptb_changed = ptb_scraped is not None and db_start is not None and abs(ptb_scraped - db_start) > 0.50
            outcome_changed = correct_outcome != db_outcome

            if not end_changed and not outcome_changed and not ptb_changed:
                continue

            info = {
                "slug": slug, "window_start": ws,
                "old_end": db_end, "new_end": fp_scraped,
                "old_ptb": db_start, "new_ptb": ptb_scraped,
                "old_outcome": db_outcome, "new_outcome": correct_outcome,
                "end_changed": end_changed, "ptb_changed": ptb_changed,
                "outcome_changed": outcome_changed, "trade_corrections": [],
            }

            if outcome_changed:
                logger.warning(
                    "  OUTCOME FLIP %s: %s→%s (fp=$%.2f ptb=$%.2f)",
                    slug, db_outcome, correct_outcome, fp_scraped, ptb or 0,
                )
            elif end_changed or ptb_changed:
                logger.info(
                    "  Price fix %s: end $%.2f→$%.2f ptb $%.2f→$%.2f",
                    slug, db_end or 0, fp_scraped,
                    db_start or 0, ptb_scraped or db_start or 0,
                )

            if args.dry_run:
                corrections.append(info)
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

            # ── Correct trades if outcome changed ──
            trade_corrections = []
            if outcome_changed:
                # Paper trades
                paper_rows = fetch_all_dicts(
                    conn,
                    "SELECT id, direction, stake, entry_price, pnl FROM paper_trades WHERE window_start = ? AND archived_at IS NULL",
                    (ws,),
                )
                for pt in paper_rows:
                    d, s, ep, op = str(pt["direction"]), float(pt["stake"] or 0), float(pt["entry_price"] or 0), float(pt["pnl"] or 0)
                    if not d or s <= 0 or ep <= 0:
                        continue
                    won = d == correct_outcome
                    np_ = (s / ep * 1.0 - s) if won else -s
                    execute_write(
                        conn,
                        "UPDATE paper_trades SET pnl = ?, exit_price = ?, close_type = CONCAT(COALESCE(close_type,''), %s) WHERE id = ?",
                        (np_, 1.0 if won else 0.0, f" [adj: {db_outcome}->{correct_outcome}]", pt["id"]),
                    )
                    trade_corrections.append(f"  Paper #{pt['id']} {d}: ${op:+.2f}→${np_:+.2f}")

                # Live trades
                for table in ("trades", "live_trades"):
                    try:
                        live_rows = fetch_all_dicts(
                            conn,
                            f"SELECT id, direction, amount, price, pnl FROM {table} WHERE window_start = ? AND status = 'CLOSED'",
                            (ws,),
                        )
                    except Exception:
                        continue
                    for lt in live_rows:
                        d, s, ep, op = str(lt["direction"]), float(lt["amount"] or 0), float(lt["price"] or 0), float(lt["pnl"] or 0)
                        if not d or s <= 0 or ep <= 0:
                            continue
                        won = d == correct_outcome
                        np_ = (s / ep * 1.0 - s) if won else -s
                        execute_write(
                            conn,
                            f"UPDATE {table} SET actual_outcome=?, won=?, pnl=?, close_reason=CONCAT(COALESCE(close_reason,''), %s) WHERE id=?",
                            (correct_outcome, 1 if won else 0, np_, f" [adj: {db_outcome}->{correct_outcome}]", lt["id"]),
                        )
                        trade_corrections.append(f"  Live #{lt['id']} {d}: ${op:+.2f}→${np_:+.2f}")

            conn.commit()
            info["trade_corrections"] = trade_corrections
            corrections.append(info)

            for tc in trade_corrections:
                logger.warning(tc)

    finally:
        _close_browser()

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
                print(f"    {c['slug']}: {c['old_outcome']} -> {c['new_outcome']}")
                for tc in c.get("trade_corrections", []):
                    print(f"  {tc}")

    if args.dry_run:
        print("\n  *** DRY RUN — no changes written ***")
    print("=" * 60)
    conn.close()


if __name__ == "__main__":
    main()
