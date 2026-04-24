"""Firecrawl-backed scrapers for 1070 Park Avenue listings.

Two modes:

  MINIMAL (default, daily)
    One Firecrawl scrape() call against the StreetEasy building page with
    format=['markdown']. Listing cards are parsed locally with regex.
    Cost: ~1 credit per sweep.
    If the local parser returns 0 listings but the page's own
    "N units for sale" header says N>0, we fall back to a single extract()
    call as a one-shot safety net. Never the default path.

  FULL (monthly, --mode full)
    Unchanged from v1: search() each configured portal, filter candidate
    URLs, batch them through extract() with a structured schema.
    Cost: ~250-300 credits. Catches whisper listings on Compass, BHS, etc.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

from firecrawl import Firecrawl

from schema import Listing

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass
class ScrapeStats:
    mode: str = "minimal"
    per_source_count: dict[str, int] = field(default_factory=dict)
    discovered_urls: int = 0
    scrape_calls: int = 0
    extract_calls: int = 0
    search_calls: int = 0
    parse_success: bool = True
    fallback_triggered: bool = False
    errors: list[str] = field(default_factory=list)


ALLOWED_DOMAINS = {
    "streeteasy.com",
    "compass.com",
    "corcoran.com",
    "elliman.com",
    "bhsusa.com",
    "sothebysrealty.com",
    "cityrealty.com",
    "zillow.com",
    "realtor.com",
    "redfin.com",
}


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host.lower().lstrip("www.")
    except Exception:
        return ""


def _source_name(url: str) -> str:
    d = _domain(url)
    for allowed in ALLOWED_DOMAINS:
        if d.endswith(allowed):
            return allowed.split(".")[0]
    return d or "unknown"


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else None
    return None


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"(\d+(?:\.\d+)?)", v)
        return float(m.group(1)) if m else None
    return None


# ---------------------------------------------------------------------------
# MINIMAL MODE: StreetEasy markdown scrape + local parse
# ---------------------------------------------------------------------------


STREETEASY_COUNT_RE = re.compile(
    r"##\s*(?P<n>\d+)\s+units?\s+for\s+sale", re.IGNORECASE
)
STREETEASY_AVAILABLE_SECTION_RE = re.compile(
    r"##\s*Available units(?P<body>.*?)(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# Per-listing unit header inside the "Available units" section.
STREETEASY_UNIT_HEADER_RE = re.compile(
    r"\[#(?P<unit>[^\]\n]+?)\]\((?P<url>https?://streeteasy\.com/building/[^)]+)\)",
    re.IGNORECASE,
)

_PRICE_RE = re.compile(r"\$([\d,]+)")
_STATUS_RE = re.compile(
    r"\b(?:(In\s+contract)|(Sold)|(Pending)|(Off\s+market)|(Active)|(Available))\b",
    re.IGNORECASE,
)
_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*beds?", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*baths?", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]+)\s*ft[²²\^2]", re.IGNORECASE)
_BROKER_RE = re.compile(r"Listing by\s+([^\n]+?)\s*(?:\n|$)", re.IGNORECASE)
_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((?P<src>https?://[^)\s]+?\.(?:jpe?g|png|gif|webp))\)",
    re.IGNORECASE,
)


def _normalize_status(block_text: str) -> str:
    """Infer listing status from the card text.

    Default is 'active' — StreetEasy only shows explicit badges for non-active
    states ("In contract", "Sold", "Pending"). Absence of any badge = active.
    """
    m = _STATUS_RE.search(block_text)
    if not m:
        return "active"
    tag = m.group(0).lower().strip()
    if "contract" in tag or "pending" in tag:
        return "in_contract"
    if "sold" in tag:
        return "sold"
    if "off" in tag:
        return "off_market"
    return "active"


def _fetch_markdown(fc: Firecrawl, url: str) -> str:
    """One Firecrawl scrape() call. Raises on failure."""
    doc = fc.scrape(url, formats=["markdown"], only_main_content=True)
    md = getattr(doc, "markdown", None)
    if md is None and hasattr(doc, "data"):
        data = doc.data
        md = (data.get("markdown") if isinstance(data, dict) else getattr(data, "markdown", None))
    if md is None and isinstance(doc, dict):
        md = doc.get("markdown") or doc.get("data", {}).get("markdown")
    return md or ""


def parse_streeteasy_markdown(md: str, *, building_address: str) -> tuple[list[Listing], int]:
    """Parse the StreetEasy building page markdown.

    Returns (listings, expected_count). `expected_count` is the number of
    sale units the page itself advertises in its header, which the caller
    uses to decide whether a 0-result parse warrants an extract() fallback.
    """
    expected_count = 0
    cm = STREETEASY_COUNT_RE.search(md)
    if cm:
        try:
            expected_count = int(cm.group("n"))
        except ValueError:
            expected_count = 0

    sm = STREETEASY_AVAILABLE_SECTION_RE.search(md)
    if not sm:
        log.info("no 'Available units' section found in markdown")
        return [], expected_count
    body = sm.group("body")

    # Find each listing card by unit-header position; each block runs from
    # one header to the next (or end of section).
    headers = list(STREETEASY_UNIT_HEADER_RE.finditer(body))
    listings: list[Listing] = []
    for i, hm in enumerate(headers):
        start = hm.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        block = body[start:end]

        unit = hm.group("unit").strip()
        url = hm.group("url").strip()

        price_m = _PRICE_RE.search(block)
        price = _safe_int(price_m.group(1)) if price_m else None

        beds_m = _BEDS_RE.search(block)
        beds = _safe_float(beds_m.group(1)) if beds_m else None

        baths_m = _BATHS_RE.search(block)
        baths = _safe_float(baths_m.group(1)) if baths_m else None

        sqft_m = _SQFT_RE.search(block)
        sqft = _safe_int(sqft_m.group(1)) if sqft_m else None

        broker_m = _BROKER_RE.search(block)
        broker = broker_m.group(1).strip() if broker_m else None

        image_m = _IMAGE_RE.search(block)
        image = image_m.group("src") if image_m else None

        status = _normalize_status(block)

        try:
            listings.append(
                Listing(
                    source="streeteasy",
                    listing_url=url,
                    address=building_address,
                    unit=unit,
                    price=price,
                    bedrooms=beds,
                    bathrooms=baths,
                    square_feet=sqft,
                    maintenance=None,  # not shown on building page
                    broker=broker,
                    status=status,
                    listed_date=None,
                    image_url=image,
                )
            )
        except Exception as e:
            log.warning("failed to build Listing for unit %s: %s", unit, e)

    return listings, expected_count


def _extract_fallback(fc: Firecrawl, url: str, address: str) -> list[Listing]:
    """One-shot safety net when markdown parsing returns 0 but the page
    advertised N>0 listings. Uses Firecrawl extract() with a schema."""
    schema = {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unit": {"type": "string"},
                        "price": {"type": "number"},
                        "bedrooms": {"type": "number"},
                        "bathrooms": {"type": "number"},
                        "square_feet": {"type": "number"},
                        "broker": {"type": "string"},
                        "status": {"type": "string"},
                        "listing_url": {"type": "string"},
                        "image_url": {"type": "string"},
                        "is_rental": {"type": "boolean"},
                    },
                },
            }
        },
        "required": ["listings"],
    }
    prompt = (
        "Extract ONLY SALE listings (not rentals) at 1070 Park Avenue on this "
        "StreetEasy building page. Each listing needs unit, price, bedrooms, "
        "bathrooms, square_feet, broker, status (active/in_contract/sold), "
        "listing_url, image_url."
    )
    try:
        result = fc.extract(
            urls=[url], prompt=prompt, schema=schema, enable_web_search=False
        )
        data = getattr(result, "data", None)
        if data is None and isinstance(result, dict):
            data = result.get("data", result)
        if not isinstance(data, dict):
            return []
        out: list[Listing] = []
        for raw in data.get("listings", []) or []:
            if not isinstance(raw, dict) or raw.get("is_rental"):
                continue
            price = _safe_int(raw.get("price"))
            if price is not None and price < 100_000:
                continue
            try:
                out.append(
                    Listing(
                        source="streeteasy",
                        listing_url=raw.get("listing_url") or url,
                        address=address,
                        unit=(raw.get("unit") or "").strip().lstrip("#") or None,
                        price=price,
                        bedrooms=_safe_float(raw.get("bedrooms")),
                        bathrooms=_safe_float(raw.get("bathrooms")),
                        square_feet=_safe_int(raw.get("square_feet")),
                        maintenance=None,
                        broker=raw.get("broker") or None,
                        status=raw.get("status") or "active",
                        listed_date=None,
                        image_url=raw.get("image_url") or None,
                    )
                )
            except Exception as e:
                log.warning("fallback: could not build Listing from %r: %s", raw, e)
        return out
    except Exception as e:
        log.exception("extract fallback failed: %s", e)
        return []


def scrape_minimal(fc: Firecrawl, cfg: dict) -> tuple[list[Listing], ScrapeStats]:
    """Daily sweep. One scrape() call. Parse markdown locally."""
    stats = ScrapeStats(mode="minimal")
    url = cfg["minimal_mode"]["streeteasy_building_url"]
    address = cfg["building"]["address"]

    try:
        md = _fetch_markdown(fc, url)
        stats.scrape_calls = 1
    except Exception as e:
        log.exception("streeteasy scrape failed: %s", e)
        stats.errors.append(f"streeteasy scrape failed: {e}")
        return [], stats

    if not md:
        stats.errors.append("streeteasy returned empty markdown")
        stats.parse_success = False
        return [], stats

    listings, expected = parse_streeteasy_markdown(md, building_address=address)
    log.info(
        "parsed %d listings from StreetEasy markdown (page header said %d units for sale)",
        len(listings), expected,
    )

    # Fallback — only if the page actually claims listings but parser came back empty.
    if not listings and expected > 0:
        log.warning("parse returned 0 but page claims %d — falling back to extract() once", expected)
        stats.fallback_triggered = True
        stats.parse_success = False
        fallback = _extract_fallback(fc, url, address)
        stats.extract_calls = 1
        listings.extend(fallback)

    for l in listings:
        stats.per_source_count[l.source] = stats.per_source_count.get(l.source, 0) + 1

    return listings, stats


# ---------------------------------------------------------------------------
# FULL MODE: all portals via search + extract (monthly sweep)
# ---------------------------------------------------------------------------


BUILDING_RE = re.compile(r"1070[\s\-]*park", re.IGNORECASE)


FULL_LISTING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "listings": {
            "type": "array",
            "description": "All SALE listings (not rentals) at 1070 Park Avenue, New York NY 10128.",
            "items": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string"},
                    "price": {"type": "number"},
                    "bedrooms": {"type": "number"},
                    "bathrooms": {"type": "number"},
                    "square_feet": {"type": "number"},
                    "maintenance": {"type": "number"},
                    "broker": {"type": "string"},
                    "status": {"type": "string"},
                    "listed_date": {"type": "string"},
                    "listing_url": {"type": "string"},
                    "image_url": {"type": "string"},
                    "is_rental": {"type": "boolean"},
                },
                "required": ["status"],
            },
        }
    },
    "required": ["listings"],
}

FULL_EXTRACT_PROMPT = (
    "This page may contain real-estate listings at 1070 Park Avenue, New York, NY 10128 "
    "(a prewar co-op on the Upper East Side between 87th and 88th). "
    "Extract ONLY SALE listings at that exact building. "
    "EXCLUDE rentals. EXCLUDE listings at other addresses. "
    "If a unit is in contract, closed, or sold, include it with that status. "
    "If no qualifying listings are present, return listings: []."
)


def _search_with_retry(fc: Firecrawl, query: str, limit: int = 10, max_attempts: int = 3) -> list[Any]:
    for attempt in range(max_attempts):
        try:
            resp = fc.search(query=query, limit=limit, sources=["web"])
            web = getattr(resp, "web", None)
            if web is None and hasattr(resp, "data"):
                web = getattr(resp.data, "web", None) if not isinstance(resp.data, dict) else resp.data.get("web")
            if web is None and isinstance(resp, dict):
                web = resp.get("web") or resp.get("data", {}).get("web")
            return list(web or [])
        except Exception as e:
            wait = 2 ** attempt
            log.warning("search attempt %d failed for %r: %s (retry in %ds)", attempt + 1, query, e, wait)
            time.sleep(wait)
    log.error("search permanently failed for %r", query)
    return []


def _discover_urls_full(fc: Firecrawl, cfg: dict, stats: ScrapeStats) -> list[str]:
    full = cfg.get("full_mode", {})
    urls: set[str] = set()
    for _, v in full.get("direct_urls", {}).items():
        if v:
            urls.add(v)
    base_query = full.get("search_query", "1070 Park Avenue New York NY 10128 for sale")
    for domain in full.get("search_domains", []):
        q = f"{base_query} site:{domain}"
        results = _search_with_retry(fc, q, limit=8)
        stats.search_calls += 1
        for r in results:
            url = getattr(r, "url", None) or (r.get("url") if isinstance(r, dict) else None)
            title = getattr(r, "title", "") or (r.get("title", "") if isinstance(r, dict) else "")
            desc = getattr(r, "description", "") or (r.get("description", "") if isinstance(r, dict) else "")
            if not url:
                continue
            blob = f"{url} {title} {desc}"
            if not BUILDING_RE.search(blob):
                continue
            if not any(_domain(url).endswith(d) for d in ALLOWED_DOMAINS):
                continue
            urls.add(url)
    stats.discovered_urls = len(urls)
    log.info("discovered %d candidate URLs", len(urls))
    for u in sorted(urls):
        log.info("  - %s", u)
    return sorted(urls)


def _extract_with_retry(fc: Firecrawl, urls: list[str], max_attempts: int = 3) -> dict | None:
    for attempt in range(max_attempts):
        try:
            result = fc.extract(
                urls=urls,
                prompt=FULL_EXTRACT_PROMPT,
                schema=FULL_LISTING_SCHEMA,
                enable_web_search=False,
            )
            if hasattr(result, "data"):
                data = result.data
            elif isinstance(result, dict):
                data = result.get("data", result)
            else:
                data = result
            if isinstance(data, dict):
                return data
            return None
        except Exception as e:
            wait = 2 ** attempt * 3
            log.warning("extract attempt %d failed for %d URLs: %s (retry in %ds)", attempt + 1, len(urls), e, wait)
            time.sleep(wait)
    log.error("extract permanently failed for %d URLs", len(urls))
    return None


def _normalize_extracted(raw_listing: dict, source_url: str, address: str) -> Listing | None:
    if raw_listing.get("is_rental"):
        return None
    price = _safe_int(raw_listing.get("price"))
    if price is not None and price < 100_000:
        return None
    listing_url = raw_listing.get("listing_url") or source_url
    if not listing_url:
        return None
    unit = raw_listing.get("unit")
    if isinstance(unit, str):
        unit = unit.strip().lstrip("#").strip() or None
    try:
        return Listing(
            source=_source_name(listing_url),
            listing_url=listing_url,
            address=address,
            unit=unit,
            price=price,
            bedrooms=_safe_float(raw_listing.get("bedrooms")),
            bathrooms=_safe_float(raw_listing.get("bathrooms")),
            square_feet=_safe_int(raw_listing.get("square_feet")),
            maintenance=_safe_int(raw_listing.get("maintenance")),
            broker=(raw_listing.get("broker") or None),
            status=raw_listing.get("status") or "active",
            listed_date=raw_listing.get("listed_date") or None,
            image_url=raw_listing.get("image_url") or None,
        )
    except Exception as e:
        log.warning("failed to build Listing from %r: %s", raw_listing, e)
        return None


def dedupe_cross_source(listings: Iterable[Listing]) -> list[Listing]:
    source_priority = {
        "streeteasy": 0, "cityrealty": 1, "compass": 2, "corcoran": 3,
        "elliman": 4, "bhsusa": 5, "sothebysrealty": 6,
        "zillow": 7, "realtor": 8, "redfin": 9,
    }
    by_key: dict[str, Listing] = {}
    for l in listings:
        key = l.dedupe_key()
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = l
            continue
        if source_priority.get(l.source, 99) < source_priority.get(existing.source, 99):
            by_key[key] = l
    return list(by_key.values())


def scrape_full(fc: Firecrawl, cfg: dict) -> tuple[list[Listing], ScrapeStats]:
    """Monthly sweep — all portals via search + extract."""
    stats = ScrapeStats(mode="full")
    address = cfg["building"]["address"]

    urls = _discover_urls_full(fc, cfg, stats)
    listings: list[Listing] = []
    batch_size = 10
    for i in range(0, len(urls), batch_size):
        batch = urls[i : i + batch_size]
        data = _extract_with_retry(fc, batch)
        stats.extract_calls += 1
        if not data:
            stats.errors.append(f"extract returned nothing for batch: {batch}")
            continue
        for raw in data.get("listings") or []:
            if isinstance(raw, dict):
                l = _normalize_extracted(raw, source_url=batch[0], address=address)
                if l:
                    listings.append(l)

    log.info("extracted %d raw listings (pre-dedupe)", len(listings))
    listings = dedupe_cross_source(listings)
    log.info("after cross-source dedupe: %d listings", len(listings))

    for l in listings:
        stats.per_source_count[l.source] = stats.per_source_count.get(l.source, 0) + 1
    return listings, stats


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scrape_all(fc: Firecrawl, cfg: dict, *, mode: str = "minimal") -> tuple[list[Listing], ScrapeStats]:
    """Dispatch to the requested mode. `mode` is 'minimal' (default) or 'full'."""
    if mode == "full":
        return scrape_full(fc, cfg)
    return scrape_minimal(fc, cfg)
