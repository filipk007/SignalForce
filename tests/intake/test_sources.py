"""Pure parsing and qualification of TED notices and the RWDZ file."""

import pytest

from scripts.intake.http import SourceError
from scripts.intake.models import external_key, public_text
from scripts.intake.rwdz import COLUMNS, read_zip
from scripts.intake.ted import fold, parse_notice
from tests.intake.conftest import CASE, NOW, PROCEDURE, RWDZ, TED, eform, row, rwdz_zip


def signals(*xmls: str):
    return fold([parse_notice(xml.encode()) for xml in xmls], TED, NOW)


def test_notice_supplies_scope_deadline_buyer_and_no_contact_data() -> None:
    (signal,) = signals(eform())
    assert (signal.native_id, signal.status, signal.source_version) == (
        PROCEDURE,
        "open",
        "100-2026",
    )
    assert signal.deadline.isoformat() == "2026-02-20T10:00:00+01:00"
    assert (signal.company_name, signal.company_ref, signal.company_role) == (
        "Przykładowa Agencja",
        "1234563218",
        "buyer",
    )
    assert signal.published_on.isoformat() == "2026-01-15"
    assert "60100000" in signal.evidence[0].excerpt and "LOT-0001" in signal.evidence[0].locator
    assert "osoba@example.test" not in signal.model_dump_json()


@pytest.mark.parametrize("override", [{"cpv": "60400000"}, {"country": "DEU"}])
def test_scope_outside_polish_road_transport_is_ignored(override: dict[str, str]) -> None:
    assert signals(eform(**override)) == ()


def test_missing_deadline_stays_a_review_candidate() -> None:
    (signal,) = signals(eform(deadline=None))
    assert signal.status == "review" and signal.deadline is None
    assert signal.intent_status(NOW) == "Review"


def test_confirmed_correction_updates_the_same_procedure() -> None:
    correction = eform(
        "200-2026", published="2026-01-18", deadline="2026-03-01", changes="100-2026"
    )
    (signal,) = signals(correction, eform())
    assert (signal.native_id, signal.source_version) == (PROCEDURE, "200-2026")
    assert signal.deadline.isoformat().startswith("2026-03-01")
    assert signal.published_on.isoformat() == "2026-01-15"
    assert signal.updated_on.isoformat() == "2026-01-18"


def test_unconfirmed_correction_is_kept_as_its_own_candidate() -> None:
    stray = eform("200-2026", published="2026-01-18", deadline="2026-03-01", changes="999-2026")
    original, candidate = signals(eform(), stray)
    assert original.source_version == "100-2026" and original.status == "open"
    assert (candidate.native_id, candidate.status) == ("pub:200-2026", "review")


@pytest.mark.parametrize(
    ("reason", "status"), [("cancel", "cancelled"), ("cancel-intent", "review")]
)
def test_cancellation_change_notices(reason: str, status: str) -> None:
    change = eform("200-2026", published="2026-01-18", changes="100-2026", reason=reason)
    assert signals(eform(), change)[0].status == status


@pytest.mark.parametrize(("code", "status"), [("selec-w", "closed"), ("open-nw", "open")])
def test_result_notice_closes_only_decided_lots(code: str, status: str) -> None:
    result = eform(
        "300-2026", kind="can-standard", published="2026-01-19", results=(("LOT-0001", code),)
    )
    assert signals(eform(), result)[0].status == status


def test_expired_deadline_is_history() -> None:
    (signal,) = signals(eform(deadline="2026-01-19"))
    assert signal.status == "open" and signal.intent_status(NOW) == "Expired"


def test_external_key_fits_the_column_and_stays_stable() -> None:
    assert external_key("ted-pl", PROCEDURE) == f"ted-pl:{PROCEDURE}"
    long = external_key("rwdz-wielkopolskie", "x" * 80)
    assert len(long) <= 64 and long == external_key("rwdz-wielkopolskie", "x" * 80)


def test_rwdz_qualifies_grouped_expansion_and_drops_personal_fields() -> None:
    rows = [
        row(),
        row(numer_dzialki="1/2"),
        row(numer_gunb="OLD", data_wydania_decyzji="2020-01-10 00:00:00"),
        row(numer_gunb="HOUSE", kategoria="I"),
    ]
    result = read_zip(rwdz_zip(rows), RWDZ, NOW, tracked=frozenset())
    assert (result.rows, result.cases_in_window) == (4, 2)
    (signal,) = result.signals
    assert (signal.native_id, signal.status, signal.company_role) == (CASE, "review", "investor")
    assert signal.company_name == "Przykładowy Zakład Sp. z o.o."
    assert signal.occurred_on.isoformat() == "2026-01-10" and signal.deadline is None
    assert "wiersze 2, 3" in signal.evidence[0].locator
    dumped = signal.model_dump_json()
    assert "Nieujawniany" not in dumped and "1/2" not in dumped


def test_rwdz_natural_person_investor_is_not_stored() -> None:
    (signal,) = read_zip(
        rwdz_zip([row(nazwa_inwestor="Jan Przykładowy")]), RWDZ, NOW, tracked=frozenset()
    ).signals
    assert signal.company_name == "" and "Jan Przykładowy" not in signal.model_dump_json()
    assert any("inwestor" in reason for reason in signal.review_reasons)


def test_rwdz_conflicting_rows_of_one_case_go_to_review() -> None:
    rows = [row(), row(numer_decyzji_urzedu="11/26")]
    (signal,) = read_zip(rwdz_zip(rows), RWDZ, NOW, tracked=frozenset()).signals
    assert any("różne" in reason for reason in signal.review_reasons)


@pytest.mark.parametrize(
    "payload",
    [
        b"not a zip",
        rwdz_zip([row()])[:-30],
        rwdz_zip([row()], header=COLUMNS[:-1] + ("inna",)),
    ],
)
def test_rwdz_rejects_a_broken_or_changed_file(payload: bytes) -> None:
    with pytest.raises(SourceError):
        read_zip(payload, RWDZ, NOW, tracked=frozenset())


def test_redaction_keeps_case_numbers_and_removes_contacts() -> None:
    text = "zmiana dec. AB.6740.10.392.2025.XVI, tel. 600 700 800, +48600700800, jan@example.test"
    cleaned = public_text(text)
    assert "AB.6740.10.392.2025.XVI" in cleaned
    assert "600" not in cleaned.replace("6740", "") and "jan@" not in cleaned
