"""Entrypoint. Orchestrates scrape -> diff -> notify -> dashboard -> git push.

Usage:
    python src/run.py                       # minimal daily sweep (~1 credit)
    python src/run.py --mode full           # full monthly sweep (all 10 portals)
    python src/run.py --test                # smoke test: sends a test email, does NOT touch state
    python src/run.py --dry                 # scrape + diff but do NOT send email or update state/dashboard
    python src/run.py --dashboard-only      # scrape + update data.json + git push; do NOT touch state or send email
    python src/run.py --no-push             # do everything but skip git add/commit/push
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from dotenv import load_dotenv
from firecrawl import Firecrawl

from dashboard import update_data_json
from diff import diff_listings, load_state, save_state
from notifier import send_change_alert, send_test
from scraper import scrape_all


CONFIG_PATH = PROJECT_DIR / "config.json"
STATE_PATH = PROJECT_DIR / "state" / "seen.json"
LOG_DIR = PROJECT_DIR / "logs"


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    return log_path


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _get_credit_snapshot(fc: Firecrawl) -> dict[str, Any]:
    """Returns a dict with remaining_credits, plan_credits, billing_period_*."""
    try:
        u = fc.get_credit_usage()
        # pydantic BaseModel → dict
        if hasattr(u, "model_dump"):
            return u.model_dump()
        if isinstance(u, dict):
            return u
        # best-effort attribute grab
        return {
            "remaining_credits": getattr(u, "remaining_credits", None),
            "plan_credits": getattr(u, "plan_credits", None),
            "billing_period_start": getattr(u, "billing_period_start", None),
            "billing_period_end": getattr(u, "billing_period_end", None),
        }
    except Exception as e:
        logging.warning("could not fetch credit usage: %s", e)
        return {}


def _git_commit_and_push(msg: str) -> bool:
    """Add data.json + index.html, commit with msg, push. Returns True on success."""
    try:
        # Only stage dashboard artifacts — keep state/.env out.
        subprocess.run(["git", "add", "data.json", "index.html"], cwd=str(PROJECT_DIR), check=True, capture_output=True)
        # nothing staged? that's OK — exit quietly
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(PROJECT_DIR), capture_output=True,
        )
        if diff.returncode == 0:
            logging.info("git: no changes to commit")
            return True
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(PROJECT_DIR), check=True, capture_output=True,
        )
        push = subprocess.run(
            ["git", "push"],
            cwd=str(PROJECT_DIR), capture_output=True, text=True,
        )
        if push.returncode != 0:
            logging.error("git push failed: %s\n%s", push.stdout, push.stderr)
            return False
        logging.info("git push ok: %s", msg)
        return True
    except subprocess.CalledProcessError as e:
        logging.error("git command failed: %s\n%s\n%s", e.cmd, e.stdout, e.stderr)
        return False
    except FileNotFoundError:
        logging.error("git not found on PATH; skipping push")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["minimal", "full"], default="minimal",
                        help="minimal = StreetEasy only (daily, ~1 credit). full = all portals (monthly).")
    parser.add_argument("--test", action="store_true", help="smoke test: no state writes, send a test email")
    parser.add_argument("--dry", action="store_true", help="scrape + diff but do not send email or update state/dashboard")
    parser.add_argument("--dashboard-only", action="store_true",
                        help="scrape + write data.json + git push; do NOT touch seen.json or send email")
    parser.add_argument("--no-push", action="store_true", help="skip git push at the end")
    args = parser.parse_args()

    log_path = setup_logging()
    logging.info("=" * 60)
    logging.info("1070 Park monitor run starting (mode=%s, test=%s, dry=%s, dashboard_only=%s, no_push=%s)",
                 args.mode, args.test, args.dry, args.dashboard_only, args.no_push)
    logging.info("log: %s", log_path)

    load_dotenv(PROJECT_DIR / ".env")

    fc_key = os.getenv("FIRECRAWL_API_KEY")
    smtp_user = os.getenv("GMAIL_USER", "")
    smtp_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
    alert_to = os.getenv("ALERT_TO", smtp_user)
    alert_bcc = os.getenv("ALERT_BCC") or None
    if alert_bcc and alert_bcc == alert_to:
        alert_bcc = None

    if not fc_key:
        logging.error("FIRECRAWL_API_KEY missing in .env")
        return 2
    if not args.dashboard_only and (not smtp_user or not smtp_password):
        logging.error("GMAIL_USER / GMAIL_APP_PASSWORD missing in .env")
        return 2

    cfg = load_config()
    fc = Firecrawl(api_key=fc_key)

    credits_before = _get_credit_snapshot(fc)
    logging.info("credits before: %s", credits_before)

    try:
        listings, stats = scrape_all(fc, cfg, mode=args.mode)
    except Exception:
        logging.exception("scrape failed catastrophically")
        return 3

    logging.info("SCRAPE SUMMARY")
    logging.info("  mode:            %s", stats.mode)
    logging.info("  discovered_urls: %d", stats.discovered_urls)
    logging.info("  scrape_calls:    %d", stats.scrape_calls)
    logging.info("  search_calls:    %d", stats.search_calls)
    logging.info("  extract_calls:   %d", stats.extract_calls)
    logging.info("  parse_success:   %s (fallback=%s)", stats.parse_success, stats.fallback_triggered)
    logging.info("  per-source:      %s", stats.per_source_count)
    logging.info("  errors:          %d", len(stats.errors))
    for err in stats.errors:
        logging.info("    - %s", err)
    for l in listings:
        logging.info(
            "  listing: source=%s unit=%s price=%s status=%s url=%s",
            l.source, l.unit, l.price, l.status, l.listing_url,
        )

    credits_after = _get_credit_snapshot(fc)
    logging.info("credits after: %s", credits_after)

    if args.test:
        logging.info("TEST MODE — not writing state/dashboard, sending test email")
        ok = send_test(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            to_addr=alert_to,
            bcc_addr=alert_bcc,
            body_html=(
                "<p>1070 Park monitor test — ignore.</p>"
                f"<p>Discovered {stats.discovered_urls} URLs; "
                f"extracted {len(listings)} sale listings; "
                f"per-source {stats.per_source_count}.</p>"
            ),
        )
        logging.info("test email ok=%s", ok)
        return 0 if ok else 4

    # Determine changes vs. prior state — needed for dashboard history + alerts.
    prior = load_state(STATE_PATH)
    diff = diff_listings(
        listings, prior,
        price_change_threshold=int(cfg.get("price_change_threshold_usd", 1)),
    )
    is_baseline = len(prior) == 0
    logging.info("DIFF SUMMARY  prior_known=%d  changes=%d", len(prior), len(diff.changes))
    for c in diff.changes:
        logging.info("    %s: unit=%s url=%s", c.kind, c.listing.unit, c.listing.listing_url)

    if args.dry:
        logging.info("DRY MODE — no state, no email, no dashboard, no push")
        return 0

    # --- Decide about alerts + state persistence ---
    alerts_sent = 0
    run_status = "ok" if stats.parse_success else "fallback"

    if args.dashboard_only:
        logging.info("DASHBOARD-ONLY — not writing state, not sending email")
    else:
        # Persist state first so a failed email still advances seen.json.
        save_state(STATE_PATH, diff.updated_state)
        logging.info("state written to %s (%d records)", STATE_PATH, len(diff.updated_state))

        if is_baseline and diff.changes:
            logging.info(
                "BASELINE RUN — suppressing alert email for %d initial listings (state now seeded)",
                len(diff.changes),
            )
        elif diff.changes:
            ok = send_change_alert(
                diff.changes,
                smtp_user=smtp_user, smtp_password=smtp_password,
                to_addr=alert_to, bcc_addr=alert_bcc,
            )
            if ok:
                alerts_sent = 1
            else:
                logging.error("alert email failed — state is updated; next run will not re-alert")
                run_status = "error"
        else:
            logging.info("no changes — no email")

    # --- Update data.json for the dashboard. Baseline and --dashboard-only runs
    # produce no history events (changes list kept out of history for those).
    changes_for_history = [] if (is_baseline or args.dashboard_only) else diff.changes

    try:
        update_data_json(
            project_dir=PROJECT_DIR,
            building_cfg=cfg.get("building", {}),
            listings=listings,
            changes=changes_for_history,
            mode=stats.mode,
            credits_before=credits_before.get("remaining_credits"),
            credits_after=credits_after.get("remaining_credits"),
            plan_credits=credits_after.get("plan_credits") or credits_before.get("plan_credits"),
            billing_period_start=str(credits_after.get("billing_period_start") or ""),
            billing_period_end=str(credits_after.get("billing_period_end") or ""),
            alerts_sent=alerts_sent,
            run_status=run_status,
        )
    except Exception:
        logging.exception("dashboard update failed — continuing")

    # --- Git push ---
    if args.no_push:
        logging.info("--no-push set; skipping git push")
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        msg = f"dashboard: {stats.mode} sweep {ts} — {len(listings)} listings, {len(diff.changes)} changes"
        _git_commit_and_push(msg)

    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 99
    sys.exit(rc)
