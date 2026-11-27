"""The nightly GUNB RWDZ ZIP of building permit applications and decisions.

The file names no decision outcome and no investor registry number, so every entry
waits for an operator check. Only organisation investor names are retained.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
import zlib
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, ConfigDict

from scripts.bk2021.models import WARSAW
from scripts.intake.http import SourceError
from scripts.intake.models import (
    RWDZ_TYPE,
    Evidence,
    ExternalSignal,
    RwdzSource,
    public_text,
)

RULE_VERSION = "rwdz-plant-expansion-v1"
UNVERIFIED = "brak potwierdzenia rodzaju decyzji i domeny inwestora"

# The one accepted schema. GUNB's page announces '#', the published file uses ';'.
COLUMNS = (
    "numer_urzad",
    "numer_gunb",
    "nazwa_organu",
    "adres_organu",
    "data_wplywu_wniosku",
    "numer_decyzji_urzedu",
    "data_wydania_decyzji",
    "nazwa_inwestor",
    "wojewodztwo",
    "miasto",
    "terc",
    "cecha",
    "cecha",
    "ulica",
    "ulica_dalej",
    "nr_domu",
    "rodzaj_inwestycji",
    "kategoria",
    "nazwa_zamierzenia_bud",
    "nazwa_zam_budowlanego",
    "kubatura",
    "projektant_nazwisko",
    "projektant_imie",
    "projektant_numer_uprawnien",
    "jednosta_numer_ew",
    "obreb_numer",
    "numer_dzialki",
    "numer_arkusza_dzialki",
    "jednostka_stara_numeracja_z_wniosku",
    "stara_numeracja_obreb_z_wnioskiu",
    "stara_numeracja_dzialka_z_wniosku",
)
# Designer, address and parcel columns are never read into a record.
_KEPT = (
    "numer_urzad",
    "numer_gunb",
    "nazwa_organu",
    "data_wplywu_wniosku",
    "numer_decyzji_urzedu",
    "data_wydania_decyzji",
    "nazwa_inwestor",
    "wojewodztwo",
    "miasto",
    "kategoria",
    "nazwa_zamierzenia_bud",
    "nazwa_zam_budowlanego",
)
_INDEX = {name: COLUMNS.index(name) for name in _KEPT}
_LEGAL_ENTITY = re.compile(
    r"z\s*ograniczon|\bsp(?:\.|ółka)?\s*z\s*o\.?\s*o\b|spółka\s+akcyjna|(?<!\w)S\.\s?A\.|"
    r"\bspółdzielnia\b|\bP\.?S\.?A\.(?!\w)",
    re.IGNORECASE,
)
_PLANT = re.compile(r"produkcyj|przemysłow|zakład|fabryk", re.IGNORECASE)
_AMENDS = re.compile(
    r"zmian\w*\s+(?:\w+\s+){0,2}(?:pozwoleni|decyzj)|zamienn|przeniesieni", re.IGNORECASE
)


class RwdzRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: int
    cases_in_window: int
    signals: tuple[ExternalSignal, ...]
    # Tracked cases with no row in this file.
    missing: tuple[str, ...]
    sha256: str


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _qualifies(record: dict[str, str]) -> bool:
    description = record["nazwa_zam_budowlanego"]
    return (
        record["nazwa_zamierzenia_bud"].lower().startswith("rozbudowa istniej")
        and record["kategoria"].strip() == "XVIII"
        and "rozbudow" in description.lower()
        and _PLANT.search(description) is not None
    )


def _signal(
    case: str,
    rows: list[tuple[int, dict[str, str]]],
    source: RwdzSource,
    now: datetime,
    sha256: str,
    updated: datetime | None,
) -> ExternalSignal:
    record = next((r for _, r in rows if _qualifies(r)), rows[0][1])
    investor = " ".join(record["nazwa_inwestor"].split())
    investor = investor if _LEGAL_ENTITY.search(investor) else ""
    description = public_text(record["nazwa_zam_budowlanego"])
    decided = _day(record["data_wydania_decyzji"])
    blocking = []
    if not _qualifies(record):
        blocking.append("wpis po korekcie nie spełnia reguły rozbudowy zakładu")
    facts = ("numer_decyzji_urzedu", "data_wydania_decyzji", "nazwa_inwestor")
    if len({tuple(r[name] for name in facts) for _, r in rows}) > 1:
        blocking.append("wiersze jednej sprawy podają różne decyzje lub inwestora")
    if decided is None or not record["numer_decyzji_urzedu"].strip():
        blocking.append("brak numeru lub daty decyzji")
    notes = []
    if not investor:
        notes.append("inwestor nieujawniony lub nie jest osobą prawną")
    if _AMENDS.search(description):
        notes.append("opis wskazuje na zmianę wcześniejszej decyzji")
    lines = ", ".join(str(line) for line, _ in rows[:10])
    number = record["numer_decyzji_urzedu"].strip()
    expires = (
        datetime.combine(decided + timedelta(days=source.intent_days), time(), WARSAW)
        if decided
        else None
    )
    return ExternalSignal(
        source_instance_id=source.instance,
        transport="gunb-zip",
        native_id=case,
        signal_type=RWDZ_TYPE,
        rule_version=RULE_VERSION,
        source_version=sha256,
        source_status="review" if blocking else "open",
        source_review_reasons=tuple(blocking + notes),
        status="review",
        review_reasons=tuple(blocking + notes) + (() if blocking else (UNVERIFIED,)),
        title=description,
        company_name=investor,
        company_role="investor",
        url=str(source.url),
        occurred_on=decided,
        source_updated_at=updated,
        observed_at=now,
        expires_at=expires,
        evidence=(
            Evidence(
                url=str(source.url),
                locator=f"CSV numer_urzad/numer_gunb = {case}; wiersze {lines}",
                excerpt=f"{record['nazwa_zamierzenia_bud']} · kategoria {record['kategoria']} · "
                f"decyzja {number} z {decided} · {description}",
                version=f"sha256:{sha256[:16]}",
            ),
        ),
        why_now=f"Decyzja {number} z {decided} dotycząca rozbudowy zakładu w miejscowości "
        f"{record['miasto']}; hipoteza ważności do {expires:%Y-%m-%d}: {description}"
        if expires
        else description,
        source_payload={name: record[name] for name in _KEPT if name != "nazwa_inwestor"}
        | {"nazwa_inwestor": investor, "nazwa_zam_budowlanego": description},
    )


def read_zip(
    content: bytes,
    source: RwdzSource,
    now: datetime,
    *,
    tracked: frozenset[str],
    updated: datetime | None = None,
) -> RwdzRead:
    """Validate the whole file, then qualify only recent and already tracked cases."""
    today = now.astimezone(WARSAW).date()
    first = today - timedelta(days=source.lookback_days)
    cases: dict[str, list[tuple[int, dict[str, str]]]] = {}
    recent: set[str] = set()
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) != 1 or not members[0].filename.endswith(".csv"):
                raise ValueError("expected exactly one CSV member")
            if members[0].file_size > source.max_uncompressed_mb * 1024 * 1024:
                raise ValueError(f"CSV is {members[0].file_size} bytes, over the limit")
            # Reading to the end also verifies the member's CRC.
            with archive.open(members[0]) as member:
                text = io.TextIOWrapper(member, encoding="utf-8-sig", newline="")
                reader = csv.reader(text, delimiter=";", strict=True)
                if tuple(next(reader, ())) != COLUMNS:
                    raise ValueError("header differs from the declared ';' schema")
                for line, values in enumerate(reader, start=2):
                    if len(values) != len(COLUMNS):
                        raise ValueError(f"line {line} has {len(values)} columns")
                    count += 1
                    case = f"{values[_INDEX['numer_urzad']]}/{values[_INDEX['numer_gunb']]}"
                    decided = _day(values[_INDEX["data_wydania_decyzji"]])
                    in_window = decided is not None and first <= decided <= today
                    if in_window:
                        recent.add(case)
                    if in_window or case in tracked:
                        record = {name: values[index] for name, index in _INDEX.items()}
                        cases.setdefault(case, []).append((line, record))
    except (zipfile.BadZipFile, zlib.error, csv.Error, ValueError, EOFError, OSError) as error:
        raise SourceError(f"{source.url}: invalid file: {error}") from error
    if count == 0:
        raise SourceError(f"{source.url}: the file has no rows")
    sha256 = hashlib.sha256(content).hexdigest()
    # ponytail: a tracked case is re-read only from rows of its own key; a renamed
    # case surfaces as missing, which is the review outcome wanted here.
    signals = tuple(
        _signal(case, rows, source, now, sha256, updated)
        for case, rows in sorted(cases.items())
        if case in tracked or any(_qualifies(record) for _, record in rows)
    )
    return RwdzRead(
        rows=count,
        cases_in_window=len(recent),
        signals=signals,
        missing=tuple(sorted(tracked - cases.keys())),
        sha256=sha256,
    )
