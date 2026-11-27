"""The shared import through the real storage models, with all HTTP mocked."""

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from scripts.bk2021.models import FeedConfig, TenantConfig, VerifiedDomain
from scripts.bk2021.runner import tick as bk_tick
from scripts.intake import __main__ as cli
from scripts.intake.export import export_csv
from scripts.intake.models import IntakeConfig
from scripts.intake.runner import SourceRun, reassign, status, tick
from scripts.scoring.wiring import load_signal_inputs
from scripts.storage.models import Account, ScanRun, SignalEvent, SignalFeed
from tests.bk2021.conftest import MockBK
from tests.intake.conftest import CASE, CONFIG, DECISION, NOW, MockWeb, eform, row, rwdz_zip

COLUMNS = [
    "domain",
    "company_name",
    "signal_type",
    "signal_id",
    "signal_date",
    "signal_url",
    "deadline",
    "why_now",
]
VERIFIED = CONFIG.model_copy(update={"decisions": (DECISION,)})


def run(web: MockWeb, database, config: IntakeConfig = CONFIG, at=NOW, **kwargs):
    return tick(config, web.client, session_factory=database, now=at, sleep=web.sleep, **kwargs)


def events(database) -> dict[str, SignalEvent]:
    with database() as db:
        return {event.signal_type: event for event in db.scalars(select(SignalEvent))}


def exported(database, config: IntakeConfig = CONFIG, at=NOW) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(export_csv(config, session_factory=database, now=at))))


def test_both_sources_land_in_shared_storage_and_replay_adds_nothing(web, database) -> None:
    first = run(web, database)
    assert [(r.key, r.state, r.created) for r in first] == [
        ("ted:ted-pl", "completed", 1),
        ("rwdz:rwdz-wlkp", "completed", 1),
    ]
    saved = events(database)
    ted, rwdz = saved["ted_road_transport"], saved["rwdz_plant_expansion"]
    assert ted.account_id is not None and ted.intent_until is not None
    assert ted.external_id.startswith("ted-pl:")
    assert rwdz.account_id is None and rwdz.intent_until is None
    assert [r["signal_type"] for r in exported(database)] == ["ted_road_transport"]
    assert list(exported(database)[0]) == COLUMNS

    again = run(web, database, at=NOW + timedelta(hours=1), force=True)
    assert [(r.state, r.created, r.changed) for r in again] == [("completed", 0, 0)] * 2
    assert again[1].note == "bez nowości: plik bez zmian (HTTP 304)"
    assert {e.id for e in events(database).values()} == {ted.id, rwdz.id}


def test_each_source_keeps_its_own_interval_and_overlap(web, database) -> None:
    run(web, database)
    later = NOW + timedelta(hours=5)
    assert [r.state for r in run(web, database, at=later)] == ["completed", "not_due"]
    since = [r.url.params["since"] for r in web.requests if r.url.host == "nichedb.dev"]
    assert since == ["2026-01-17T12:00:00+00:00", "2026-01-19T12:00:00+00:00"]
    report = status(CONFIG, session_factory=database, now=later)
    assert [(s.key, s.interval_hours, s.publisher_schedule) for s in report.sources] == [
        ("ted:ted-pl", 4, "dni robocze"),
        ("rwdz:rwdz-wlkp", 24, "co noc"),
    ]
    assert [p.company_name for p in report.review] == ["Przykładowy Zakład Sp. z o.o."]


def test_discovery_pages_by_id_so_equal_update_times_are_not_lost(web, database) -> None:
    config = CONFIG.model_copy(update={"ted": CONFIG.ted.model_copy(update={"page_size": 1})})
    web.notices["101-2026"] = eform("101-2026", procedure="aaaaaaaa-0000-0000-0000-000000000000")
    web.candidates.append((2, "101-2026", "cn-standard"))
    assert run(web, database, config, source="ted")[0].created == 2
    pages = [r for r in web.requests if r.url.host == "nichedb.dev"]
    assert [r.url.params.get("after") for r in pages] == [None, "1", "2"]


def test_one_failed_source_keeps_its_cursor_and_the_other_completes(web, database) -> None:
    web.fail["api.ted.europa.eu"] = [httpx.Response(500, text="boom")] * 3
    ted, rwdz = run(web, database)
    assert (ted.state, rwdz.state) == ("failed", "completed") and "HTTP 500" in ted.error
    assert set(events(database)) == {"rwdz_plant_expansion"}
    with database() as db:
        assert {f.source_type: f.last_success for f in db.scalars(select(SignalFeed))}[
            "ted"
        ] is None
        assert sorted(r.status for r in db.scalars(select(ScanRun))) == ["completed", "failed"]
    health = status(CONFIG, session_factory=database, now=NOW).sources[0]
    assert (health.last_attempt_status, health.due) == ("failed", True)
    assert run(web, database)[0].state == "completed"


def test_rate_limit_honours_retry_after(web, database) -> None:
    web.fail["ted.europa.eu"] = [httpx.Response(429, headers={"retry-after": "7"})]
    assert run(web, database, source="ted")[0].state == "completed"
    assert 7 in web.slept


def test_correction_and_close_update_one_event_and_leave_history(web, database) -> None:
    run(web, database, source="ted")
    original = events(database)["ted_road_transport"].id
    web.candidates = []
    web.notices["200-2026"] = eform(
        "200-2026", published="2026-01-19", deadline="2026-03-01", changes="100-2026"
    )
    assert run(web, database, source="ted", force=True)[0].changed == 1
    event = events(database)["ted_road_transport"]
    assert event.id == original and event.payload["deadline"].startswith("2026-03-01")

    web.notices["300-2026"] = eform(
        "300-2026", kind="can-standard", published="2026-01-20", results=(("LOT-0001", "selec-w"),)
    )
    run(web, database, source="ted", force=True)
    event = events(database)["ted_road_transport"]
    assert (event.id, event.payload["status"], event.intent_until) == (original, "closed", None)
    assert exported(database) == []
    with database() as db:
        assert (
            load_signal_inputs(
                1, event.account_id, NOW - timedelta(days=90), db, warned=set(), now=NOW
            )
            == []
        )


def test_deadline_removes_current_intent_between_scans(web, database) -> None:
    run(web, database, source="ted")
    after = NOW + timedelta(days=60)
    assert len(exported(database)) == 1 and exported(database, at=after) == []
    assert "ted_road_transport" in events(database)


def test_operator_checks_activate_one_row_per_domain_and_can_be_withdrawn(web, database) -> None:
    run(web, database)
    assert reassign(VERIFIED, session_factory=database, now=NOW) == 1
    assert sorted(r["domain"] for r in exported(database)) == [
        "zaklad.example",
        "zamawiajacy.example",
    ]
    same_domain = VERIFIED.model_copy(
        update={"decisions": (DECISION.model_copy(update={"domain": "zamawiajacy.example"}),)}
    )
    reassign(same_domain, session_factory=database, now=NOW)
    (only,) = exported(database)
    assert only["signal_type"] == "ted_road_transport"

    unverified = CONFIG.model_copy(update={"companies": ()})
    reassign(unverified, session_factory=database, now=NOW)
    assert exported(database) == []
    assert events(database)["rwdz_plant_expansion"].intent_until is None


def test_negative_decision_is_closed_even_with_a_domain(web, database) -> None:
    negative = CONFIG.model_copy(
        update={"decisions": (DECISION.model_copy(update={"decision": "negative"}),)}
    )
    run(web, database, negative, source="rwdz")
    event = events(database)["rwdz_plant_expansion"]
    assert (event.payload["status"], event.intent_until) == ("closed", None)


def test_new_file_corrects_the_same_entry_and_a_missing_row_needs_review(web, database) -> None:
    run(web, database, VERIFIED, source="rwdz")
    first = events(database)["rwdz_plant_expansion"]
    assert first.intent_until is not None

    web.publish_file([row(nazwa_zam_budowlanego="Rozbudowa zakładu produkcyjnego, etap 2")], 2)
    assert run(web, database, VERIFIED, source="rwdz", force=True)[0].changed == 1
    corrected = events(database)["rwdz_plant_expansion"]
    assert corrected.id == first.id and "etap 2" in corrected.payload["title"]

    web.publish_file([row(kategoria="XVII")], 3)
    run(web, database, VERIFIED, source="rwdz", force=True)
    assert "reguły" in events(database)["rwdz_plant_expansion"].payload["review_reasons"][0]

    web.publish_file([row(numer_gunb="OTHER", kategoria="I")], 4)
    run(web, database, VERIFIED, source="rwdz", force=True)
    missing = events(database)["rwdz_plant_expansion"]
    assert (missing.id, missing.payload["status"], missing.intent_until) == (
        first.id,
        "review",
        None,
    )
    assert "brak wiersza" in missing.payload["review_reasons"][0]
    reassign(VERIFIED, session_factory=database, now=NOW)
    assert events(database)["rwdz_plant_expansion"].payload["status"] == "review"


def test_corrupt_file_fails_without_moving_the_version(web, database) -> None:
    run(web, database, source="rwdz")
    web.zip, web.etag = rwdz_zip([row()])[:-30], '"broken"'
    assert run(web, database, source="rwdz", force=True)[0].state == "failed"
    with database() as db:
        assert db.scalar(select(SignalFeed)).meta["cursor"]["etag"] == '"v1"'
    assert len(events(database)) == 1


def test_tenants_do_not_share_events_or_accounts(web, database) -> None:
    run(web, database, source="ted")
    run(web, database, CONFIG.model_copy(update={"tenant": "beta"}), source="ted")
    with database() as db:
        assert len(db.scalars(select(SignalEvent)).all()) == 2
        assert len(db.scalars(select(Account)).all()) == 2
    assert len(exported(database)) == 1
    assert CASE not in export_csv(CONFIG, session_factory=database, now=NOW)


def test_cli_reports_a_failed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "intake.yaml"
    path.write_text(
        "tenant: alpha\nted:\n  instance: ted-pl\n  interval_hours: 4\n"
        "  publisher_schedule: dni robocze\n",
        encoding="utf-8",
    )

    def failed(config: IntakeConfig, *, source: str | None, force: bool) -> tuple[SourceRun, ...]:
        assert (config.tenant, source, force) == ("alpha", "ted", True)
        return (SourceRun(key="ted:ted-pl", state="failed", error="source unavailable"),)

    monkeypatch.setattr(cli, "tick", failed)
    assert cli.main(["--config", str(path), "tick", "--source", "ted", "--force"]) == 1
    assert "source unavailable" in capsys.readouterr().out


def test_long_expired_procedures_stop_being_reread(web, database) -> None:
    run(web, database, source="ted")
    web.candidates, web.requests = [], []
    run(web, database, source="ted", at=NOW + timedelta(days=36), force=True)
    assert [r.url.host for r in web.requests] == [
        "nichedb.dev",
        "api.ted.europa.eu",
        "ted.europa.eu",
    ]
    web.requests = []
    run(web, database, source="ted", at=NOW + timedelta(days=60), force=True)
    assert [r.url.host for r in web.requests] == ["nichedb.dev"]
    assert "ted_road_transport" in events(database)


def test_bk2021_keeps_its_own_rows_and_shares_the_one_per_domain_export(web, database) -> None:
    bk = MockBK()
    feed = FeedConfig(key="daily", queries=("BESS",), interval_hours=24)
    domain = VerifiedDomain(
        nip="1234563218",
        domain="zamawiajacy.example",
        evidence_url="https://zamawiajacy.example/nip",
    )
    bk_config = TenantConfig(tenant="alpha", feeds=(feed,), verified_domains=(domain,))
    bk_now = datetime(2026, 1, 5, 12, tzinfo=UTC)
    bk_tick(bk_config, bk.client, session_factory=database, now=bk_now)
    (only,) = exported(database, at=bk_now)
    assert (only["signal_type"], only["signal_id"]) == ("bk2021", "101")

    run(web, database, source="ted", at=bk_now)
    (only,) = exported(database, at=bk_now)
    assert only["signal_type"] == "ted_road_transport"
    assert set(events(database)) == {"bk2021", "ted_road_transport"}
    with database() as db:
        assert len(db.scalars(select(Account)).all()) == 1
