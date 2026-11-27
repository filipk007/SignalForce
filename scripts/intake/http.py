"""Bounded requests with a limited retry for throttled or unavailable sources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from scripts.net.guard import get_capped

Sleep = Callable[[float], Awaitable[None]]
_ATTEMPTS = 3
_MAX_WAIT_SECONDS = 60
_RETRIED = frozenset({429, 500, 502, 503, 504})


class SourceError(RuntimeError):
    """An incomplete import. The next tick retries from the last success."""


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    sleep: Sleep = asyncio.sleep,
    max_bytes: int = 8 * 1024 * 1024,
    method: str = "GET",
    allow: frozenset[int] = frozenset(),
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = await get_capped(
                client, url, max_bytes=max_bytes, method=method, headers=headers, **kwargs
            )
        except httpx.HTTPError as error:
            if attempt == _ATTEMPTS:
                raise SourceError(f"{url}: {type(error).__name__}: {error}") from error
            await sleep(2**attempt)
            continue
        if response.is_success or response.status_code in allow:
            return response
        if response.status_code not in _RETRIED or attempt == _ATTEMPTS:
            raise SourceError(f"{url}: HTTP {response.status_code}: {response.text[:300]!r}")
        retry_after = response.headers.get("retry-after", "")
        wait = int(retry_after) if retry_after.isdigit() else 2**attempt
        if wait > _MAX_WAIT_SECONDS:
            raise SourceError(f"{url}: HTTP {response.status_code}, retry after {wait}s")
        await sleep(wait)
    raise AssertionError("unreachable")
