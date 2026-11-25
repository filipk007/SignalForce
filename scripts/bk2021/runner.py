"""Run one due-feed tick and reconcile pending BK2021 accounts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from scripts.bk2021.adapter import BK2021, Batch, SourceError
from scripts.bk2021.models import SOURCE_TYPE, FeedConfig, SignalPayload, TenantConfig
from scripts.bk2021.store import (
    Bindings,
    account_id_for,
    binding_for,
    find_feed,
    known_open_ids,
    save_announcement,
    tenant_id,
    utc_aware,
)
from scripts.net.guard import guarded_client
from scripts.scoring.wiring import run_scoring_stage
from scripts.storage.models import ScanRun, SignalEvent, SignalFeed
from scripts.storage.session import get_session

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], AbstractContextManager[Session]]
ClientFactory = Callable[[], httpx.AsyncClient]


class FeedRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    state: Literal["completed", "failed", "not_due"]
    pages: int = 0
    details: int = 0
    refreshed: int = 0
    signals_emitted: int = 0
    error: str = ""


class FeedStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    interval_hours: int
    last_success: datetime | None
    due: bool
    last_attempt_status: str | None
    last_error: str | None


class PendingSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    company_name: str
    nip: str | None
    url: str
    status: str
    intent: str
    deadline: datetime | None
    evidence: tuple[str, ...]


class TenantStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    feeds: tuple[FeedStatus, ...]
    unassigned: tuple[PendingSignal, ...]


def _default_client() -> httpx.AsyncClient:
    return guarded_client(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        headers={"Accept": "application/json", "User-Agent": "SignalForce/0.1 (BK2021)"},
    )


def _due(last_success: datetime | None, config: FeedConfig, now: datetime) -> bool:
    return last_success is None or now >= utc_aware(last_success) + timedelta(
        hours=config.interval_hours
    )


async def _collect(
    client_factory: ClientFactory, config: FeedConfig, known: set[int], now: datetime
) -> Batch:
    async with client_factory() as client:
        return await BK2021(client).collect(config, known, now)


def _run_feed(
    session: Session,
    tenant: int,
    config: FeedConfig,
    bindings: Bindings,
    client_factory: ClientFactory,
    now: datetime,
    *,
    force: bool,
    fixed_time: bool,
) -> FeedRun:
    feed = find_feed(session, tenant, config.key)
    if feed is not None and not force and not _due(feed.last_success, config, now):
        return FeedRun(key=config.key, state="not_due")
    if feed is None:
        feed = SignalFeed(tenant_id=tenant, source_type=SOURCE_TYPE, feed_key=config.key)
        session.add(feed)
    run = ScanRun(tenant_id=tenant, feed_key=config.key, started_at=now)
    session.add(run)
    session.flush()

    try:
        with session.begin_nested():
            batch = asyncio.run(
                _collect(client_factory, config, known_open_ids(session, feed), now)
            )
            affected: set[int] = set()
            for detail in batch.announcements:
                created, accounts = save_announcement(
                    session, feed, config, detail, bindings, now, run.id
                )
                run.signals_emitted += int(created)
                affected.update(accounts)
            finished = now if fixed_time else datetime.now(UTC)
            if affected:
                run_scoring_stage(tenant, session, now=finished, account_ids=affected)
            feed.last_success = finished
            run.status = "completed"
            run.finished_at = finished
            run.sources_probed = len(batch.announcements)
    except (SourceError, SQLAlchemyError) as error:
        run.status = "failed"
        run.finished_at = now if fixed_time else datetime.now(UTC)
        run.error = (
            str(error) if isinstance(error, SourceError) else f"database: {type(error).__name__}"
        )
        logger.error("BK2021 tenant=%s feed=%s: %s", tenant, config.key, run.error)
        return FeedRun(key=config.key, state="failed", error=run.error)
    return FeedRun(
        key=config.key,
        state="completed",
        pages=batch.pages,
        details=len(batch.announcements),
        refreshed=batch.refreshed,
        signals_emitted=run.signals_emitted,
    )


def tick(
    config: TenantConfig,
    client_factory: ClientFactory = _default_client,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
    feed_key: str | None = None,
    force: bool = False,
) -> tuple[FeedRun, ...]:
    """Try each selected feed independently. A failed write leaves its schedule due."""
    feeds = tuple(feed for feed in config.feeds if feed_key is None or feed.key == feed_key)
    if not feeds:
        raise ValueError(f"unknown BK2021 feed {feed_key!r}")
    bindings = {entry.nip: entry for entry in config.verified_domains}
    reports = []
    for feed in feeds:
        with session_factory() as session:
            tenant = tenant_id(session, config.tenant, lock=True)
            reports.append(
                _run_feed(
                    session,
                    tenant,
                    feed,
                    bindings,
                    client_factory,
                    now or datetime.now(UTC),
                    force=force,
                    fixed_time=now is not None,
                )
            )
    return tuple(reports)


def status(
    config: TenantConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> TenantStatus:
    now = now or datetime.now(UTC)
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant)
        feeds = []
        for config_feed in config.feeds:
            feed = find_feed(session, tenant, config_feed.key)
            last_success = utc_aware(feed.last_success) if feed and feed.last_success else None
            run = session.scalar(
                select(ScanRun)
                .where(ScanRun.tenant_id == tenant, ScanRun.feed_key == config_feed.key)
                .order_by(ScanRun.id.desc())
                .limit(1)
            )
            feeds.append(
                FeedStatus(
                    key=config_feed.key,
                    interval_hours=config_feed.interval_hours,
                    last_success=last_success,
                    due=_due(last_success, config_feed, now),
                    last_attempt_status=run.status if run else None,
                    last_error=run.error if run else None,
                )
            )
        rows = session.scalars(
            select(SignalEvent)
            .where(
                SignalEvent.tenant_id == tenant,
                SignalEvent.signal_type == SOURCE_TYPE,
                SignalEvent.account_id.is_(None),
            )
            .order_by(SignalEvent.external_id)
        )
        pending = []
        for event in rows:
            payload = SignalPayload.model_validate(event.payload)
            pending.append(
                PendingSignal(
                    external_id=str(payload.source_id),
                    company_name=payload.buyer,
                    nip=payload.nip,
                    url=payload.url,
                    status=payload.status,
                    intent=payload.intent_status(now),
                    deadline=payload.deadline,
                    evidence=payload.evidence,
                )
            )
        return TenantStatus(feeds=tuple(feeds), unassigned=tuple(pending))


def reassign(
    config: TenantConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> int:
    """Apply the latest verified mapping to stored entries, including closed ones."""
    bindings = {entry.nip: entry for entry in config.verified_domains}
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant, lock=True)
        events = session.scalars(
            select(SignalEvent).where(
                SignalEvent.tenant_id == tenant, SignalEvent.signal_type == SOURCE_TYPE
            )
        ).all()
        changed = 0
        affected = set()
        for event in events:
            payload = SignalPayload.model_validate(event.payload)
            binding = binding_for(payload, bindings)
            account = account_id_for(session, tenant, payload, binding)
            if event.account_id != account:
                changed += 1
                affected.update(value for value in (account, event.account_id) if value is not None)
                event.account_id = account
            evidence_url = str(binding.evidence_url) if binding else None
            if payload.mapping_evidence_url != evidence_url:
                updated = payload.model_copy(update={"mapping_evidence_url": evidence_url})
                event.payload = updated.model_dump(mode="json")
        if affected:
            run_scoring_stage(tenant, session, now=now or datetime.now(UTC), account_ids=affected)
        return changed
