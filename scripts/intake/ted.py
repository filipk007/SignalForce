"""TED road transport: NicheDB discovery, verification against the original eForms XML.

NicheDB only lists candidates (it has no CPV, deadline or procedure ID). Scope, dates,
status and corrections come from TED. TED is the logical source whichever API found it.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scripts.intake.http import Sleep, SourceError, fetch
from scripts.intake.models import (
    TED_TYPE,
    Evidence,
    ExternalSignal,
    TedSource,
    public_text,
    registry_id,
)

NICHEDB_URL = "https://nichedb.dev/api/v1/items"
SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice"
RULE_VERSION = "ted-road-transport-v1"

_DECIDED = frozenset({"selec-w", "clos-nw"})
_ON_HOLD = frozenset({"cancel-intent", "susp-review"})
_PROCEDURE = re.compile(r"[0-9a-zA-Z-]{1,64}")
_NUMBER = re.compile(r"[0-9]+-[0-9]{4}")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Lot(_Frozen):
    id: str
    name: str
    cpvs: tuple[str, ...]
    countries: tuple[str, ...]
    regions: tuple[str, ...]
    deadline: datetime | None


class Notice(_Frozen):
    number: str
    notice_id: str
    version: str
    procedure_id: str
    kind: str
    published_on: date
    changed_ref: str | None
    change_reasons: tuple[str, ...]
    title: str
    buyer: str
    buyer_ref: str | None
    buyer_website: str | None
    lots: tuple[Lot, ...]
    results: tuple[tuple[str, str], ...]

    @property
    def order(self) -> tuple[date, int]:
        return self.published_on, int(self.number.split("-")[0])

    @property
    def refs(self) -> frozenset[str]:
        """The identifiers a later change notice may name."""
        return frozenset({self.number, f"{self.notice_id}-{self.version}"})

    @property
    def xml_url(self) -> str:
        return f"{NOTICE_URL}/{self.number}/xml"


class TedBatch(_Frozen):
    signals: tuple[ExternalSignal, ...]
    pages: int
    discovered: int
    notices: int


class _NicheData(_Frozen):
    noticeType: str = ""


class _NicheItem(_Frozen):
    id: int = Field(gt=0)
    external_id: str
    data: _NicheData


class _NichePage(_Frozen):
    items: tuple[_NicheItem, ...]


class _Hit(_Frozen):
    number: str = Field(alias="publication-number")


class _SearchResult(_Frozen):
    notices: tuple[_Hit, ...]
    total: int = Field(alias="totalNoticeCount")


def _path(*names: str) -> str:
    return "/".join(f"{{*}}{name}" for name in names)


def _text(element: ET.Element, *names: str) -> str:
    return (element.findtext(_path(*names)) or "").strip()


def _texts(element: ET.Element, *names: str) -> tuple[str, ...]:
    found = ((node.text or "").strip() for node in element.findall(_path(*names)))
    return tuple(dict.fromkeys(value for value in found if value))


def _publication_number(value: str) -> str:
    return value.strip().lstrip("0")


def _deadline(lot: ET.Element) -> datetime | None:
    for period in ("TenderSubmissionDeadlinePeriod", "ParticipationRequestReceptionPeriod"):
        day = _text(lot, "TenderingProcess", period, "EndDate")
        if day:
            clock = _text(lot, "TenderingProcess", period, "EndTime") or "00:00:00"
            offset = day[10:] if len(clock) == 8 else ""
            value = datetime.fromisoformat(f"{day[:10]}T{clock}{offset}")
            if value.tzinfo is None:
                raise ValueError("deadline has no timezone")
            return value
    return None


def parse_notice(content: bytes) -> Notice:
    """Read only the procurement facts. Contact details are never extracted."""
    if b"<!DOCTYPE" in content[:2048]:
        raise SourceError("TED notice declares a DTD")
    try:
        root = ET.fromstring(content)
        extension = root.find(".//{*}EformsExtension")
        if extension is None:
            raise ValueError("not an eForms notice")
        project = ("ProcurementProject",)
        address = ("RealizedLocation", "Address")
        shared = root.find(_path(*project))
        lots = []
        for lot in root.findall(_path("ProcurementProjectLot")):
            own = lot.find(_path(*project))
            if own is None:
                raise ValueError("lot has no procurement project")
            place = own if own.find(_path(*address)) is not None else shared
            codes = ("ItemClassificationCode",)
            lots.append(
                Lot(
                    id=_text(lot, "ID"),
                    name=public_text(_text(own, "Name")),
                    cpvs=_texts(own, "MainCommodityClassification", *codes)
                    + _texts(own, "AdditionalCommodityClassification", *codes),
                    countries=_texts(place, *address, "Country", "IdentificationCode")
                    if place is not None
                    else (),
                    regions=_texts(place, *address, "CountrySubentityCode")
                    if place is not None
                    else (),
                    deadline=_deadline(lot),
                )
            )
        buyer_key = _text(root, "ContractingParty", "Party", "PartyIdentification", "ID")
        company = next(
            (
                node
                for node in extension.findall(_path("Organizations", "Organization", "Company"))
                if _text(node, "PartyIdentification", "ID") == buyer_key
            ),
            None,
        )
        if company is None:
            raise ValueError("buyer organisation is missing")
        company_id = _text(company, "PartyLegalEntity", "CompanyID")
        changes = ("Changes",)
        results = tuple(
            (_text(result, "TenderLot", "ID"), _text(result, "TenderResultCode"))
            for result in extension.findall(_path("NoticeResult", "LotResult"))
        )
        changed = _text(extension, *changes, "ChangedNoticeIdentifier")
        notice = Notice(
            number=_publication_number(_text(extension, "Publication", "NoticePublicationID")),
            notice_id=_text(root, "ID"),
            version=_text(root, "VersionID"),
            procedure_id=_text(root, "ContractFolderID"),
            kind=_text(root, "NoticeTypeCode"),
            published_on=date.fromisoformat(
                _text(extension, "Publication", "PublicationDate")[:10]
            ),
            changed_ref=_publication_number(changed) or None,
            change_reasons=_texts(extension, *changes, "ChangeReason", "ReasonCode"),
            title=public_text(_text(root, *project, "Name")),
            buyer=_text(company, "PartyName", "Name"),
            buyer_ref=registry_id(company_id) or None,
            buyer_website=_text(company, "WebsiteURI") or None,
            lots=tuple(lots),
            results=results,
        )
        if not _NUMBER.fullmatch(notice.number) or not _PROCEDURE.fullmatch(notice.procedure_id):
            raise ValueError("notice or procedure identifier is missing")
        return notice
    except (ET.ParseError, ValueError) as error:
        raise SourceError(f"invalid TED notice XML: {error}") from error


def _in_scope(notice: Notice, source: TedSource) -> tuple[Lot, ...]:
    return tuple(
        lot
        for lot in notice.lots
        if "POL" in lot.countries and any(c.startswith(source.cpv_prefix) for c in lot.cpvs)
    )


def _signal(
    native_id: str,
    notice: Notice,
    first: Notice,
    lots: tuple[Lot, ...],
    status: str,
    reasons: tuple[str, ...],
    source: TedSource,
    now: datetime,
    *,
    deadline: datetime | None,
    refs: frozenset[str],
    updates: tuple[Notice, ...] = (),
) -> ExternalSignal:
    latest = updates[-1] if updates else notice
    evidence = [
        Evidence(
            url=notice.xml_url,
            locator=f"ProcurementProjectLot {lot.id}: CPV, RealizedLocation, deadline",
            excerpt=" · ".join(
                (
                    lot.name,
                    "CPV " + ", ".join(lot.cpvs),
                    ", ".join(lot.regions or lot.countries),
                    "termin " + (lot.deadline.isoformat() if lot.deadline else "brak"),
                )
            ),
            version=notice.number,
        )
        for lot in lots[:5]
    ]
    evidence += [
        Evidence(
            url=other.xml_url,
            locator="LotResult" if other.results else "NoticeTypeCode / ChangeReason",
            excerpt=", ".join(
                [other.kind, *other.change_reasons, *(" ".join(pair) for pair in other.results)]
            ),
            version=other.number,
        )
        for other in updates
        if other is not notice
    ]
    cpvs = sorted({c for lot in lots for c in lot.cpvs if c.startswith(source.cpv_prefix)})
    why_now = (
        f"Otwarty nabór ofert na transport drogowy w Polsce (CPV {', '.join(cpvs)}), "
        f"termin {deadline:%Y-%m-%d %H:%M %z}: {lots[0].name}"
        if status == "open" and deadline
        else f"Postępowanie na transport drogowy: {notice.title}"
    )
    return ExternalSignal(
        source_instance_id=source.instance,
        transport="nichedb+ted",
        native_id=native_id,
        signal_type=TED_TYPE,
        rule_version=RULE_VERSION,
        source_version=latest.number,
        source_status=status,
        source_review_reasons=reasons,
        status=status,
        review_reasons=reasons,
        title=notice.title,
        company_name=notice.buyer,
        company_role="buyer",
        company_ref=notice.buyer_ref,
        url=f"{NOTICE_URL}/-/detail/{notice.number}",
        published_on=first.published_on,
        updated_on=latest.published_on,
        observed_at=now,
        deadline=deadline,
        expires_at=deadline,
        evidence=tuple(evidence),
        why_now=why_now,
        source_payload={
            "procedure_id": notice.procedure_id,
            "notice_id": notice.notice_id,
            "notice_type": notice.kind,
            "buyer_website": notice.buyer_website or "",
            "cpv": cpvs,
            "lots": [lot.id for lot in lots],
        },
        refs=tuple(sorted(refs)),
    )


def _procedure_signals(
    chain: list[Notice], source: TedSource, now: datetime
) -> Iterable[ExternalSignal]:
    competitions = [n for n in chain if n.kind.startswith("cn-")]
    base = next((n for n in competitions if n.changed_ref is None), None)
    current = base
    refs: frozenset[str] = base.refs if base else frozenset()
    updates: list[Notice] = []
    closed: set[str] = set()
    cancelled = False
    hold: str | None = None
    strays = []
    for notice in chain:
        if notice is base:
            continue
        if notice.kind.startswith("cn-"):
            if current is None or notice.changed_ref not in refs:
                strays.append(notice)
                continue
            current = notice
            cancelled = "cancel" in notice.change_reasons
            hold = (
                "zapowiedź unieważnienia lub zawieszenie"
                if _ON_HOLD.intersection(notice.change_reasons)
                else None
            )
        elif notice.kind.startswith("can-") and current is not None:
            if notice.kind == "can-modif":
                closed.update(lot.id for lot in current.lots)
            elif not notice.results:
                hold = "ogłoszenie o wyniku bez rozstrzygnięć części"
            closed.update(lot for lot, code in notice.results if code in _DECIDED)
        else:
            continue
        refs |= notice.refs
        updates.append(notice)

    if base is not None and current is not None:
        accepted = [base, *(n for n in updates if n.kind.startswith("cn-"))]
        if any(_in_scope(n, source) for n in accepted):
            lots = _in_scope(current, source)
            open_lots = tuple(lot for lot in lots if lot.id not in closed)
            deadlines = [lot.deadline for lot in open_lots if lot.deadline]
            reasons: tuple[str, ...] = ()
            if cancelled:
                status = "cancelled"
            elif not lots:
                status = "review"
                reasons = ("po korekcie brak transportu drogowego w Polsce",)
            elif not open_lots:
                status = "closed"
            elif hold or not deadlines:
                status = "review"
                reasons = (hold or "brak terminu w oryginale TED",)
            else:
                status = "open"
            if status in ("cancelled", "closed"):
                deadlines = [lot.deadline for lot in lots if lot.deadline]
            yield _signal(
                base.procedure_id,
                current,
                base,
                lots,
                status,
                reasons,
                source,
                now,
                deadline=max(deadlines, default=None),
                refs=refs,
                updates=tuple(updates),
            )
    for notice in strays:
        lots = _in_scope(notice, source)
        if lots:
            yield _signal(
                f"pub:{notice.number}",
                notice,
                notice,
                lots,
                "review",
                ("korekta bez potwierdzonego odwołania do znanego ogłoszenia",),
                source,
                now,
                deadline=max((lot.deadline for lot in lots if lot.deadline), default=None),
                refs=notice.refs,
            )


def fold(notices: Iterable[Notice], source: TedSource, now: datetime) -> tuple[ExternalSignal, ...]:
    """One signal per procedure. Changes apply only when they name a notice already in it."""
    chains: dict[str, list[Notice]] = {}
    for notice in sorted(notices, key=lambda n: n.order):
        chains.setdefault(notice.procedure_id, []).append(notice)
    return tuple(
        signal for chain in chains.values() for signal in _procedure_signals(chain, source, now)
    )


class Ted:
    def __init__(
        self, client: httpx.AsyncClient, source: TedSource, sleep: Sleep = asyncio.sleep
    ) -> None:
        self.client = client
        self.source = source
        self.sleep = sleep
        self.cache: dict[str, Notice] = {}

    async def discover(self, since: datetime) -> tuple[tuple[str, ...], int]:
        """Walk by ID: the updated-time order has no usable cursor and ties are common."""
        found: list[str] = []
        after = 0
        for page in range(1, self.source.max_pages + 1):
            params: dict[str, str | int] = {
                "source": "eu-tenders",
                "tags": "pl",
                "since": since.isoformat(),
                "sort": "id",
                "order": "asc",
                "limit": self.source.page_size,
            }
            if after:
                params["after"] = after
            response = await fetch(self.client, NICHEDB_URL, params=params, sleep=self.sleep)
            try:
                items = _NichePage.model_validate_json(response.content).items
            except ValueError as error:
                raise SourceError(f"NicheDB invalid page {page}: {error}") from error
            for item in items:
                if item.id <= after:
                    raise SourceError(f"NicheDB ID cursor did not advance on page {page}")
                after = item.id
                number = item.external_id.removeprefix("ted-")
                if item.data.noticeType.startswith("cn-") and _NUMBER.fullmatch(number):
                    found.append(number)
            if len(items) < self.source.page_size:
                return tuple(dict.fromkeys(found)), page
        raise SourceError(f"NicheDB page limit ({self.source.max_pages}) reached")

    async def notice(self, number: str) -> Notice:
        if number not in self.cache:
            if len(self.cache) >= self.source.max_notices:
                raise SourceError(f"TED notice limit ({self.source.max_notices}) reached")
            if self.cache:
                # The public XML endpoint throttles bursts.
                await self.sleep(self.source.request_pause_seconds)
            url = f"{NOTICE_URL}/{number}/xml"
            response = await fetch(self.client, url, sleep=self.sleep)
            notice = parse_notice(response.content)
            if notice.number != number:
                raise SourceError(f"{url}: returned notice {notice.number}")
            self.cache[number] = notice
        return self.cache[number]

    async def procedure_numbers(self, procedure_id: str) -> tuple[str, ...]:
        response = await fetch(
            self.client,
            SEARCH_URL,
            method="POST",
            json={
                "query": f"procedure-identifier={procedure_id}",
                "fields": ["publication-number"],
                "limit": 100,
                "scope": "ALL",
            },
            sleep=self.sleep,
        )
        try:
            result = _SearchResult.model_validate_json(response.content)
        except ValueError as error:
            raise SourceError(f"{SEARCH_URL}: invalid result: {error}") from error
        if result.total != len(result.notices) or not result.notices:
            raise SourceError(
                f"{SEARCH_URL}: procedure {procedure_id} listed "
                f"{len(result.notices)} of {result.total} notices"
            )
        return tuple(hit.number for hit in result.notices)

    async def collect(self, since: datetime, tracked: frozenset[str], now: datetime) -> TedBatch:
        """Discover candidates, then rebuild every relevant procedure from its TED notices."""
        discovered, pages = await self.discover(since)
        procedures = set(tracked)
        for number in dict.fromkeys((*self.source.publications, *discovered)):
            notice = await self.notice(number)
            if _in_scope(notice, self.source):
                procedures.add(notice.procedure_id)
        chains: list[Notice] = []
        for procedure_id in sorted(procedures):
            listed = set(await self.procedure_numbers(procedure_id))
            # The search index can trail publication, so keep what was already read.
            listed.update(n.number for n in self.cache.values() if n.procedure_id == procedure_id)
            for number in sorted(listed):
                chains.append(await self.notice(number))
        return TedBatch(
            signals=fold(chains, self.source, now),
            pages=pages,
            discovered=len(discovered),
            notices=len(self.cache),
        )
