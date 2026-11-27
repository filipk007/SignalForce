"""Synthetic TED eForms, NicheDB pages and RWDZ ZIPs. No real notices or people."""

import csv
import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.intake.models import (
    DecisionCheck,
    IntakeConfig,
    RwdzSource,
    TedSource,
    VerifiedCompany,
)
from scripts.intake.rwdz import COLUMNS
from scripts.storage.models import Base, Tenant

NOW = datetime(2026, 1, 20, 12, tzinfo=UTC)
PROCEDURE = "11111111-2222-3333-4444-555555555555"
CASE = "AB.1.2026/ST-WK-XX/WNIOSEK/100/2026"


def eform(
    number: str = "100-2026",
    *,
    kind: str = "cn-standard",
    procedure: str = PROCEDURE,
    published: str = "2026-01-15",
    deadline: str | None = "2026-02-20",
    cpv: str = "60100000",
    country: str = "POL",
    changes: str | None = None,
    reason: str = "update-add",
    results: tuple[tuple[str, str], ...] = (),
) -> str:
    root = "ContractAwardNotice" if kind.startswith("can") else "ContractNotice"
    change = (
        f"<efac:Changes><efbc:ChangedNoticeIdentifier>{changes}</efbc:ChangedNoticeIdentifier>"
        f"<efac:ChangeReason><cbc:ReasonCode>{reason}</cbc:ReasonCode></efac:ChangeReason>"
        "</efac:Changes>"
        if changes
        else ""
    )
    lot_results = "".join(
        f"<efac:LotResult><cbc:TenderResultCode>{code}</cbc:TenderResultCode><efac:TenderLot>"
        f'<cbc:ID schemeName="Lot">{lot}</cbc:ID></efac:TenderLot></efac:LotResult>'
        for lot, code in results
    )
    period = (
        "<cac:TenderSubmissionDeadlinePeriod>"
        f"<cbc:EndDate>{deadline}+01:00</cbc:EndDate><cbc:EndTime>10:00:00+01:00</cbc:EndTime>"
        "</cac:TenderSubmissionDeadlinePeriod>"
        if deadline
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
 xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
 xmlns:efac="http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1"
 xmlns:efbc="http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1"
 xmlns:efext="http://data.europa.eu/p27/eforms-ubl-extensions/1">
 <ext:UBLExtensions><ext:UBLExtension><ext:ExtensionContent><efext:EformsExtension>
  {change}
  <efac:NoticeResult>{lot_results}</efac:NoticeResult>
  <efac:Organizations><efac:Organization><efac:Company>
   <cbc:WebsiteURI>www.zamawiajacy.example</cbc:WebsiteURI>
   <cac:PartyIdentification><cbc:ID>ORG-0001</cbc:ID></cac:PartyIdentification>
   <cac:PartyName><cbc:Name>Przykładowa Agencja</cbc:Name></cac:PartyName>
   <cac:PartyLegalEntity><cbc:CompanyID>NIP 123-456-32-18</cbc:CompanyID></cac:PartyLegalEntity>
   <cac:Contact><cbc:ElectronicMail>osoba@example.test</cbc:ElectronicMail></cac:Contact>
  </efac:Company></efac:Organization></efac:Organizations>
  <efac:Publication>
   <efbc:NoticePublicationID>00{number}</efbc:NoticePublicationID>
   <efbc:PublicationDate>{published}+01:00</efbc:PublicationDate>
  </efac:Publication>
 </efext:EformsExtension></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>
 <cbc:ID schemeName="notice-id">notice-{number}</cbc:ID>
 <cbc:ContractFolderID>{procedure}</cbc:ContractFolderID>
 <cbc:VersionID>01</cbc:VersionID>
 <cbc:NoticeTypeCode>{kind}</cbc:NoticeTypeCode>
 <cac:ContractingParty><cac:Party><cac:PartyIdentification><cbc:ID>ORG-0001</cbc:ID>
 </cac:PartyIdentification></cac:Party></cac:ContractingParty>
 <cac:ProcurementProject><cbc:Name>Usługi przewozu</cbc:Name></cac:ProcurementProject>
 <cac:ProcurementProjectLot><cbc:ID schemeName="Lot">LOT-0001</cbc:ID>
  <cac:TenderingProcess>{period}</cac:TenderingProcess>
  <cac:ProcurementProject><cbc:Name>Przewóz przesyłek i palet</cbc:Name>
   <cac:MainCommodityClassification><cbc:ItemClassificationCode>60000000</cbc:ItemClassificationCode>
   </cac:MainCommodityClassification>
   <cac:AdditionalCommodityClassification><cbc:ItemClassificationCode>{cpv}</cbc:ItemClassificationCode>
   </cac:AdditionalCommodityClassification>
   <cac:RealizedLocation><cac:Address><cbc:CountrySubentityCode>PL623</cbc:CountrySubentityCode>
    <cac:Country><cbc:IdentificationCode>{country}</cbc:IdentificationCode></cac:Country>
   </cac:Address></cac:RealizedLocation>
  </cac:ProcurementProject>
 </cac:ProcurementProjectLot>
</{root}>"""


def row(**overrides: str) -> dict[str, str]:
    base = dict.fromkeys(COLUMNS, "")
    base.update(
        numer_urzad="AB.1.2026",
        numer_gunb="ST-WK-XX/WNIOSEK/100/2026",
        nazwa_organu="Starosta Przykładowy",
        numer_decyzji_urzedu="10/26",
        data_wydania_decyzji="2026-01-10 00:00:00",
        data_wplywu_wniosku="2025-12-01 00:00:00",
        nazwa_inwestor="Przykładowy Zakład Sp. z o.o.",
        wojewodztwo="wielkopolskie",
        miasto="Przykładowo",
        rodzaj_inwestycji="Obiekt budowlany inny niż budynek mieszkalny jednorodzinny",
        kategoria="XVIII",
        nazwa_zamierzenia_bud="rozbudowa istniejącego/istniejących obiektów budowlanych",
        nazwa_zam_budowlanego="Rozbudowa zakładu produkcyjnego o nową halę",
        projektant_nazwisko="Nieujawniany",
        numer_dzialki="1/1",
    )
    base.update(overrides)
    return base


def rwdz_zip(rows: list[dict[str, str]], header: tuple[str, ...] = COLUMNS) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.writer(text, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([[item[column] for column in COLUMNS] for item in rows])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("wynik_wielkopolskie.csv", "﻿" + text.getvalue())
    return output.getvalue()


class MockWeb:
    def __init__(self) -> None:
        self.notices: dict[str, str] = {"100-2026": eform()}
        self.candidates: list[tuple[int, str, str]] = [(1, "100-2026", "cn-standard")]
        self.zip = rwdz_zip([row()])
        self.etag = '"v1"'
        self.last_modified = "Mon, 19 Jan 2026 22:00:00 GMT"
        self.fail: dict[str, list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.slept: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if self.fail.get(host):
            return self.fail[host].pop(0)
        if host == "nichedb.dev":
            params = request.url.params
            assert (params["sort"], params["order"], params["tags"]) == ("id", "asc", "pl")
            after, limit = int(params.get("after", 0)), int(params["limit"])
            page = [c for c in self.candidates if c[0] > after][:limit]
            return httpx.Response(200, json={"items": [self._item(*c) for c in page]})
        if host == "api.ted.europa.eu":
            query = request.read().decode()
            found = [
                {"publication-number": number}
                for number, xml in self.notices.items()
                if xml.split("<cbc:ContractFolderID>")[1].split("<")[0] in query
            ]
            return httpx.Response(200, json={"notices": found, "totalNoticeCount": len(found)})
        if host == "ted.europa.eu":
            number = request.url.path.split("/")[-2]
            if number not in self.notices:
                return httpx.Response(404, text="missing")
            return httpx.Response(200, text=self.notices[number])
        assert host == "wyszukiwarka.gunb.gov.pl"
        headers = {"etag": self.etag, "last-modified": self.last_modified}
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers=headers)
        return httpx.Response(200, content=self.zip, headers=headers)

    def _item(self, item_id: int, number: str, kind: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "external_id": f"ted-{number}",
            "updated_at": "2026-01-15T04:00:00.000Z",
            "data": {"noticeType": kind, "publicationNumber": number},
        }

    def publish_file(self, rows: list[dict[str, str]], version: int) -> None:
        self.zip = rwdz_zip(rows)
        self.etag = f'"v{version}"'
        self.last_modified = f"Mon, {18 + version} Jan 2026 22:00:00 GMT"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


TED = TedSource(instance="ted-pl", interval_hours=4, publisher_schedule="dni robocze")
RWDZ = RwdzSource(instance="rwdz-wlkp", interval_hours=24, publisher_schedule="co noc")
COMPANY = VerifiedCompany(
    registry_id="1234563218",
    domain="www.zamawiajacy.example",
    evidence_url="https://zamawiajacy.example/o-nas",
)
DECISION = DecisionCheck(
    case=CASE,
    decision="positive",
    decision_evidence_url="https://wyszukiwarka.gunb.gov.pl/",
    domain="zaklad.example",
    domain_evidence_url="https://zaklad.example/o-firmie",
)
CONFIG = IntakeConfig(tenant="alpha", ted=TED, rwdz=RWDZ, companies=(COMPANY,))


@pytest.fixture
def web() -> MockWeb:
    return MockWeb()


@pytest.fixture
def database() -> Iterator[Any]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def session() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as db, db.begin():
            yield db

    with session() as db:
        db.add_all(
            [Tenant(id=1, slug="alpha", name="Alpha"), Tenant(id=2, slug="beta", name="Beta")]
        )
    yield session
    engine.dispose()
