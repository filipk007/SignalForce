"""One active, verified signal per tenant domain, across tenders and decisions."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from io import StringIO

from sqlalchemy import select

from scripts.bk2021.export import safe_cell
from scripts.bk2021.models import SOURCE_TYPE, SignalPayload
from scripts.bk2021.runner import SessionFactory
from scripts.bk2021.store import tenant_id
from scripts.intake.models import RWDZ_TYPE, ExternalSignal, IntakeConfig
from scripts.storage.models import LIFECYCLE_TYPES, Account, SignalEvent
from scripts.storage.session import get_session

_COLUMNS = (
    "domain",
    "company_name",
    "signal_type",
    "signal_id",
    "signal_date",
    "signal_url",
    "deadline",
    "why_now",
)


def export_csv(
    config: IntakeConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> str:
    """An open tender outranks a permit decision, then the newer date, then the stable key."""
    candidates: list[tuple[str, int, int, str, tuple[str, ...]]] = []
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant)
        results = session.execute(
            select(Account.domain, SignalEvent)
            .join(SignalEvent, Account.id == SignalEvent.account_id)
            .where(
                Account.tenant_id == tenant,
                SignalEvent.tenant_id == tenant,
                SignalEvent.signal_type.in_(LIFECYCLE_TYPES),
                SignalEvent.unexpired_intent(now or datetime.now(UTC)),
            )
        )
        for domain, event in results:
            if event.signal_type == SOURCE_TYPE:
                tender = SignalPayload.model_validate(event.payload)
                name, url = tender.buyer, tender.url
                day: date = tender.first_published
                deadline, why_now = tender.deadline, " ".join(tender.evidence)
            else:
                signal = ExternalSignal.model_validate(event.payload)
                name, url = signal.company_name or domain, signal.url
                day = signal.signal_date or event.detected_at.date()
                deadline, why_now = signal.deadline, signal.why_now
            row = (
                domain,
                safe_cell(name),
                event.signal_type,
                event.external_id or "",
                day.isoformat(),
                url,
                deadline.isoformat() if deadline else "",
                safe_cell(why_now),
            )
            rank = int(event.signal_type == RWDZ_TYPE)
            candidates.append((domain, rank, -day.toordinal(), event.external_id or "", row))
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_COLUMNS)
    seen: set[str] = set()
    for domain, *_, row in sorted(candidates):
        if domain not in seen:
            seen.add(domain)
            writer.writerow(row)
    return output.getvalue()
