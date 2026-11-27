"""Run each due source in its own transaction, with its own health and cursor."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from scripts.bk2021.runner import ClientFactory, SessionFactory
from scripts.bk2021.store import tenant_id, utc_aware
from scripts.intake import rwdz
from scripts.intake.http import Sleep, SourceError, fetch
from scripts.intake.models import (
    RWDZ_TYPE,
    TED_TYPE,
    ExternalSignal,
    IntakeConfig,
    RwdzSource,
    SourceConfig,
    TedSource,
)
from scripts.intake.store import intake_events, save_signal, stored
from scripts.intake.ted import Ted
from scripts.net.guard import guarded_client
from scripts.scoring.wiring import run_scoring_stage
from scripts.storage.models import ScanRun, SignalFeed
from scripts.storage.session import get_session

logger = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 600
# Deadline extensions and results are re-read this long after the last known deadline.
_REREAD_AFTER_DEADLINE = timedelta(days=14)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Batch(_Frozen):
    signals: tuple[ExternalSignal, ...] = ()
    cursor: dict[str, str] = {}
    note: str


class SourceRun(_Frozen):
    key: str
    state: Literal["completed", "failed", "not_due"]
    records: int = 0
    created: int = 0
    changed: int = 0
    note: str = ""
    error: str = ""


class SourceStatus(_Frozen):
    key: str
    interval_hours: int
    publisher_schedule: str
    last_success: datetime | None
    due: bool
    last_attempt_status: str | None
    last_error: str | None


class ReviewEntry(_Frozen):
    signal_type: str
    external_id: str
    case: str
    company_name: str
    company_ref: str | None
    intent: str
    reasons: tuple[str, ...]
    url: str


class IntakeStatus(_Frozen):
    sources: tuple[SourceStatus, ...]
    review: tuple[ReviewEntry, ...]


def _default_client() -> httpx.AsyncClient:
    return guarded_client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "SignalForce/0.1 (intake)"},
    )


def _sources(config: IntakeConfig) -> dict[str, SourceConfig]:
    found = {"ted": config.ted, "rwdz": config.rwdz}
    return {kind: source for kind, source in found.items() if source is not None}


def _due(last_success: datetime | None, source: SourceConfig, now: datetime) -> bool:
    return last_success is None or now >= utc_aware(last_success) + timedelta(
        hours=source.interval_hours
    )


def _feed(session: Session, tenant: int, kind: str, instance: str) -> SignalFeed | None:
    return session.scalar(
        select(SignalFeed).where(
            SignalFeed.tenant_id == tenant,
            SignalFeed.source_type == kind,
            SignalFeed.feed_key == instance,
        )
    )


async def _collect_ted(
    client: httpx.AsyncClient,
    source: TedSource,
    since: datetime,
    previous: list[ExternalSignal],
    now: datetime,
    sleep: Sleep,
) -> Batch:
    # Open and unclear procedures are re-read from TED even if discovery omits them.
    tracked = frozenset(
        signal.native_id
        for signal in previous
        if signal.source_status in ("open", "review")
        and not signal.native_id.startswith("pub:")
        and (signal.expires_at is None or signal.expires_at > now - _REREAD_AFTER_DEADLINE)
    )
    found = await Ted(client, source, sleep).collect(since, tracked, now)
    note = (
        f"NicheDB od {since:%Y-%m-%d %H:%M} UTC: {found.pages} str., "
        f"{found.discovered} kandydatów cn; TED XML: {found.notices}; "
        f"sygnały: {len(found.signals)}"
    )
    return Batch(signals=found.signals, note=note)


async def _collect_rwdz(
    client: httpx.AsyncClient,
    source: RwdzSource,
    cursor: dict[str, str],
    previous: list[ExternalSignal],
    now: datetime,
    sleep: Sleep,
) -> Batch:
    headers = {}
    # A changed rule or window re-reads the file even when the publisher did not change it.
    rule = f"{rwdz.RULE_VERSION}/{source.lookback_days}/{source.intent_days}"
    if cursor.get("rule") == rule:
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]
    response = await fetch(
        client,
        str(source.url),
        headers=headers,
        allow=frozenset({304}),
        max_bytes=source.max_download_mb * 1024 * 1024,
        sleep=sleep,
    )
    if response.status_code == 304:
        if not headers:
            raise SourceError(f"{source.url}: HTTP 304 without a conditional request")
        return Batch(cursor=cursor, note="bez nowości: plik bez zmian (HTTP 304)")
    content = response.content
    modified = response.headers.get("last-modified", "")
    new_cursor = {
        "etag": response.headers.get("etag", ""),
        "last_modified": modified,
        "sha256": hashlib.sha256(content).hexdigest(),
        "rule": rule,
        "bytes": str(len(content)),
    }
    if cursor.get("sha256") == new_cursor["sha256"] and cursor.get("rule") == rule:
        return Batch(cursor=new_cursor, note="bez nowości: ten sam plik (SHA-256)")
    try:
        updated = parsedate_to_datetime(modified) if modified else None
    except ValueError as error:
        raise SourceError(f"{source.url}: invalid Last-Modified {modified!r}") from error
    tracked = {signal.native_id: signal for signal in previous}
    read = rwdz.read_zip(content, source, now, tracked=frozenset(tracked), updated=updated)
    reason = "brak wiersza w nowym pliku; decyzji nie uznano za wycofaną"
    missing = tuple(
        tracked[case].model_copy(
            update={
                "source_status": "review",
                "source_review_reasons": (reason,),
                "status": "review",
                "review_reasons": (reason,),
                "source_version": read.sha256,
                "source_updated_at": updated,
            }
        )
        for case in read.missing
    )
    note = (
        f"plik {len(content)} B: {read.rows} wierszy, {read.cases_in_window} spraw "
        f"z ostatnich {source.lookback_days} dni, sygnały: {len(read.signals)}, "
        f"brakujące: {len(missing)}"
    )
    return Batch(signals=read.signals + missing, cursor=new_cursor, note=note)


async def _collect(
    client_factory: ClientFactory,
    source: SourceConfig,
    feed: SignalFeed,
    previous: list[ExternalSignal],
    now: datetime,
    sleep: Sleep,
) -> Batch:
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS), client_factory() as client:
            if isinstance(source, TedSource):
                since = (
                    utc_aware(feed.last_success) - timedelta(hours=source.overlap_hours)
                    if feed.last_success
                    else now - timedelta(days=source.lookback_days)
                )
                return await _collect_ted(client, source, since, previous, now, sleep)
            assert isinstance(source, RwdzSource)
            cursor = (feed.meta or {}).get("cursor", {})
            return await _collect_rwdz(client, source, cursor, previous, now, sleep)
    except TimeoutError as error:
        raise SourceError(f"source exceeded {_TIMEOUT_SECONDS}s") from error


def _run_source(
    session: Session,
    tenant: int,
    kind: str,
    source: SourceConfig,
    config: IntakeConfig,
    client_factory: ClientFactory,
    now: datetime,
    sleep: Sleep,
    *,
    force: bool,
) -> SourceRun:
    key = f"{kind}:{source.instance}"
    feed = _feed(session, tenant, kind, source.instance)
    if feed is None:
        feed = SignalFeed(tenant_id=tenant, source_type=kind, feed_key=source.instance, meta={})
        session.add(feed)
    # Publication schedule and import interval are stored apart from run health.
    feed.meta = (feed.meta or {}) | {
        "publisher_schedule": source.publisher_schedule,
        "interval_hours": source.interval_hours,
    }
    if not force and not _due(feed.last_success, source, now):
        return SourceRun(key=key, state="not_due")
    run = ScanRun(tenant_id=tenant, feed_key=key, started_at=now)
    session.add(run)
    session.flush()
    signal_type = TED_TYPE if kind == "ted" else RWDZ_TYPE
    try:
        with session.begin_nested():
            previous = stored(session, tenant, signal_type, source.instance)
            batch = asyncio.run(_collect(client_factory, source, feed, previous, now, sleep))
            created = changed = 0
            affected: set[int] = set()
            for signal in batch.signals:
                new, updated, accounts = save_signal(session, tenant, signal, config, now, run.id)
                created += new
                changed += updated
                affected |= accounts
            if affected:
                run_scoring_stage(tenant, session, now=now, account_ids=affected)
            # The start of a complete run is the next discovery boundary.
            feed.last_success = now
            feed.meta = feed.meta | {"cursor": batch.cursor, "note": batch.note}
            run.status = "completed"
            run.finished_at = max(now, datetime.now(UTC))
            run.sources_probed = len(batch.signals)
            run.changes_detected = changed
            run.signals_emitted = created
    except (SourceError, SQLAlchemyError) as error:
        run.status = "failed"
        run.finished_at = max(now, datetime.now(UTC))
        run.error = (
            str(error) if isinstance(error, SourceError) else f"database: {type(error).__name__}"
        )
        logger.error("intake tenant=%s source=%s: %s", tenant, key, run.error)
        return SourceRun(key=key, state="failed", error=run.error)
    return SourceRun(
        key=key,
        state="completed",
        records=len(batch.signals),
        created=created,
        changed=changed,
        note=batch.note,
    )


def tick(
    config: IntakeConfig,
    client_factory: ClientFactory = _default_client,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
    source: str | None = None,
    force: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> tuple[SourceRun, ...]:
    """Try every selected source. A failure keeps that source due and spares the others."""
    sources = {k: v for k, v in _sources(config).items() if source in (None, k)}
    if not sources:
        raise ValueError(f"source {source!r} is not configured")
    reports = []
    for kind, source_config in sources.items():
        with session_factory() as session:
            tenant = tenant_id(session, config.tenant, lock=True)
            reports.append(
                _run_source(
                    session,
                    tenant,
                    kind,
                    source_config,
                    config,
                    client_factory,
                    now or datetime.now(UTC),
                    sleep,
                    force=force,
                )
            )
    return tuple(reports)


def status(
    config: IntakeConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> IntakeStatus:
    now = now or datetime.now(UTC)
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant)
        sources = []
        for kind, source in _sources(config).items():
            key = f"{kind}:{source.instance}"
            feed = _feed(session, tenant, kind, source.instance)
            success = utc_aware(feed.last_success) if feed and feed.last_success else None
            run = session.scalar(
                select(ScanRun)
                .where(ScanRun.tenant_id == tenant, ScanRun.feed_key == key)
                .order_by(ScanRun.id.desc())
                .limit(1)
            )
            sources.append(
                SourceStatus(
                    key=key,
                    interval_hours=source.interval_hours,
                    publisher_schedule=source.publisher_schedule,
                    last_success=success,
                    due=_due(success, source, now),
                    last_attempt_status=run.status if run else None,
                    last_error=run.error if run else None,
                )
            )
        review = []
        for event in intake_events(session, tenant):
            signal = ExternalSignal.model_validate(event.payload)
            if signal.status == "review" or event.account_id is None:
                review.append(
                    ReviewEntry(
                        signal_type=signal.signal_type,
                        external_id=signal.external_id,
                        case=signal.native_id,
                        company_name=signal.company_name,
                        company_ref=signal.company_ref,
                        intent=signal.intent_status(now),
                        reasons=signal.review_reasons
                        + (() if event.account_id else ("brak zweryfikowanej firmy",)),
                        url=signal.url,
                    )
                )
        return IntakeStatus(sources=tuple(sources), review=tuple(review))


def reassign(
    config: IntakeConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> int:
    """Apply the current operator checks to stored entries without fetching a source."""
    now = now or datetime.now(UTC)
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant, lock=True)
        changed = 0
        affected: set[int] = set()
        for event in intake_events(session, tenant):
            before = (event.account_id, event.payload["status"])
            signal = ExternalSignal.model_validate(event.payload)
            # Checks are applied to this tenant's whole set, so a removed check withdraws.
            _, _, accounts = save_signal(session, tenant, signal, config, now, None)
            if (event.account_id, event.payload["status"]) != before:
                changed += 1
                affected |= accounts
        if affected:
            run_scoring_stage(tenant, session, now=now, account_ids=affected)
        return changed
