from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scripts.intake.runner import tick
from scripts.storage.models import Account
from scripts.web import routes_dashboard
from tests.intake.conftest import CONFIG, NOW, MockWeb


def test_signals_page_shows_health_review_queue_and_history(
    logged_in: TestClient, patched_sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    web = MockWeb()
    config = CONFIG.model_copy(update={"tenant": "tester"})
    monkeypatch.setattr(routes_dashboard, "_utcnow", lambda: NOW)
    factory = routes_dashboard.get_session

    def run(**kwargs):
        return tick(config, web.client, session_factory=factory, sleep=web.sleep, now=NOW, **kwargs)

    assert logged_in.get("/dashboard/signals").status_code == 200
    run()
    web.fail["nichedb.dev"] = [httpx.Response(403, text="forbidden")]
    run(source="ted", force=True)

    page = logged_in.get("/dashboard/signals").text
    for value in (
        "ted:ted-pl",
        "dni robocze",
        "every 4 h",
        "HTTP 403",
        "rwdz:rwdz-wlkp",
        "co noc",
        "Active",
        "Review",
        "buyer",
        "investor",
        "2026-02-20 10:00",
        "ProcurementProjectLot LOT-0001",
        "brak potwierdzenia",
    ):
        assert value in page, value
    review = logged_in.get("/dashboard/signals?show=review").text
    assert "Przykładowy Zakład" in review and "Przykładowa Agencja" not in review

    with patched_sessions() as db:
        account_id = db.scalar(select(Account.id))
    account = logged_in.get(f"/dashboard/account/{account_id}").text
    assert "Przewóz przesyłek i palet" in account and "Active" in account
    assert "ted_road_transport" in logged_in.get("/dashboard").text
    assert "ted:ted-pl" in logged_in.get("/dashboard/runs").text
    assert "BK2021 / ted" not in logged_in.get("/dashboard/runs").text

    monkeypatch.setattr(routes_dashboard, "_utcnow", lambda: NOW + timedelta(days=60))
    later = logged_in.get(f"/dashboard/account/{account_id}").text
    assert "Expired" in later and '<span class="value">0.0</span>' in later
    assert "ted_road_transport" not in logged_in.get("/dashboard").text
    assert "Przykładowa Agencja" not in logged_in.get("/dashboard/signals?show=active").text
