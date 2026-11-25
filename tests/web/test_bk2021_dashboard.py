from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scripts.bk2021.models import FeedConfig, TenantConfig, VerifiedDomain
from scripts.bk2021.runner import tick
from scripts.storage.models import Account
from scripts.web import routes_dashboard
from tests.bk2021.conftest import NOW, MockBK, detail


def test_dashboard_preserves_inactive_tender_evidence(
    logged_in: TestClient, patched_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    bk = MockBK()
    config = TenantConfig(
        tenant="tester",
        feeds=(FeedConfig(key="daily", queries=("BESS",), interval_hours=24),),
        verified_domains=(
            VerifiedDomain(
                nip="1234563218",
                domain="przyklad.example",
                evidence_url="https://przyklad.example/nip",
            ),
        ),
    )
    monkeypatch.setattr(routes_dashboard, "_utcnow", lambda: NOW)
    session_factory = routes_dashboard.get_session
    assert tick(config, bk.client, session_factory=session_factory, now=NOW)[0].state == "completed"
    with patched_sessions() as db:
        account_id = db.scalar(select(Account.id))

    url = f"/dashboard/account/{account_id}"
    response = logged_in.get(url)
    assert response.status_code == 200
    for value in ("Active", "2026-01-10 23:59:59 CET", "250–270 kWh", "/ogloszenia/101", "501"):
        assert value in response.text
    assert "No signals recorded yet." not in response.text
    assert "bk2021" in logged_in.get("/dashboard").text

    after_deadline = NOW + timedelta(days=6)
    monkeypatch.setattr(routes_dashboard, "_utcnow", lambda: after_deadline)
    expired = logged_in.get(url)
    assert "Expired" in expired.text
    assert "250–270 kWh" in expired.text
    assert '<span class="value">0.0</span>' in expired.text
    assert "Last saved score 15.5" in expired.text
    assert "bk2021" not in logged_in.get("/dashboard").text

    bk.details[101] = detail(
        version=502, status="CANCELLED", deadline=None, description="Dostawa PV."
    )
    bk.search = lambda _: ([], 0)
    assert (
        tick(config, bk.client, session_factory=session_factory, now=after_deadline)[0].state
        == "completed"
    )
    closed = logged_in.get(url)
    assert all(
        value in closed.text
        for value in ("CANCELLED", "502", "Evidence from version 501", "250–270 kWh")
    )
    assert "private@example.test" not in closed.text
