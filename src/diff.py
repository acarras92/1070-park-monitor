"""Diff a fresh scrape against persisted state.

State file format (state/seen.json):
{
  "<listing_url>": {
    "first_seen": "2026-04-24T12:00:00Z",
    "last_seen": "2026-04-24T16:00:00Z",
    "last_price": 4250000,
    "last_status": "active",
    "unit": "5B",
    "source": "streeteasy"
  }
}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema import Listing

log = logging.getLogger(__name__)


@dataclass
class Change:
    kind: str  # "new" | "price" | "status"
    listing: Listing
    old_price: int | None = None
    new_price: int | None = None
    old_status: str | None = None
    new_status: str | None = None


@dataclass
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    updated_state: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("state file unreadable, starting fresh: %s", e)
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def diff_listings(
    listings: list[Listing],
    prior_state: dict[str, dict[str, Any]],
    *,
    price_change_threshold: int = 1,
) -> DiffResult:
    now_iso = datetime.now(timezone.utc).isoformat()
    result = DiffResult()
    # start from prior so off-market listings keep their last record
    result.updated_state = {k: dict(v) for k, v in prior_state.items()}

    for l in listings:
        key = l.listing_url
        prior = prior_state.get(key)

        if prior is None:
            result.changes.append(Change(kind="new", listing=l, new_price=l.price, new_status=l.status))
            result.updated_state[key] = {
                "first_seen": now_iso,
                "last_seen": now_iso,
                "last_price": l.price,
                "last_status": l.status,
                "unit": l.unit,
                "source": l.source,
            }
            continue

        prior_price = prior.get("last_price")
        prior_status = prior.get("last_status")

        if (
            l.price is not None
            and prior_price is not None
            and abs(l.price - prior_price) >= price_change_threshold
        ):
            result.changes.append(
                Change(
                    kind="price",
                    listing=l,
                    old_price=prior_price,
                    new_price=l.price,
                    old_status=prior_status,
                    new_status=l.status,
                )
            )

        if prior_status and l.status and l.status != prior_status:
            result.changes.append(
                Change(
                    kind="status",
                    listing=l,
                    old_price=prior_price,
                    new_price=l.price,
                    old_status=prior_status,
                    new_status=l.status,
                )
            )

        # update the record
        result.updated_state[key] = {
            "first_seen": prior.get("first_seen", now_iso),
            "last_seen": now_iso,
            "last_price": l.price if l.price is not None else prior_price,
            "last_status": l.status or prior_status,
            "unit": l.unit or prior.get("unit"),
            "source": l.source,
        }

    return result
