"""Persist intake signals in SignalForce's existing SignalEvent and Account tables."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from scripts.bk2021.models import WARSAW
from scripts.bk2021.store import ensure_account
from scripts.intake.models import INTAKE_TYPES, RWDZ_TYPE, ExternalSignal, IntakeConfig
from scripts.intake.rwdz import UNVERIFIED
from scripts.storage.models import SignalEvent

# A new file or a re-read alone is not a change of the entry.
_VERSION_FIELDS = {"observed_at", "source_version", "source_updated_at", "evidence"}


def apply_checks(signal: ExternalSignal, config: IntakeConfig) -> tuple[ExternalSignal, str | None]:
    """Combine source facts with the operator's current checks. Returns the verified domain."""
    status, reasons = signal.source_status, signal.source_review_reasons
    domain = evidence = decision_evidence = None
    if signal.signal_type == RWDZ_TYPE:
        check = next((c for c in config.decisions if c.case == signal.native_id), None)
        if check is not None:
            domain, evidence = check.domain, str(check.domain_evidence_url)
            decision_evidence = str(check.decision_evidence_url)
        if check is not None and check.decision == "negative":
            status, reasons = "closed", ()
        elif status == "open" and check is None:
            status, reasons = "review", (*reasons, UNVERIFIED)
        elif status == "open":
            reasons = ()
    else:
        company = next((c for c in config.companies if c.registry_id == signal.company_ref), None)
        if company is not None:
            domain, evidence = company.domain, str(company.evidence_url)
    resolved = signal.model_copy(
        update={
            "status": status,
            "review_reasons": reasons,
            "mapping_evidence_url": evidence,
            "decision_evidence_url": decision_evidence,
        }
    )
    return resolved, domain


def stored(session: Session, tenant: int, signal_type: str, instance: str) -> list[ExternalSignal]:
    payloads = session.scalars(
        select(SignalEvent.payload).where(
            SignalEvent.tenant_id == tenant,
            SignalEvent.signal_type == signal_type,
            SignalEvent.external_id.startswith(f"{instance}:", autoescape=True),
        )
    )
    return [ExternalSignal.model_validate(payload) for payload in payloads]


def _older(incoming: ExternalSignal, previous: ExternalSignal) -> bool:
    if incoming.signal_type == RWDZ_TYPE:
        if incoming.source_updated_at is None or previous.source_updated_at is None:
            return False
        return incoming.source_updated_at < previous.source_updated_at
    return (incoming.updated_on or datetime.min.date()) < (
        previous.updated_on or datetime.min.date()
    )


def save_signal(
    session: Session,
    tenant: int,
    incoming: ExternalSignal,
    config: IntakeConfig,
    now: datetime,
    run_id: int | None,
) -> tuple[bool, bool, set[int]]:
    """Upsert by tenant, signal type and source key. Returns created, changed, accounts."""
    event = session.scalar(
        select(SignalEvent).where(
            SignalEvent.tenant_id == tenant,
            SignalEvent.signal_type == incoming.signal_type,
            SignalEvent.external_id == incoming.external_id,
        )
    )
    previous = ExternalSignal.model_validate(event.payload) if event is not None else None
    if previous is not None:
        if _older(incoming, previous):
            incoming = previous
        else:
            incoming = incoming.model_copy(update={"observed_at": previous.observed_at})
    signal, domain = apply_checks(incoming, config)
    account_id = (
        ensure_account(session, tenant, domain, signal.company_name or domain) if domain else None
    )
    intent_until = (
        signal.expires_at.astimezone(UTC)
        if signal.status == "open" and account_id is not None and signal.expires_at
        else None
    )
    created = event is None
    if event is None:
        day = signal.signal_date
        event = SignalEvent(
            tenant_id=tenant,
            signal_type=signal.signal_type,
            external_id=signal.external_id,
            detected_at=now,
            occurred_at=datetime.combine(day, datetime.min.time(), WARSAW) if day else None,
        )
        session.add(event)
    elif (
        previous is not None
        and event.account_id == account_id
        and signal.model_dump(exclude=_VERSION_FIELDS)
        == previous.model_dump(exclude=_VERSION_FIELDS)
    ):
        return False, False, set()
    affected = {value for value in (event.account_id, account_id) if value is not None}
    event.account_id = account_id
    event.intent_until = intent_until
    event.payload = signal.model_dump(mode="json")
    if run_id is not None:
        event.scan_run_id = run_id
    session.flush()
    return created, not created, affected


def intake_events(session: Session, tenant: int) -> list[SignalEvent]:
    return list(
        session.scalars(
            select(SignalEvent)
            .where(SignalEvent.tenant_id == tenant, SignalEvent.signal_type.in_(INTAKE_TYPES))
            .order_by(SignalEvent.signal_type, SignalEvent.external_id)
        )
    )
