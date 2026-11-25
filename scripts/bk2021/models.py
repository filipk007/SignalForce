"""Configuration and sanitized public BK2021 records."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from scripts.registry.domains import normalize_domain

SOURCE_TYPE = "bk2021"
BASE_URL = "https://bazakonkurencyjnosci.funduszeeuropejskie.gov.pl"
WARSAW = ZoneInfo("Europe/Warsaw")
PUBLISHED = "PUBLISHED"

_CONTACT_LINE = re.compile(
    r"\b(?:osoba do kontaktu|dane kontaktowe|kontakt\s*:|tel(?:efon)?\s*[:.]|"
    r"e-?mail\s*:|adres\s+(?:e-?mail|poczty elektronicznej))",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+48[\s.-]*)?(?:\d[\s.-]*){8}\d(?!\w)")

FeedKey = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)]
PositiveId = Annotated[int, Field(strict=True, gt=0)]


class FeedConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FeedKey
    queries: tuple[Query, ...] = Field(min_length=1, max_length=20)
    interval_hours: int = Field(ge=1, le=8760, strict=True)
    lookback_days: int = Field(default=14, ge=1, le=3660, strict=True)
    page_size: int = Field(default=20, ge=1, le=50, strict=True)
    max_pages: int = Field(default=60, ge=1, le=1000, strict=True)
    max_details: int = Field(default=750, ge=1, le=5000, strict=True)
    min_capacity_kwh: int = Field(default=100, ge=1, le=1_000_000, strict=True)

    @field_validator("queries")
    @classmethod
    def unique_queries(cls, queries: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(queries))


def normalize_nip(value: str) -> str | None:
    nip = re.sub(r"[\s-]", "", value)
    if not re.fullmatch(r"[0-9]{10}", nip):
        return None
    checksum = sum(int(digit) * weight for digit, weight in zip(nip, (6, 5, 7, 2, 3, 4, 5, 6, 7)))
    return nip if checksum % 11 == int(nip[-1]) else None


class VerifiedDomain(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nip: str
    domain: str
    evidence_url: AnyHttpUrl

    @field_validator("nip")
    @classmethod
    def valid_nip(cls, value: str) -> str:
        nip = normalize_nip(value)
        if nip is None:
            raise ValueError("invalid Polish NIP checksum")
        return nip

    @field_validator("domain")
    @classmethod
    def bare_domain(cls, value: str) -> str:
        return normalize_domain(value)


class TenantConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    feeds: tuple[FeedConfig, ...] = Field(min_length=1)
    verified_domains: tuple[VerifiedDomain, ...] = ()

    @model_validator(mode="after")
    def unique_keys(self) -> TenantConfig:
        if len({feed.key for feed in self.feeds}) != len(self.feeds):
            raise ValueError("duplicate feed key")
        if len({binding.nip for binding in self.verified_domains}) != len(self.verified_domains):
            raise ValueError("duplicate mapped NIP")
        return self

    @classmethod
    def load(cls, path: Path) -> TenantConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def source_text(value: str) -> str:
    """Keep the procurement scope while removing common inline contact information."""
    lines = (_CONTACT_LINE.split(line, maxsplit=1)[0].strip() for line in value.splitlines())
    return "\n".join(
        _PHONE.sub("[redacted phone]", _EMAIL.sub("[redacted email]", line))
        for line in lines
        if line
    )


class OrderItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    category: str | None = None


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    items: tuple[OrderItem, ...]


class Announcement(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: PositiveId
    version_id: PositiveId
    first_published: date
    version_published: date
    status: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    deadline: datetime | None
    title: str
    buyer: str
    nip: str | None
    orders: tuple[Order, ...]

    @computed_field
    @property
    def url(self) -> str:
        return f"{BASE_URL}/ogloszenia/{self.source_id}"

    @classmethod
    def from_source(cls, source_id: int, data: dict[str, Any]) -> Announcement:
        try:
            version = data["advertisement"]
            stable = version["advertisement"]
            if type(stable["id"]) is not int or stable["id"] != source_id:
                raise ValueError("announcement ID does not match endpoint")
            buyer = version["advertiser_address_details"] or {}
            nip = buyer.get("identification_number")
            raw_deadline = version["submission_deadline"]
            deadline = datetime.fromisoformat(raw_deadline) if raw_deadline else None
            if deadline is not None:
                deadline = (
                    deadline.replace(tzinfo=WARSAW)
                    if deadline.tzinfo is None
                    else deadline.astimezone(WARSAW)
                )
            status = stable["status"]["label"]
            if status == PUBLISHED and deadline is None:
                raise ValueError("published announcement has no deadline")
            return cls(
                source_id=source_id,
                version_id=version["id"],
                first_published=date.fromisoformat(stable["publication_date"]),
                version_published=date.fromisoformat(version["publication_date"]),
                status=status,
                deadline=deadline,
                title=source_text(version["title"] or ""),
                buyer=(buyer.get("name") or "").strip(),
                nip=(normalize_nip(str(nip)) or str(nip).strip()[:64])
                if nip and buyer.get("identification_number_type") == "nip"
                else None,
                orders=tuple(
                    Order(
                        title=source_text(order["title"] or ""),
                        items=tuple(
                            OrderItem(
                                description=source_text(item["description"] or ""),
                                category=(item.get("category") or {}).get("name"),
                            )
                            for item in order["order_items"]
                        ),
                    )
                    for order in version["orders"]
                ),
            )
        except (KeyError, TypeError, AttributeError, OverflowError) as error:
            raise ValueError("malformed announcement details") from error


class SignalPayload(Announcement):
    qualified: bool
    evidence: tuple[str, ...]
    evidence_version_id: int
    mapping_evidence_url: str | None = None

    @property
    def warsaw_deadline(self) -> datetime | None:
        return self.deadline.astimezone(WARSAW) if self.deadline else None

    def intent_status(self, now: datetime) -> str:
        if self.status != PUBLISHED:
            return "Inactive"
        if not self.qualified:
            return "Out of scope"
        if self.deadline is None or self.deadline <= now:
            return "Expired"
        return "Active"
