"""Operator configuration and the shared record for TED and RWDZ signals."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from scripts.registry.domains import normalize_domain

TED_TYPE = "ted_road_transport"
RWDZ_TYPE = "rwdz_plant_expansion"
INTAKE_TYPES = (TED_TYPE, RWDZ_TYPE)

Instance = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,24}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)]


_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
# Nine digits written as a phone number. Dots and slashes belong to case numbers.
_PHONE = re.compile(r"(?<![\w./])(?:\+48[ -]?)?\d{3}[ -]?\d{3}[ -]?\d{3}(?![\w/]|\.\w)")


def public_text(value: str) -> str:
    """Remove inline contact details from a published free-text description."""
    cleaned = _PHONE.sub("[redacted phone]", _EMAIL.sub("[redacted email]", value))
    return " ".join(cleaned.split())


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def external_key(instance: str, native_id: str) -> str:
    """Stable per-tenant key of a logical source entry, within SignalEvent's 64 characters."""
    key = f"{instance}:{native_id}"
    if len(key) <= 64:
        return key
    return f"{instance}:{hashlib.sha256(native_id.encode()).hexdigest()[:32]}"


def registry_id(value: str) -> str:
    """A NIP or REGON as written by the source, reduced to its characters."""
    return re.sub(r"^(NIP|REGON|KRS|PL)|[^0-9A-Z]", "", value.upper().strip())[:32]


class SourceConfig(Frozen):
    instance: Instance
    # How often the publisher releases data. Informative, shown next to health.
    publisher_schedule: Text
    # How often SignalForce imports it.
    interval_hours: int = Field(ge=1, le=8760, strict=True)


class TedSource(SourceConfig):
    lookback_days: int = Field(default=3, ge=1, le=90, strict=True)
    overlap_hours: int = Field(default=24, ge=1, le=240, strict=True)
    page_size: int = Field(default=200, ge=1, le=200, strict=True)
    max_pages: int = Field(default=10, ge=1, le=100, strict=True)
    max_notices: int = Field(default=200, ge=1, le=2000, strict=True)
    cpv_prefix: Annotated[str, StringConstraints(pattern=r"^[0-9]{2,8}$")] = "601"
    # Explicit publication numbers to verify even when discovery does not list them.
    publications: tuple[Annotated[str, StringConstraints(pattern=r"^[0-9]+-[0-9]{4}$")], ...] = ()
    request_pause_seconds: float = Field(default=1.0, ge=0, le=60)


class RwdzSource(SourceConfig):
    url: AnyHttpUrl = AnyHttpUrl(
        "https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_wielkopolskie.zip"
    )
    lookback_days: int = Field(default=30, ge=1, le=366, strict=True)
    # A hypothesis about how long a decision stays actionable.
    intent_days: int = Field(default=90, ge=1, le=730, strict=True)
    max_download_mb: int = Field(default=100, ge=1, le=500, strict=True)
    max_uncompressed_mb: int = Field(default=600, ge=1, le=2000, strict=True)


class VerifiedCompany(Frozen):
    """Operator-checked link from an official registry number to a company domain."""

    registry_id: str
    domain: str
    evidence_url: AnyHttpUrl

    @field_validator("registry_id")
    @classmethod
    def clean_id(cls, value: str) -> str:
        return registry_id(value)

    @field_validator("domain")
    @classmethod
    def bare_domain(cls, value: str) -> str:
        return normalize_domain(value)


class DecisionCheck(Frozen):
    """Operator check of one RWDZ case: the file carries no decision outcome or company ID."""

    case: Text
    decision: Literal["positive", "negative"]
    decision_evidence_url: AnyHttpUrl
    domain: str
    domain_evidence_url: AnyHttpUrl

    @field_validator("domain")
    @classmethod
    def bare_domain(cls, value: str) -> str:
        return normalize_domain(value)


class IntakeConfig(Frozen):
    tenant: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    ted: TedSource | None = None
    rwdz: RwdzSource | None = None
    companies: tuple[VerifiedCompany, ...] = ()
    decisions: tuple[DecisionCheck, ...] = ()

    @model_validator(mode="after")
    def unique_entries(self) -> IntakeConfig:
        if self.ted is None and self.rwdz is None:
            raise ValueError("configure at least one source")
        if self.ted and self.rwdz and self.ted.instance == self.rwdz.instance:
            raise ValueError("duplicate source instance")
        if len({entry.registry_id for entry in self.companies}) != len(self.companies):
            raise ValueError("duplicate company registry ID")
        if len({entry.case for entry in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate decision case")
        return self

    @classmethod
    def load(cls, path: Path) -> IntakeConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Evidence(Frozen):
    url: str
    locator: str
    excerpt: str
    version: str


class ExternalSignal(Frozen):
    """The stored payload of one TED procedure or RWDZ case."""

    source_instance_id: str
    transport: str
    native_id: str
    signal_type: str
    rule_version: str
    source_version: str
    # What the source itself says. "review" never counts as current intent.
    source_status: Literal["open", "closed", "cancelled", "review"]
    source_review_reasons: tuple[str, ...] = ()
    # After operator checks are applied.
    status: Literal["open", "closed", "cancelled", "review"]
    review_reasons: tuple[str, ...] = ()
    title: str
    company_name: str
    company_role: Literal["buyer", "investor"]
    company_ref: str | None = None
    url: str
    occurred_on: date | None = None
    published_on: date | None = None
    updated_on: date | None = None
    source_updated_at: datetime | None = None
    observed_at: datetime
    deadline: datetime | None = None
    expires_at: datetime | None = None
    evidence: tuple[Evidence, ...]
    why_now: str
    mapping_evidence_url: str | None = None
    decision_evidence_url: str | None = None
    source_payload: dict[str, str | list[str]] = Field(default_factory=dict)
    # Publication numbers and notice identifiers already folded into this entry.
    refs: tuple[str, ...] = ()

    @property
    def external_id(self) -> str:
        return external_key(self.source_instance_id, self.native_id)

    @property
    def signal_date(self) -> date | None:
        return self.occurred_on or self.published_on

    def intent_status(self, now: datetime) -> str:
        if self.status != "open":
            return self.status.capitalize()
        if self.expires_at is None or self.expires_at <= now:
            return "Expired"
        return "Active"
