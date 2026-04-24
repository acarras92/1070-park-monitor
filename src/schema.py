from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Listing(BaseModel):
    source: str
    listing_url: str
    address: str
    unit: Optional[str] = None
    price: Optional[int] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    square_feet: Optional[int] = None
    maintenance: Optional[int] = None
    broker: Optional[str] = None
    status: str = "active"
    listed_date: Optional[str] = None
    image_url: Optional[str] = None
    scraped_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("source")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("status")
    @classmethod
    def _norm_status(cls, v: str) -> str:
        if not v:
            return "active"
        s = v.lower().replace("-", "_").replace(" ", "_").strip()
        mapping = {
            "in_contract": "in_contract",
            "contract_signed": "in_contract",
            "contract": "in_contract",
            "pending": "in_contract",
            "sold": "sold",
            "closed": "sold",
            "active": "active",
            "for_sale": "active",
            "available": "active",
            "off_market": "off_market",
            "delisted": "off_market",
            "withdrawn": "off_market",
        }
        return mapping.get(s, s)

    def dedupe_key(self) -> str:
        """Key used to collapse cross-source duplicates of the same unit."""
        unit = (self.unit or "").upper().replace(" ", "").replace("#", "").replace("APT", "")
        if unit and self.price:
            return f"unit={unit}|price={self.price}"
        if unit:
            return f"unit={unit}"
        return self.listing_url
