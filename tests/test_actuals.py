"""Actual production reporting and reconciliation rules."""

from __future__ import annotations

import threading

import pytest

from app.actuals import evaluate_actuals
from app.errors import ActualsValidationError, CsvParseError
from app.service import ReconciliationService

from .conftest import FORECAST_CSV, MAPPING_CSV, upload_text

# Lines: L1, L2 under S1; L3 under S2.  Base 2026-01: L1=10, L2=20, L3=30.
PUBLISHED_CSV = """node_id,level,period,value
S1,,2026-01,40
"""


def _published(service, ds):
    upload_text(service, ds, "mapping", MAPPING_CSV)
    upload_text(service, ds, "line_forecast", FORECAST_CSV)
    upload_text(service, ds, "published", PUBLISHED_CSV)


def _snapshot(service, ds) -> int:
    preview = service.preview(ds)
    return service.publish(ds, preview["preview_id"])["version_number"]


@pytest.fixture()
def snapshot(service, dataset_id):
    _published(service, dataset_id)
    return _snapshot(service, dataset_id)


def submit(service, ds, version, text, period="2026-01", filename="actuals.csv"):
    return service.submit_actuals(
        ds, version, period, filename, text.encode("utf-8")
    )


def totals(service, ds, version, period="2026-01"):
    stored = service.db.get_actual_reconciliation(ds, version, period)
    return stored["result"] if stored else None


# ----------------------------------------------------------------------
# Parsing / validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "node_id,quantity\nL1,-1\n",
        "node_id,quantity\nL1,2.5\n",
        "node_id,quantity\nL1,abc\n",
        "node_id,quantity\n,5\n",
        "node_id,quantity\n",
    ],
)
def test_invalid_actuals_files_are_rejected_without_state_change(
    service, dataset_id, snapshot, text
):
    with pytest.raises((CsvParseError, ActualsValidationError)):
        submit(service, dataset_id, snapshot, text)
    assert totals(service, dataset_id, snapshot) is None
    assert service.db.actual_values(dataset_id, snapshot, "2026-01") == {}


def test_unknown_and_wrong_level_nodes_are_rejected(service, dataset_id, snapshot):
    with pytest.raises(ActualsValidationError):
        submit(service, dataset_id, snapshot, "node_id,quantity\nL9,5\n")
    with pytest.raises(ActualsValidationError):
        submit(service, dataset_id, snapshot, "node_id,quantity\nP1,5\n")
    assert totals(service, dataset_id, snapshot) is None


def test_alias_value_column_accepted(service, dataset_id, snapshot):
    outcome = submit(service, dataset_id, snapshot, "node_id,value\nL1,12\n")
    assert outcome["reconciliation"]["items"][0]["actual"] == 12


# ----------------------------------------------------------------------
# Reconciliation rules
# ----------------------------------------------------------------------
def test_reported_line_is_reconciled_on_its_own(service, dataset_id, snapshot):
    recon = submit(service, dataset_id, snapshot, "node_id,quantity\nL1,12\n")[
        "reconciliation"
    ]
    item = next(i for i in recon["items"] if i["kind"] == "line")
    assert item["node_id"] == "L1"
    assert item["actual"] == 12
    assert item["covered_lines"] == ["L1"]
    assert item["base_error"] == abs(12 - 10)
    # Corrected forecast comes from the published snapshot (S1 pinned to 40).
    snap = service.db.get_snapshot(dataset_id, snapshot)["result"]["periods"][0]
    assert item["corrected_forecast"] == pytest.approx(snap["corrected"]["L1"])
    assert item["corrected_error"] == pytest.approx(
        abs(12 - item["corrected_forecast"])
    )


def test_shop_total_remainder_against_unreported_lines(service, dataset_id, snapshot):
    recon = submit(service, dataset_id, snapshot, "node_id,quantity\nS1,45\nL1,15\n")[
        "reconciliation"
    ]
    remainder_item = next(i for i in recon["items"] if i["kind"] == "shop_remainder")
    assert remainder_item["node_id"] == "S1"
    assert remainder_item["actual"] == 45 - 15  # total minus reported line actual
    assert remainder_item["covered_lines"] == ["L2"]  # only unreported lines
    assert remainder_item["base_forecast"] == 20  # L2 base forecast
    shop = next(s for s in recon["shops"] if s["shop_id"] == "S1")
    assert shop["status"] == "total_reported"
    assert shop["remainder"] == 30
    # S2 has no total: nothing is reported, so its lines are missing data.
    assert recon["missing_lines"] == ["L3"]
    assert all(i["node_id"] != "L3" for i in recon["items"])


def test_shop_without_total_marks_unreported_lines_missing(service, dataset_id, snapshot):
    recon = submit(service, dataset_id, snapshot, "node_id,quantity\nL1,11\n")[
        "reconciliation"
    ]
    assert recon["missing_lines"] == ["L2", "L3"]
    shop = next(s for s in recon["shops"] if s["shop_id"] == "S1")
    assert shop["status"] == "no_total"
    assert shop["remainder"] is None


def test_negative_remainder_rejects_whole_batch(service, dataset_id, snapshot):
    submit(service, dataset_id, snapshot, "node_id,quantity\nS1,30\nL1,10\n")
    before = totals(service, dataset_id, snapshot)
    with pytest.raises(ActualsValidationError) as info:
        # L1 already reported 10; a new total of 5 would leave -5.
        submit(service, dataset_id, snapshot, "node_id,quantity\nS1,5\n")
    assert info.value.details["conflicts"][0]["shop_id"] == "S1"
    assert info.value.details["conflicts"][0]["reason"] == "negative_remainder"
    assert totals(service, dataset_id, snapshot) == before
    assert service.db.actual_values(dataset_id, snapshot, "2026-01")["S1"][
        "quantity"
    ] == 30


def test_fully_reported_shop_mismatch_rejects_batch(service, dataset_id, snapshot):
    with pytest.raises(ActualsValidationError) as info:
        submit(
            service,
            dataset_id,
            snapshot,
            "node_id,quantity\nS1,100\nL1,10\nL2,20\n",
        )
    conflict = info.value.details["conflicts"][0]
    assert conflict["reason"] == "fully_reported_mismatch"
    assert conflict["shop_id"] == "S1"
    assert conflict["remainder"] == 70
    assert totals(service, dataset_id, snapshot) is None


def test_fully_reported_shop_matching_total_is_accepted(service, dataset_id, snapshot):
    recon = submit(
        service, dataset_id, snapshot, "node_id,quantity\nS1,31\nL1,11\nL2,20\n"
    )["reconciliation"]
    shop = next(s for s in recon["shops"] if s["shop_id"] == "S1")
    assert shop["status"] == "fully_reported"
    assert shop["remainder"] == 0
    assert {i["node_id"] for i in recon["items"]} == {"L1", "L2"}


def test_later_batch_corrects_earlier_value(service, dataset_id, snapshot):
    submit(service, dataset_id, snapshot, "node_id,quantity\nS1,50\nL1,10\n")
    first = totals(service, dataset_id, snapshot)
    outcome = submit(service, dataset_id, snapshot, "node_id,quantity\nL1,15\n")
    item = next(i for i in outcome["reconciliation"]["items"] if i["node_id"] == "L1")
    assert item["actual"] == 15
    assert outcome["reconciliation"]["totals"] != first["totals"]
    assert service.db.actual_values(dataset_id, snapshot, "2026-01")["L1"][
        "quantity"
    ] == 15


def test_repeated_id_within_batch_last_row_wins(service, dataset_id, snapshot):
    recon = submit(service, dataset_id, snapshot, "node_id,quantity\nL1,9\nL1,13\n")[
        "reconciliation"
    ]
    item = next(i for i in recon["items"] if i["node_id"] == "L1")
    assert item["actual"] == 13


def test_error_summary_uses_same_items_for_both_forecasts(service, dataset_id, snapshot):
    recon = submit(service, dataset_id, snapshot, "node_id,quantity\nS1,50\nL1,15\n")[
        "reconciliation"
    ]
    items = recon["items"]
    assert recon["totals"]["item_count"] == len(items)
    assert recon["totals"]["base_error"] == pytest.approx(
        sum(i["base_error"] for i in items)
    )
    assert recon["totals"]["corrected_error"] == pytest.approx(
        sum(i["corrected_error"] for i in items)
    )
    assert recon["totals"]["improvement"] == pytest.approx(
        recon["totals"]["base_error"] - recon["totals"]["corrected_error"]
    )
    # The corrected forecast is really the snapshot's corrected value: a line
    # item equals its line, the shop-remainder item equals the unreported
    # lines' corrected sum.
    corrected = service.db.get_snapshot(dataset_id, snapshot)["result"]["periods"][0][
        "corrected"
    ]
    for item in items:
        if item["kind"] == "line":
            assert item["corrected_forecast"] == pytest.approx(
                corrected[item["node_id"]]
            )
        else:
            assert item["corrected_forecast"] == pytest.approx(
                sum(corrected[line] for line in item["covered_lines"])
            )


def test_success_updates_detail_and_summary_together(service, dataset_id, snapshot):
    outcome = submit(service, dataset_id, snapshot, "node_id,quantity\nL1,12\n")
    stored = totals(service, dataset_id, snapshot)
    assert stored["items"] == outcome["reconciliation"]["items"]
    assert stored["totals"] == outcome["reconciliation"]["totals"]
    batches = service.db.list_actual_batches(dataset_id, snapshot, "2026-01")
    assert batches[0]["status"] == "applied"


def test_rejection_is_recorded_as_batch_but_keeps_state(service, dataset_id, snapshot):
    with pytest.raises(ActualsValidationError):
        submit(service, dataset_id, snapshot, "node_id,quantity\nL9,3\n")
    batches = service.db.list_actual_batches(dataset_id, snapshot, "2026-01")
    assert batches[0]["status"] == "rejected"
    assert totals(service, dataset_id, snapshot) is None


def test_period_must_exist_in_snapshot(service, dataset_id, snapshot):
    with pytest.raises(ActualsValidationError):
        submit(service, dataset_id, snapshot, "node_id,quantity\nL1,1\n", period="2099-01")


def test_actuals_are_scoped_per_snapshot_version(service, dataset_id, snapshot):
    submit(service, dataset_id, snapshot, "node_id,quantity\nL1,12\n")
    second = _snapshot(service, dataset_id)
    assert second != snapshot
    assert totals(service, dataset_id, second) is None
    assert service.db.actual_values(dataset_id, second, "2026-01") == {}


# ----------------------------------------------------------------------
# Pure evaluation and concurrency
# ----------------------------------------------------------------------
def test_evaluate_actuals_pure_logic(service, dataset_id):
    from app.hierarchy import build_hierarchy
    from app.csv_io import MappingRow

    rows = [
        MappingRow("P", None, "plant", "p", 1),
        MappingRow("S1", "P", "shop", "s1", 1),
        MappingRow("L1", "S1", "line", "l1", 1),
        MappingRow("L2", "S1", "line", "l2", 1),
    ]
    h = build_hierarchy(rows)
    base = {"P": 30, "S1": 30, "L1": 10, "L2": 20}
    corrected = {"P": 40, "S1": 40, "L1": 15, "L2": 25}
    result = evaluate_actuals(h, base, corrected, {"L1": 12}, {"S1": 35})
    item = next(i for i in result["items"] if i["kind"] == "shop_remainder")
    assert item["actual"] == 23
    assert item["base_forecast"] == 20
    assert item["corrected_forecast"] == 25


def test_concurrent_batches_are_serialised_and_never_mix(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "concurrent.db")
    service = ReconciliationService(db)
    ds = service.create_dataset("并发实产").id
    _published(service, ds)
    version = _snapshot(service, ds)

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def report_s1():
        svc = ReconciliationService(db)
        try:
            barrier.wait(timeout=10)
            submit(svc, ds, version, "node_id,quantity\nS1,50\nL1,10\n")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def report_l3():
        svc = ReconciliationService(db)
        try:
            barrier.wait(timeout=10)
            submit(svc, ds, version, "node_id,quantity\nL3,25\n")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=report_s1), threading.Thread(target=report_l3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors
    values = db.actual_values(ds, version, "2026-01")
    # Both batches landed and the final reconciliation reflects the full stored
    # state, never a half-merged mixture — whichever order the lock granted.
    assert values["S1"]["quantity"] == 50
    assert values["L1"]["quantity"] == 10
    assert values["L3"]["quantity"] == 25
    stored = totals(service, ds, version)
    assert stored["actuals"]["S1"] == 50
    assert stored["actuals"]["L1"] == 10
    assert stored["actuals"]["L3"] == 25
    assert {i["node_id"] for i in stored["items"]} == {"L1", "S1", "L3"}


def test_failed_concurrent_batch_keeps_successful_state(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "concurrent2.db")
    service = ReconciliationService(db)
    ds = service.create_dataset("并发退回").id
    _published(service, ds)
    version = _snapshot(service, ds)
    good = submit(service, ds, version, "node_id,quantity\nS1,50\nL1,10\n")
    before = totals(service, ds, version)

    errors: list[BaseException] = []

    def bad_batch():
        svc = ReconciliationService(db)
        try:
            submit(svc, ds, version, "node_id,quantity\nS1,1\n")
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=bad_batch)
    t.start()
    t.join(timeout=20)

    assert len(errors) == 1
    assert totals(service, ds, version) == before
    assert db.actual_values(ds, version, "2026-01")["S1"]["quantity"] == 50
    assert good["reconciliation"]["totals"]["item_count"] == 2
