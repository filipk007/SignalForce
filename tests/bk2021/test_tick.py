from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scripts.bk2021.export import export_csv
from scripts.bk2021.models import Announcement, FeedConfig, TenantConfig, VerifiedDomain
from scripts.bk2021.runner import SessionFactory, reassign, status, tick
from scripts.bk2021.store import Bindings
from scripts.scoring.wiring import load_signal_inputs
from scripts.storage.models import Account, Base, ScanRun, SignalEvent, SignalFeed, Tenant
from tests.bk2021.conftest import NOW, MockBK, detail

FAST = FeedConfig(key="fast", queries=("BESS",), interval_hours=2)
DAILY = FeedConfig(key="daily", queries=("magazyn energii",), interval_hours=24)
BINDING = VerifiedDomain(
    nip="1234563218", domain="www.przyklad.example", evidence_url="https://przyklad.example/nip"
)
CONFIG = TenantConfig(tenant="alpha", feeds=(FAST, DAILY), verified_domains=(BINDING,))


@pytest.fixture
def database() -> Iterator[SessionFactory]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def session() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as db, db.begin():
            yield db

    with session() as db:
        db.add_all(
            [Tenant(id=1, slug="alpha", name="Alpha"), Tenant(id=2, slug="beta", name="Beta")]
        )
    yield session
    engine.dispose()


def test_independent_feed_schedules_and_tenant_isolation(
    database: SessionFactory, bk: MockBK
) -> None:
    assert [
        (r.key, r.state) for r in tick(CONFIG, bk.client, session_factory=database, now=NOW)
    ] == [("fast", "completed"), ("daily", "completed")]
    next_tick = NOW + timedelta(hours=2)
    assert [
        (r.key, r.state) for r in tick(CONFIG, bk.client, session_factory=database, now=next_tick)
    ] == [("fast", "completed"), ("daily", "not_due")]
    beta = CONFIG.model_copy(update={"tenant": "beta", "verified_domains": ()})
    assert all(
        r.state == "completed" for r in tick(beta, bk.client, session_factory=database, now=NOW)
    )
    with database() as db:
        events = db.scalars(select(SignalEvent).order_by(SignalEvent.tenant_id)).all()
        assert [(e.tenant_id, e.external_id, e.account_id is None) for e in events] == [
            (1, "101", False),
            (2, "101", True),
        ]
        assert len(db.scalars(select(Account)).all()) == 1
    alpha_status = status(CONFIG, session_factory=database, now=next_tick)
    assert {r.key: r.last_success for r in alpha_status.feeds} == {"fast": next_tick, "daily": NOW}
    assert status(beta, session_factory=database, now=NOW).unassigned[0].external_id == "101"


def test_partial_refresh_does_not_commit_and_retry_is_due(
    database: SessionFactory, bk: MockBK
) -> None:
    tick(CONFIG, bk.client, session_factory=database, now=NOW, feed_key="fast")
    bk.details = {101: detail(version=502), 102: 429}
    bk.search = lambda p: ([101], 1) if p["query"] == "magazyn energii" else ([101, 102], 2)
    later = NOW + timedelta(hours=2)
    results = tick(CONFIG, bk.client, session_factory=database, now=later)
    assert [r.state for r in results] == ["failed", "completed"]
    assert "HTTP 429" in results[0].error
    snapshot = status(CONFIG, session_factory=database, now=later)
    assert {r.key: r.last_success for r in snapshot.feeds} == {"fast": NOW, "daily": later}
    with database() as db:
        runs = db.scalars(select(ScanRun).order_by(ScanRun.id)).all()
        assert [(r.feed_key, r.status) for r in runs] == [
            ("fast", "completed"),
            ("fast", "failed"),
            ("daily", "completed"),
        ]
        assert [row.external_id for row in db.scalars(select(SignalEvent))] == ["101"]
    bk.details[102] = detail(102)
    retry = tick(CONFIG, bk.client, session_factory=database, now=later + timedelta(seconds=30))
    assert [r.state for r in retry] == ["completed", "not_due"]
    with database() as db:
        assert len(db.scalars(select(SignalEvent)).all()) == 2


def test_replay_amend_and_refresh_after_search_disappearance(
    database: SessionFactory, bk: MockBK
) -> None:
    def run(at: datetime) -> None:
        results = tick(CONFIG, bk.client, session_factory=database, now=at, force=True)
        assert all(result.state == "completed" for result in results)

    run(NOW)
    with database() as db:
        event = db.scalars(select(SignalEvent)).one()
        first_id, first_detected, account_id = event.id, event.detected_at, event.account_id
        assert account_id is not None
        assert (
            len(
                load_signal_inputs(
                    1, account_id, NOW - timedelta(days=90), db, warned=set(), now=NOW
                )
            )
            == 1
        )
    run(NOW + timedelta(minutes=1))
    bk.details[101] = detail(version=502, deadline="2026-01-12 13:30:00")
    bk.search = lambda _: ([], 0)
    run(NOW + timedelta(minutes=2))
    bk.details[101] = detail(version=501)  # stale response must not overwrite canonical version
    run(NOW + timedelta(minutes=3))
    with database() as db:
        event = db.scalars(select(SignalEvent)).one()
        assert (event.id, event.detected_at) == (first_id, first_detected)
        assert event.payload["version_id"] == 502
        assert event.payload["first_published"] == "2026-01-03"
        assert event.payload["deadline"] == "2026-01-12T13:30:00+01:00"
        assert "250–270 kWh" in event.payload["evidence"][0]
        assert (
            len(
                load_signal_inputs(
                    1, account_id, NOW - timedelta(days=90), db, warned=set(), now=NOW
                )
            )
            == 1
        )
        db.add(SignalEvent(tenant_id=1, signal_type="bk2021", external_id="101"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    bk.details[101] = detail(version=502, status="CANCELLED", deadline=None)
    run(NOW + timedelta(minutes=4))
    with database() as db:
        event = db.scalars(select(SignalEvent)).one()
        assert (event.id, event.account_id, event.payload["status"]) == (
            first_id,
            account_id,
            "CANCELLED",
        )
        assert not load_signal_inputs(
            1, account_id, NOW - timedelta(days=90), db, warned=set(), now=NOW
        )


def test_new_version_out_of_scope_and_time_expiration_exclude_intent(
    database: SessionFactory, bk: MockBK
) -> None:
    tick(CONFIG, bk.client, session_factory=database, now=NOW)
    deadline = datetime(2026, 1, 10, 22, 59, 59, tzinfo=UTC)
    assert "przyklad.example" in export_csv(CONFIG, session_factory=database, now=NOW)
    assert "przyklad.example" not in export_csv(CONFIG, session_factory=database, now=deadline)
    bk.details[101] = detail(version=503, description="Dostawa paneli fotowoltaicznych.")
    tick(CONFIG, bk.client, session_factory=database, now=NOW + timedelta(hours=2))
    assert "przyklad.example" not in export_csv(CONFIG, session_factory=database, now=NOW)
    with database() as db:
        event = db.scalars(select(SignalEvent)).one()
        assert event.payload["version_id"] == 503
        assert event.payload["qualified"] is False
        assert "250–270 kWh" in event.payload["evidence"][0]


def test_reassignment_uses_only_verified_nip_and_needs_no_requests(
    database: SessionFactory, bk: MockBK
) -> None:
    unmapped = CONFIG.model_copy(update={"verified_domains": ()})
    bk.details[102] = detail(102, nip="invalid")
    tick(unmapped, bk.client, session_factory=database, now=NOW)
    assert [
        r.external_id for r in status(unmapped, session_factory=database, now=NOW).unassigned
    ] == ["101", "102"]
    requests = len(bk.requests)
    assert reassign(CONFIG, session_factory=database, now=NOW) == 1
    assert [
        r.external_id for r in status(CONFIG, session_factory=database, now=NOW).unassigned
    ] == ["102"]
    assert reassign(unmapped, session_factory=database, now=NOW) == 1
    assert len(bk.requests) == requests


def test_export_selects_latest_publication_once_per_domain(
    database: SessionFactory, bk: MockBK
) -> None:
    import csv
    from io import StringIO

    bk.details[102] = detail(102)
    bk.details[102]["advertisement"]["advertisement"]["publication_date"] = "2026-01-04"
    tick(CONFIG, bk.client, session_factory=database, now=NOW)
    rows = list(csv.DictReader(StringIO(export_csv(CONFIG, session_factory=database, now=NOW))))
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == [
        "domain",
        "company_name",
        "nip",
        "signal_type",
        "signal_id",
        "signal_date",
        "signal_url",
        "deadline",
        "why_now",
    ]
    assert (row["signal_id"], row["signal_date"], row["nip"]) == ("102", "2026-01-04", BINDING.nip)
    assert "250–270 kWh" in row["why_now"]


def test_failed_write_rolls_back_events_and_membership(
    database: SessionFactory, bk: MockBK, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from scripts.bk2021 import runner
    from scripts.storage.models import BK2021FeedMember

    bk.details[102] = detail(102)
    save_announcement = runner.save_announcement

    def fail_second(
        session: Session,
        feed: SignalFeed,
        config: FeedConfig,
        incoming: Announcement,
        bindings: Bindings,
        now: datetime,
        run_id: int,
    ) -> tuple[bool, set[int]]:
        if incoming.source_id == 102:
            raise SQLAlchemyError("mock write failure")
        return save_announcement(session, feed, config, incoming, bindings, now, run_id)

    monkeypatch.setattr(runner, "save_announcement", fail_second)
    assert (
        tick(CONFIG, bk.client, session_factory=database, now=NOW, feed_key="fast")[0].state
        == "failed"
    )
    assert status(CONFIG, session_factory=database, now=NOW).feeds[0].last_success is None
    with database() as db:
        assert not db.scalars(select(SignalEvent)).all()
        assert not db.scalars(select(BK2021FeedMember)).all()
        assert not db.scalars(select(Account)).all()
        assert db.scalars(select(ScanRun)).one().status == "failed"


def test_database_allows_unresolved_stable_ids_from_other_sources(database: SessionFactory) -> None:
    with database() as db:
        db.add(SignalEvent(tenant_id=1, signal_type="synthetic_feed", external_id="external-7"))
    with database() as db:
        assert db.scalars(select(SignalEvent)).one().account_id is None
    with pytest.raises(IntegrityError), database() as db:
        db.add(SignalEvent(tenant_id=1, signal_type="hiring"))
        db.flush()
