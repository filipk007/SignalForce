"""Persistence of BK2021 announcements within SignalForce's storage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeAlias

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scripts.bk2021.models import (
    PUBLISHED,
    SOURCE_TYPE,
    WARSAW,
    Announcement,
    FeedConfig,
    SignalPayload,
    VerifiedDomain,
)
from scripts.bk2021.qualification import qualify
from scripts.storage.models import Account, BK2021FeedMember, SignalEvent, SignalFeed, Tenant

Bindings: TypeAlias = Mapping[str, VerifiedDomain]


def utc_aware(value: datetime) -> datetime:
    # SQLite drops the offset on ORM reads. Postgres preserves it.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def tenant_id(session: Session, slug: str, *, lock: bool = False) -> int:
    query = select(Tenant.id).where(Tenant.slug == slug)
    if lock:
        # Serialize BK2021 writes for this tenant, including overlapping feeds.
        # NO KEY UPDATE still lets other processes create rows with a tenant FK.
        query = query.with_for_update(key_share=True)
    found = session.scalar(query)
    if found is None:
        raise ValueError(f"unknown SignalForce tenant {slug!r}")
    return found


def find_feed(session: Session, tenant: int, key: str) -> SignalFeed | None:
    return session.scalar(
        select(SignalFeed).where(
            SignalFeed.tenant_id == tenant,
            SignalFeed.source_type == SOURCE_TYPE,
            SignalFeed.feed_key == key,
        )
    )


def known_open_ids(session: Session, feed: SignalFeed) -> set[int]:
    ids = session.scalars(
        select(SignalEvent.external_id)
        .join(BK2021FeedMember, BK2021FeedMember.signal_event_id == SignalEvent.id)
        .where(
            BK2021FeedMember.feed_id == feed.id,
            SignalEvent.tenant_id == feed.tenant_id,
            SignalEvent.signal_type == SOURCE_TYPE,
            SignalEvent.payload["status"].as_string() == PUBLISHED,
        )
    )
    return {int(external_id) for external_id in ids if external_id is not None}


def binding_for(payload: Announcement, bindings: Bindings) -> VerifiedDomain | None:
    return bindings.get(payload.nip) if payload.nip else None


def account_id_for(
    session: Session, tenant: int, payload: Announcement, binding: VerifiedDomain | None
) -> int | None:
    if binding is None:
        return None
    lookup = select(Account.id).where(Account.tenant_id == tenant, Account.domain == binding.domain)
    account_id = session.scalar(lookup)
    if account_id is not None:
        return account_id
    try:
        # The career registry can create this domain concurrently.
        with session.begin_nested():
            account = Account(tenant_id=tenant, domain=binding.domain, name=payload.buyer[:255])
            session.add(account)
            session.flush()
    except IntegrityError:
        account_id = session.scalar(lookup)
        if account_id is None:
            raise
        return account_id
    return account.id


def save_announcement(
    session: Session,
    feed: SignalFeed,
    config: FeedConfig,
    incoming: Announcement,
    bindings: Bindings,
    now: datetime,
    run_id: int,
) -> tuple[bool, set[int]]:
    external_id = str(incoming.source_id)
    event = session.scalar(
        select(SignalEvent).where(
            SignalEvent.tenant_id == feed.tenant_id,
            SignalEvent.signal_type == SOURCE_TYPE,
            SignalEvent.external_id == external_id,
        )
    )
    previous = SignalPayload.model_validate(event.payload) if event is not None else None
    announcement = incoming
    if previous is not None:
        if incoming.version_id < previous.version_id:
            announcement = Announcement.model_validate(previous.model_dump())
        else:
            announcement = incoming.model_copy(update={"first_published": previous.first_published})

    memberships = (
        list(
            session.scalars(
                select(BK2021FeedMember)
                .join(SignalFeed, SignalFeed.id == BK2021FeedMember.feed_id)
                .where(
                    BK2021FeedMember.signal_event_id == event.id,
                    SignalFeed.tenant_id == feed.tenant_id,
                    SignalFeed.source_type == SOURCE_TYPE,
                )
            )
        )
        if event is not None
        else []
    )
    membership = next((member for member in memberships if member.feed_id == feed.id), None)
    current_evidence = qualify(announcement, config.min_capacity_kwh)
    if event is None and not current_evidence:
        return False, set()

    created = event is None
    if event is None:
        event = SignalEvent(
            tenant_id=feed.tenant_id,
            signal_type=SOURCE_TYPE,
            external_id=external_id,
            detected_at=now,
            occurred_at=datetime.combine(announcement.first_published, datetime.min.time(), WARSAW),
        )
        session.add(event)
        session.flush()

    thresholds = {member.min_capacity_kwh for member in memberships if member.feed_id != feed.id}
    if membership is not None:
        membership.min_capacity_kwh = config.min_capacity_kwh
    elif current_evidence:
        session.add(
            BK2021FeedMember(
                feed_id=feed.id,
                signal_event_id=event.id,
                min_capacity_kwh=config.min_capacity_kwh,
            )
        )
    evidence = tuple(
        dict.fromkeys(
            current_evidence
            + tuple(
                quote
                for threshold in sorted(thresholds)
                for quote in qualify(announcement, threshold)
            )
        )
    )
    binding = binding_for(announcement, bindings)
    account_id = account_id_for(session, feed.tenant_id, announcement, binding)
    intent_until = (
        announcement.deadline.astimezone(UTC)
        if evidence and announcement.status == PUBLISHED and announcement.deadline is not None
        else None
    )
    new_payload = SignalPayload(
        **announcement.model_dump(exclude={"url"}),
        qualified=bool(evidence),
        evidence=evidence or (previous.evidence if previous else ()),
        evidence_version_id=(
            announcement.version_id
            if evidence or previous is None
            else previous.evidence_version_id
        ),
        mapping_evidence_url=str(binding.evidence_url) if binding else None,
    ).model_dump(mode="json")
    old_until = utc_aware(event.intent_until) if event.intent_until is not None else None
    affected: set[int] = set()
    if event.account_id != account_id or old_until != intent_until:
        affected = {value for value in (event.account_id, account_id) if value is not None}
    if new_payload != event.payload or event.account_id != account_id:
        event.scan_run_id = run_id
        event.payload = new_payload
        event.account_id = account_id
        event.intent_until = intent_until
    return created, affected
