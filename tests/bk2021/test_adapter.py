from typing import Any

import httpx
import pytest

from scripts.bk2021.adapter import BK2021, SourceError
from scripts.bk2021.models import Announcement, FeedConfig
from scripts.bk2021.qualification import qualify
from tests.bk2021.conftest import NOW, MockBK, detail

FEED = FeedConfig(key="daily", queries=("magazyn energii",), interval_hours=24, lookback_days=2)


@pytest.mark.parametrize(
    ("description", "qualifies"),
    [
        ("Dostawa i montaż magazynu energii o pojemności 1 200 kWh.", True),
        ("Zakup bateryjnego magazynu energii dla zakładu produkcyjnego.", True),
        ("Dostawa magazynu energii cieplnej 1 MWh dla zakładu produkcyjnego.", False),
        ("Budowa wodorowego magazynu energii 1 MWh dla zakładu produkcyjnego.", False),
        ("Dostawa regałów i montaż w magazynie towarowym.", False),
        ("Wykonanie audytu i nadzór nad montażem magazynu energii 300 kWh.", False),
        ("Opracowanie projektu budowy magazynu energii 1 MWh.", False),
        ("Dostawa paneli PV. Opcja dobudowy magazynu energii.", False),
        ("Dostawa przydomowych magazynów energii o łącznej pojemności 300 kWh.", False),
        ("Budowa sieciowego magazynu energii (BESS) 100 MWh.", False),
        ("Przegląd i serwis magazynu energii 500 kWh.", False),
        ("Dostawa PV przy istniejącym magazynie energii 250 kWh.", False),
        ("Dostawa magazynu energii 15 kWh o produkcji z PV 2000 kWh rocznie.", False),
        (
            "Zakup pompy z wykorzystaniem magazynu energii. Pojemność magazynu energii 220 kWh.",
            False,
        ),
        (
            "Montaż PV z wykorzystaniem magazynu energii. Zabudowany ma zostać magazyn energii 220 kWh.",
            True,
        ),
    ],
)
def test_scope_is_qualified_from_item(description: str, qualifies: bool) -> None:
    announcement = Announcement.from_source(101, detail(description=description))
    assert bool(qualify(announcement, 100)) is qualifies


def test_detail_retains_only_procurement_fields() -> None:
    announcement = Announcement.from_source(101, detail(nip="123-456-32-18"))
    assert announcement.nip == "1234563218"
    assert announcement.first_published.isoformat() == "2026-01-03"
    assert announcement.version_id == 501
    assert announcement.deadline is not None
    assert announcement.deadline.isoformat() == "2026-01-10T23:59:59+01:00"
    assert "private@" not in announcement.model_dump_json()
    assert "250–270 kWh" in qualify(announcement, 100)[0]
    assert (
        not qualify(announcement, 1000) or "zakładu produkcyjnego" in qualify(announcement, 1000)[0]
    )


@pytest.mark.parametrize("payload", [detail(source_id=102), detail(deadline=None)])
def test_invalid_detail_is_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Announcement.from_source(101, payload)


@pytest.mark.asyncio
async def test_pagination_deduplicates_and_refreshes_known_ids(bk: MockBK) -> None:
    bk.details = {item: detail(item) for item in (101, 102, 103, 104)}

    def hits(p: httpx.QueryParams) -> tuple[list[int], int]:
        if p["query"] == "BESS":
            return [102, 103], 2
        return ([101, 102] if p["page"] == "1" else [102, 103]), 3

    bk.search = hits
    config = FEED.model_copy(update={"queries": ("magazyn energii", "BESS"), "page_size": 2})
    async with bk.client() as client:
        batch = await BK2021(client).collect(config, {104}, NOW)
    assert [record.source_id for record in batch.announcements] == [101, 102, 103, 104]
    assert (batch.pages, batch.refreshed) == (3, 1)


@pytest.mark.asyncio
async def test_pagination_recovers_by_split_date_range(bk: MockBK) -> None:
    bk.details = {item: detail(item) for item in (101, 102, 103)}

    def hits(p: httpx.QueryParams) -> tuple[list[int], int]:
        first, last = p["publicationDateRange[from]"], p["publicationDateRange[to]"]
        if first != last:
            return ([101, 102] if p["page"] == "1" else [102]), 3
        return ([101], 1) if first == "2026-01-04" else ([102, 103], 2)

    bk.search = hits
    async with bk.client() as client:
        batch = await BK2021(client).collect(FEED.model_copy(update={"page_size": 2}), set(), NOW)
    assert batch.pages == 4
    assert [item.source_id for item in batch.announcements] == [101, 102, 103]


@pytest.mark.asyncio
async def test_unaccounted_search_results_fail(bk: MockBK) -> None:
    bk.search = lambda _: ([101], 3)
    config = FEED.model_copy(update={"max_pages": 1})
    async with bk.client() as client:
        with pytest.raises(SourceError, match="page limit"):
            await BK2021(client).collect(config, set(), NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_http_errors_include_endpoint_and_status(bk: MockBK, status: int) -> None:
    bk.details[101] = status
    async with bk.client() as client:
        with pytest.raises(SourceError, match=f"/101.*HTTP {status}.*upstream failure"):
            await BK2021(client).collect(FEED, set(), NOW)


@pytest.mark.asyncio
async def test_timeout_is_reported() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(SourceError, match="ReadTimeout"):
            await BK2021(client).collect(FEED, set(), NOW)
