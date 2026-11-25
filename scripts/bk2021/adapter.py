"""Bounded read-only BK2021 search and authoritative detail refresh."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scripts.bk2021.models import BASE_URL, PUBLISHED, WARSAW, Announcement, FeedConfig, PositiveId
from scripts.net.guard import get_capped

_API = f"{BASE_URL}/api/announcements"
_MAX_BYTES = 4 * 1024 * 1024
_SCAN_TIMEOUT_SECONDS = 240


class SourceError(RuntimeError):
    """An incomplete scan. The caller may retry on the next tick."""


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: PositiveId


class SearchMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = Field(strict=True, ge=0)


class SearchPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    advertisements: tuple[SearchHit, ...]
    meta: SearchMeta


class Batch(BaseModel):
    model_config = ConfigDict(frozen=True)

    announcements: tuple[Announcement, ...]
    pages: int
    refreshed: int


class BK2021:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        url = _API + path
        try:
            response = await get_capped(self.client, url, params=params, max_bytes=_MAX_BYTES)
            if response.is_error or response.is_redirect:
                # HTTP error bodies can contain request echoes. Bound the diagnostic.
                raise SourceError(
                    f"{response.url}: HTTP {response.status_code}: {response.text[:300]!r}"
                )
            payload = response.json()
            if payload["status"] != "OK" or not isinstance(payload["data"], dict):
                raise ValueError("expected a successful data envelope")
            return payload["data"]
        except (httpx.HTTPError, TypeError, KeyError, ValueError) as error:
            raise SourceError(f"{url}: {type(error).__name__}: {error}") from error

    async def _search(
        self, feed: FeedConfig, query: str, start: date, end: date
    ) -> tuple[set[int], int]:
        pages = 0

        async def search(first: date, last: date, size: int) -> set[int]:
            nonlocal pages
            found: set[int] = set()
            expected = None
            for page in range(1, feed.max_pages + 1):
                if pages >= feed.max_pages:
                    raise SourceError(f"BK2021 page limit ({feed.max_pages}) for {query!r}")
                pages += 1
                data = await self._get(
                    "/search",
                    {
                        "query": query,
                        "page": page,
                        "limit": size,
                        "sort": "publicationDate",
                        "status[]": PUBLISHED,
                        "publicationDateRange[from]": first.isoformat(),
                        "publicationDateRange[to]": last.isoformat(),
                    },
                )
                try:
                    result = SearchPage.model_validate(data)
                    if len(result.advertisements) > size:
                        raise ValueError("more rows than requested")
                except ValueError as error:
                    raise SourceError(f"BK2021 invalid search page {page} for {query!r}") from error
                total = result.meta.total
                if total > feed.max_details:
                    raise SourceError(
                        f"BK2021 detail limit ({feed.max_details}) for {query!r}: {total}"
                    )
                if expected is None:
                    expected = total
                if total != expected:
                    raise SourceError(f"BK2021 result count changed between pages for {query!r}")
                found.update(hit.id for hit in result.advertisements)
                if len(found) == expected:
                    return found
                if (
                    len(found) > expected
                    or len(result.advertisements) < size
                    or page * size >= expected
                ):
                    break

            # Ordering can shift across pages when several notices share a publication date.
            if first < last:
                middle = first + (last - first) // 2
                recovered = await search(first, middle, size)
                recovered |= await search(middle + timedelta(days=1), last, size)
            elif size < 50:
                recovered = await search(first, last, 50)
            else:
                recovered = set()
            if len(recovered) != expected or not found.issubset(recovered):
                raise SourceError(
                    f"BK2021 incomplete search for {query!r} ({first} to {last}): "
                    f"{len(found)} of {expected} results"
                )
            return recovered

        ids = await search(start, end, feed.page_size)
        return ids, pages

    async def collect(self, feed: FeedConfig, known: set[int], now: datetime) -> Batch:
        try:
            async with asyncio.timeout(_SCAN_TIMEOUT_SECONDS):
                return await self._collect(feed, known, now)
        except TimeoutError as error:
            raise SourceError(f"BK2021 feed exceeded {_SCAN_TIMEOUT_SECONDS}s") from error

    async def _collect(self, feed: FeedConfig, known: set[int], now: datetime) -> Batch:
        today = now.astimezone(WARSAW).date()
        start = today - timedelta(days=feed.lookback_days - 1)
        ids: set[int] = set()
        pages = 0
        for query in feed.queries:
            found, count = await self._search(feed, query, start, today)
            ids.update(found)
            pages += count
        refreshed = len(known - ids)
        ids.update(known)
        if len(ids) > feed.max_details:
            raise SourceError(f"BK2021 detail limit ({feed.max_details}): {len(ids)}")
        announcements = []
        for source_id in sorted(ids):
            data = await self._get(f"/{source_id}")
            try:
                announcements.append(Announcement.from_source(source_id, data))
            except ValueError as error:
                raise SourceError(
                    f"{_API}/{source_id}: invalid announcement details: {error}"
                ) from error
        return Batch(announcements=tuple(announcements), pages=pages, refreshed=refreshed)
