"""Dashboard writer. Builds data.json consumed by index.html.

data.json schema:

{
  "building": { name, address, streeteasy_url, neighborhood, type, cross_streets },
  "last_updated": "2026-04-24T10:16:00Z",
  "current_listings": [<listing with price_per_sqft computed>],
  "history": {
    "<unit>": [
       { "ts": "...", "kind": "first_seen|price|status", "price": ..., "status": ..., "note": "..." }
    ]
  },
  "run_log": [
    { "ts": ..., "mode": ..., "listings_found": n, "changes": n,
      "credits_used": n, "credits_remaining": n, "alerts_sent": n, "status": "ok" }
  ],
  "credits": {
    "plan_credits": 3000,
    "remaining_credits": n,
    "used_this_cycle": n,
    "billing_period_start": "...",
    "billing_period_end": "..."
  }
}

run_log is truncated to the last RUN_LOG_MAX entries.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from diff import Change
from schema import Listing

log = logging.getLogger(__name__)

RUN_LOG_MAX = 200  # index.html shows last 30; we keep more in the file for replay
DATA_FILENAME = "data.json"


@dataclass
class RunLogEntry:
    ts: str
    mode: str
    listings_found: int
    changes: int
    credits_used: int | None
    credits_remaining: int | None
    alerts_sent: int
    status: str  # "ok" | "error" | "fallback"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _listing_to_dict(l: Listing) -> dict[str, Any]:
    ppsf = None
    if l.price and l.square_feet:
        ppsf = round(l.price / l.square_feet)
    return {
        "source": l.source,
        "listing_url": l.listing_url,
        "unit": l.unit,
        "price": l.price,
        "bedrooms": l.bedrooms,
        "bathrooms": l.bathrooms,
        "square_feet": l.square_feet,
        "price_per_sqft": ppsf,
        "maintenance": l.maintenance,
        "broker": l.broker,
        "status": l.status,
        "listed_date": l.listed_date,
        "image_url": l.image_url,
        "scraped_at": l.scraped_at,
    }


def _load_existing(data_path: Path) -> dict[str, Any]:
    if not data_path.exists():
        return {}
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("existing data.json unreadable, starting fresh: %s", e)
        return {}


def _append_history(
    history: dict[str, list[dict[str, Any]]],
    changes: list[Change],
    ts: str,
) -> None:
    """Append change events to per-unit timeline."""
    for c in changes:
        key = c.listing.unit or c.listing.listing_url
        events = history.setdefault(key, [])
        if c.kind == "new":
            events.append({
                "ts": ts,
                "kind": "first_seen",
                "price": c.new_price,
                "status": c.new_status,
                "note": "First appeared on StreetEasy",
                "listing_url": c.listing.listing_url,
            })
        elif c.kind == "price":
            delta = (c.new_price or 0) - (c.old_price or 0)
            pct = (delta / c.old_price * 100) if c.old_price else 0
            events.append({
                "ts": ts,
                "kind": "price",
                "price": c.new_price,
                "old_price": c.old_price,
                "delta": delta,
                "pct": round(pct, 1),
                "status": c.new_status,
                "note": f"Price {'cut' if delta < 0 else 'raised'} by ${abs(delta):,} ({pct:+.1f}%)",
                "listing_url": c.listing.listing_url,
            })
        elif c.kind == "status":
            events.append({
                "ts": ts,
                "kind": "status",
                "status": c.new_status,
                "old_status": c.old_status,
                "price": c.new_price,
                "note": f"Status changed: {c.old_status} → {c.new_status}",
                "listing_url": c.listing.listing_url,
            })


def update_data_json(
    *,
    project_dir: Path,
    building_cfg: dict[str, Any],
    listings: list[Listing],
    changes: list[Change],
    mode: str,
    credits_before: int | None,
    credits_after: int | None,
    plan_credits: int | None,
    billing_period_start: str | None,
    billing_period_end: str | None,
    alerts_sent: int,
    run_status: str = "ok",
) -> Path:
    """Update repo-root data.json with latest scrape + run log entry.

    Returns the path written.
    """
    data_path = project_dir / DATA_FILENAME
    ts = _iso_now()
    existing = _load_existing(data_path)

    history = existing.get("history") or {}
    if not isinstance(history, dict):
        history = {}
    _append_history(history, changes, ts)

    credits_used_this_run = None
    if credits_before is not None and credits_after is not None:
        credits_used_this_run = max(credits_before - credits_after, 0)

    used_this_cycle = None
    if plan_credits is not None and credits_after is not None:
        used_this_cycle = max(plan_credits - credits_after, 0)

    run_entry = {
        "ts": ts,
        "mode": mode,
        "listings_found": len(listings),
        "changes": len(changes),
        "credits_used": credits_used_this_run,
        "credits_remaining": credits_after,
        "alerts_sent": alerts_sent,
        "status": run_status,
    }

    run_log = existing.get("run_log") or []
    if not isinstance(run_log, list):
        run_log = []
    run_log.append(run_entry)
    run_log = run_log[-RUN_LOG_MAX:]

    data = {
        "building": {
            "name": building_cfg.get("name", "1070 Park Avenue"),
            "address": building_cfg.get("address", "1070 Park Avenue, New York, NY 10128"),
            "neighborhood": building_cfg.get("neighborhood", "Carnegie Hill"),
            "cross_streets": building_cfg.get("cross_streets", ""),
            "type": building_cfg.get("type", "prewar co-op"),
            "streeteasy_url": "https://streeteasy.com/building/1070-park-avenue-new_york",
        },
        "last_updated": ts,
        "current_listings": [_listing_to_dict(l) for l in listings],
        "history": history,
        "run_log": run_log,
        "credits": {
            "plan_credits": plan_credits,
            "remaining_credits": credits_after,
            "used_this_cycle": used_this_cycle,
            "billing_period_start": billing_period_start,
            "billing_period_end": billing_period_end,
        },
    }

    data_path.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    log.info("wrote %s (%d current, %d history units, %d run log entries)",
             data_path, len(listings), len(history), len(run_log))
    return data_path
