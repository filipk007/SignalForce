"""Export the freshest active, resolved BK2021 signal per tenant account domain."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from io import StringIO

from sqlalchemy import select

from scripts.bk2021.models import SOURCE_TYPE, SignalPayload, TenantConfig
from scripts.bk2021.runner import SessionFactory
from scripts.bk2021.store import tenant_id
from scripts.storage.models import Account, SignalEvent
from scripts.storage.session import get_session

_COLUMNS = (
    "domain",
    "company_name",
    "nip",
    "signal_type",
    "signal_id",
    "signal_date",
    "signal_url",
    "deadline",
    "why_now",
)


def safe_cell(value: str) -> str:
    # Spreadsheet clients may treat quotes from the announcement as formulas.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export_csv(
    config: TenantConfig,
    *,
    session_factory: SessionFactory = get_session,
    now: datetime | None = None,
) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_COLUMNS)
    with session_factory() as session:
        tenant = tenant_id(session, config.tenant)
        results = session.execute(
            select(Account.domain, SignalEvent.payload)
            .join(SignalEvent, Account.id == SignalEvent.account_id)
            .where(
                Account.tenant_id == tenant,
                SignalEvent.tenant_id == tenant,
                SignalEvent.signal_type == SOURCE_TYPE,
                SignalEvent.unexpired_intent(now or datetime.now(UTC)),
            )
            .order_by(
                Account.domain, SignalEvent.occurred_at.desc(), SignalEvent.external_id.desc()
            )
        )
        seen = set()
        for domain, data in results:
            if domain in seen:
                continue
            seen.add(domain)
            payload = SignalPayload.model_validate(data)
            writer.writerow(
                (
                    domain,
                    safe_cell(payload.buyer),
                    payload.nip,
                    SOURCE_TYPE,
                    payload.source_id,
                    payload.first_published.isoformat(),
                    payload.url,
                    payload.deadline.isoformat() if payload.deadline else "",
                    safe_cell(" ".join(payload.evidence)),
                )
            )
    return output.getvalue()
