"""Read-only dashboard: accounts, account detail, source health, run history.

ADR-0018 is the spec. Four pages, Jinja2 + HTMX, no build step (ADR-0002
Decision 2). Every route requires a signed-in invite and scopes to that
invite's tenant (`_tenant_id`). An invite with no tenant sees the "no
workspace" empty state, with one exception: the OWNER invite falls back to
the tenant named by TENANT_SLUG (the env var the CLI entrypoints read), so the
operator still sees the hosted tenant. A non-owner invite never falls back.
There is no tenant selector: the tenant comes from the invite, never the URL.

Never raises on missing data: an invite with no tenant, a tenant with no rows
yet, or a requested account_id that doesn't exist all render a normal page
(or a 404 for the single case where "this specific thing wasn't found" is
the correct HTTP semantics) -- never a 500, matching ADR-0002's "health
endpoints must never raise" posture extended to a human-facing page.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from scripts.bk2021.models import SOURCE_TYPE, SignalPayload
from scripts.measure.recall_report import compute_recall_for_tenant
from scripts.scoring.engine import SignalInput, score_account, zero_out
from scripts.scoring.wiring import load_signal_inputs
from scripts.storage.models import (
    Account,
    AccountSource,
    ScanRun,
    Score,
    SignalEvent,
    SourceHealthRecord,
    Tenant,
)
from scripts.storage.session import get_session
from scripts.web.auth import InviteIdentity, require_invite

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_invite)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_RECENT_SIGNAL_WINDOW_DAYS = 30
_SIGNAL_TIMELINE_LIMIT = 50
_RUN_HISTORY_LIMIT = 30
_HEALTH_TRAILING_DAYS = 14

DashboardInvite = Annotated[InviteIdentity, Depends(require_invite)]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant_id(session: Session, invite: InviteIdentity) -> int | None:
    """The signed-in invite's tenant. The owner invite may still resolve through TENANT_SLUG."""
    if invite.tenant_id is not None:
        return invite.tenant_id
    slug = os.environ.get("TENANT_SLUG")
    if invite.is_owner and slug:
        return session.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one_or_none()
    return None


# ---------------------------------------------------------------------------
# /dashboard -- top-scored accounts
# ---------------------------------------------------------------------------


def _load_latest_scores(tenant_id: int, session: Session) -> dict[int, Score]:
    latest_ids = (
        select(func.max(Score.id)).where(Score.tenant_id == tenant_id).group_by(Score.account_id)
    )
    rows = session.execute(select(Score).where(Score.id.in_(latest_ids))).scalars().all()
    return {s.account_id: s for s in rows}


def _load_recent_signal_types(
    tenant_id: int, window_start: datetime, session: Session, now: datetime
) -> dict[int, list[str]]:
    rows = session.execute(
        select(SignalEvent.account_id, SignalEvent.signal_type)
        .where(
            SignalEvent.tenant_id == tenant_id,
            SignalEvent.detected_at >= window_start,
            SignalEvent.account_id.is_not(None),
            or_(SignalEvent.signal_type != SOURCE_TYPE, SignalEvent.unexpired_intent(now)),
        )
        .distinct()
    ).all()
    out: dict[int, list[str]] = {}
    for account_id, signal_type in rows:
        if account_id is not None:
            out.setdefault(account_id, []).append(signal_type)
    for signal_types in out.values():
        signal_types.sort()
    return out


def _load_last_changed(tenant_id: int, session: Session) -> dict[int, datetime | None]:
    rows = session.execute(
        select(AccountSource.account_id, func.max(AccountSource.last_changed_at))
        .where(AccountSource.tenant_id == tenant_id)
        .group_by(AccountSource.account_id)
    ).all()
    return dict(rows)


def _load_account_rows(tenant_id: int, session: Session) -> list[dict[str, object]]:
    accounts = (
        session.execute(select(Account).where(Account.tenant_id == tenant_id)).scalars().all()
    )
    latest_scores = _load_latest_scores(tenant_id, session)
    now = _utcnow()
    recent_signals = _load_recent_signal_types(
        tenant_id, now - timedelta(days=_RECENT_SIGNAL_WINDOW_DAYS), session, now
    )
    last_changed = _load_last_changed(tenant_id, session)
    tender_accounts = set(
        session.scalars(
            select(SignalEvent.account_id)
            .where(SignalEvent.tenant_id == tenant_id, SignalEvent.signal_type == SOURCE_TYPE)
            .distinct()
        )
    )

    rows = []
    for account in accounts:
        latest = latest_scores.get(account.id)
        score = latest.score if latest is not None else None
        if account.id in tender_accounts:
            inputs = _current_signals(tenant_id, account.id, session, now)
            score = score_account(inputs, now=now).score
        rows.append(
            {
                "account": account,
                "score": score,
                "signal_types": recent_signals.get(account.id, []),
                "last_changed_at": last_changed.get(account.id),
            }
        )
    # Scored accounts first (highest score first); unscored accounts last.
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0)))
    return rows


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, invite: DashboardInvite) -> HTMLResponse:
    # Rendered INSIDE the session block, not after: get_session()'s commit
    # expires every loaded ORM attribute by default (expire_on_commit=True),
    # and Jinja2's TemplateResponse renders synchronously in __init__ -- if
    # rendering happened after the `with` block closed the session, any
    # attribute the template touches that wasn't already materialized would
    # raise DetachedInstanceError. Reproduced directly before this fix.
    with get_session() as session:
        tenant_id = _tenant_id(session, invite)
        rows = _load_account_rows(tenant_id, session) if tenant_id is not None else []
        return templates.TemplateResponse(
            request, "accounts.html", {"rows": rows, "tenant_configured": tenant_id is not None}
        )


# ---------------------------------------------------------------------------
# /dashboard/account/{id} -- full trace, timeline, sources, zero-out table
# ---------------------------------------------------------------------------


def _load_account_detail(
    tenant_id: int, account_id: int, session: Session
) -> dict[str, object] | None:
    account = session.execute(
        select(Account).where(Account.id == account_id, Account.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if account is None:
        return None

    latest_score = session.execute(
        select(Score)
        .where(Score.tenant_id == tenant_id, Score.account_id == account_id)
        .order_by(Score.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    signal_events = (
        session.execute(
            select(SignalEvent)
            .where(
                SignalEvent.tenant_id == tenant_id,
                SignalEvent.account_id == account_id,
                SignalEvent.signal_type != SOURCE_TYPE,
            )
            .order_by(SignalEvent.detected_at.desc())
            .limit(_SIGNAL_TIMELINE_LIMIT)
        )
        .scalars()
        .all()
    )

    sources = (
        session.execute(
            select(AccountSource).where(
                AccountSource.tenant_id == tenant_id, AccountSource.account_id == account_id
            )
        )
        .scalars()
        .all()
    )

    tenders = [
        SignalPayload.model_validate(data)
        for data in session.scalars(
            select(SignalEvent.payload)
            .where(
                SignalEvent.tenant_id == tenant_id,
                SignalEvent.account_id == account_id,
                SignalEvent.signal_type == SOURCE_TYPE,
            )
            .order_by(SignalEvent.occurred_at.desc(), SignalEvent.id.desc())
        )
    ]
    now = _utcnow()
    signals = _current_signals(tenant_id, account_id, session, now)
    current_score = latest_score.score if latest_score is not None else None
    if tenders:
        current_score = score_account(signals, now=now).score
    zero_out_rows = _compute_zero_out_table(signals, now)

    return {
        "account": account,
        "score": latest_score,
        "current_score": current_score,
        "bk2021_tenders": tenders,
        "now": now,
        "signal_events": signal_events,
        "sources": sources,
        "zero_out_rows": zero_out_rows,
    }


def _current_signals(
    tenant_id: int, account_id: int, session: Session, now: datetime
) -> list[SignalInput]:
    window_start = now - timedelta(days=90)  # ADR-0016 Decision 2
    return load_signal_inputs(tenant_id, account_id, window_start, session, warned=set(), now=now)


def _compute_zero_out_table(signals: list[SignalInput], now: datetime) -> list[dict[str, object]]:
    """Show each signal type's contribution to the account's score."""
    if not signals:
        return []

    full = score_account(signals, now=now)
    distinct_types = sorted({s.signal_type for s in signals})
    return [
        {
            "signal_type": signal_type,
            "full_score": full.score,
            "without_score": zero_out(signals, signal_type, now=now).score,
        }
        for signal_type in distinct_types
    ]


@router.get("/dashboard/account/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: int, invite: DashboardInvite) -> HTMLResponse:
    with get_session() as session:
        tenant_id = _tenant_id(session, invite)
        detail = (
            _load_account_detail(tenant_id, account_id, session) if tenant_id is not None else None
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="Account not found")
        return templates.TemplateResponse(request, "account_detail.html", detail)


# ---------------------------------------------------------------------------
# /dashboard/health -- recall, detection lag, per-source health trend
# ---------------------------------------------------------------------------


def _count_deactivated_sources(tenant_id: int, session: Session) -> int:
    """ADR-0024: the count half of CRITICAL GAP #4's fix -- re-resolution
    (ADR-0004 Decision 6) already gives a deactivated source a way back,
    but nothing surfaced that any existed until this."""
    return session.execute(
        select(func.count())
        .select_from(AccountSource)
        .where(AccountSource.tenant_id == tenant_id, AccountSource.active.is_(False))
    ).scalar_one()


def _load_health_rows(tenant_id: int, session: Session) -> list[dict[str, object]]:
    cutoff = (_utcnow() - timedelta(days=_HEALTH_TRAILING_DAYS)).date()
    rows = (
        session.execute(
            select(SourceHealthRecord)
            .where(
                SourceHealthRecord.tenant_id == tenant_id,
                SourceHealthRecord.run_date >= cutoff,
            )
            .order_by(SourceHealthRecord.source_type, SourceHealthRecord.run_date.desc())
        )
        .scalars()
        .all()
    )
    by_source_type: dict[str, list[SourceHealthRecord]] = {}
    for row in rows:
        by_source_type.setdefault(row.source_type, []).append(row)

    return [
        {
            "source_type": source_type,
            "latest": records[0],
            "trend": records,
        }
        for source_type, records in sorted(by_source_type.items())
    ]


@router.get("/dashboard/health", response_class=HTMLResponse)
def health(request: Request, invite: DashboardInvite) -> HTMLResponse:
    with get_session() as session:
        tenant_id = _tenant_id(session, invite)
        if tenant_id is None:
            recall_report = None
            health_rows: list[dict[str, object]] = []
            deactivated_sources = None
        else:
            recall_report = compute_recall_for_tenant(tenant_id, session, _utcnow())
            health_rows = _load_health_rows(tenant_id, session)
            deactivated_sources = _count_deactivated_sources(tenant_id, session)
        return templates.TemplateResponse(
            request,
            "health.html",
            {
                "recall": recall_report,
                "health_rows": health_rows,
                "deactivated_sources": deactivated_sources,
            },
        )


# ---------------------------------------------------------------------------
# /dashboard/runs -- scan run history
# ---------------------------------------------------------------------------


def _load_runs(tenant_id: int, session: Session) -> list[dict[str, object]]:
    runs = (
        session.execute(
            select(ScanRun)
            .where(ScanRun.tenant_id == tenant_id)
            .order_by(ScanRun.started_at.desc())
            .limit(_RUN_HISTORY_LIMIT)
        )
        .scalars()
        .all()
    )
    rows = []
    for run in runs:
        duration_s = None
        if run.started_at is not None and run.finished_at is not None:
            duration_s = (run.finished_at - run.started_at).total_seconds()
        change_rate = None
        if run.sources_probed:
            change_rate = run.changes_detected / run.sources_probed
        rows.append({"run": run, "duration_s": duration_s, "change_rate": change_rate})
    return rows


@router.get("/dashboard/runs", response_class=HTMLResponse)
def runs(request: Request, invite: DashboardInvite) -> HTMLResponse:
    with get_session() as session:
        tenant_id = _tenant_id(session, invite)
        rows = _load_runs(tenant_id, session) if tenant_id is not None else []
        return templates.TemplateResponse(request, "runs.html", {"rows": rows})
