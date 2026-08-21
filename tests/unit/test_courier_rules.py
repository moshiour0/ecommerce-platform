"""
Unit tests for the courier contract and settlement reconciliation.

Three properties matter here, and they fail in three different ways.

**An unknown status is never guessed.** Couriers add statuses without telling
anyone. Defaulting an unrecognised word to IN_TRANSIT hides a delivery;
defaulting it to DELIVERED consumes stock on a word nobody has read. So it
returns None and the caller parks it.

**A mapping that cannot see a delivery is refused before it is used.** A
courier file that omits DELIVERED leaves every one of its parcels dispatched
forever while the seller waits to be paid, and nothing errors — the callbacks
arrive and are politely ignored.

**A settlement mismatch is classified, never absorbed.** The courier's file is
the only evidence the platform has that it was paid. Writing down whatever it
says would make the courier the authority on what it owes.
"""

import json
from pathlib import Path

import pytest

from conftest import courier_rules

CourierStatus = courier_rules.CourierStatus
ReconcileOutcome = courier_rules.ReconcileOutcome
build_index = courier_rules.build_index
map_status = courier_rules.map_status
action_for = courier_rules.action_for
validate_mapping = courier_rules.validate_mapping
reconcile_row = courier_rules.reconcile_row
summarise_settlement = courier_rules.summarise_settlement
normalise = courier_rules.normalise

COMPLETE = {
    "PICKED_UP": ["Picked-Up", "collected"],
    "DELIVERED": ["Delivered"],
    "RETURNING": ["Returning", "RTO"],
    "RETURNED": ["Returned"],
    "IN_TRANSIT": ["In Transit"],
}

DELIVERED_ORDER = {"expected_cents": 1200, "status": "DELIVERED"}


# ---------------------------------------------------------------------------
# spelling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "Delivered", "delivered", "DELIVERED", " Delivered ", "deliv ered".replace(" ", ""),
])
def test_the_same_word_spelled_differently_maps_the_same(raw):
    # One provider sends `Delivered` from one endpoint and `delivered` from
    # another. Comparing raw strings makes the mapping a list of every
    # spelling somebody happened to observe.
    assert map_status(build_index(COMPLETE), raw) is CourierStatus.DELIVERED


def test_hyphens_and_spaces_fold_to_underscores():
    index = build_index(COMPLETE)
    for raw in ("Picked-Up", "picked up", "PICKED_UP", "Picked Up"):
        assert map_status(index, raw) is CourierStatus.PICKED_UP


def test_normalise_is_stable_on_junk():
    assert normalise(None) == ""
    assert normalise("") == ""
    assert normalise("  ") == ""


# ---------------------------------------------------------------------------
# an unknown status is never guessed
# ---------------------------------------------------------------------------

def test_an_unrecognised_word_maps_to_nothing():
    assert map_status(build_index(COMPLETE), "at_sorting_hub") is None


def test_nothing_maps_to_a_status_that_moves_stock_by_accident():
    # The failure this guards: a default that lands on DELIVERED would consume
    # stock on a word nobody has read.
    index = build_index(COMPLETE)
    for junk in ("", None, "???", "delivered_maybe", "undelivered"):
        assert map_status(index, junk) is not CourierStatus.DELIVERED


def test_an_unmapped_status_drives_no_action():
    assert action_for(None) is None


def test_an_empty_index_maps_nothing():
    assert map_status({}, "delivered") is None
    assert map_status(None, "delivered") is None


# ---------------------------------------------------------------------------
# which statuses move the order
# ---------------------------------------------------------------------------

def test_the_four_that_move_stock_or_money_drive_actions():
    assert action_for(CourierStatus.PICKED_UP) == "dispatch"
    assert action_for(CourierStatus.DELIVERED) == "deliver"
    assert action_for(CourierStatus.RETURNING) == "mark_rto"
    assert action_for(CourierStatus.RETURNED) == "complete_return"


def test_movement_updates_are_informational():
    # Recorded on the shipment, but they change nothing about the order.
    for status in (CourierStatus.IN_TRANSIT, CourierStatus.OUT_FOR_DELIVERY,
                   CourierStatus.PICKUP_PENDING):
        assert action_for(status) is None


def test_a_failed_attempt_is_not_a_return():
    # THE distinction in this file. Couriers retry two or three times before
    # giving up; treating the first failure as an RTO sends stock back for a
    # buyer who was simply out.
    assert action_for(CourierStatus.DELIVERY_FAILED) is None
    assert action_for(CourierStatus.RETURNING) == "mark_rto"


# ---------------------------------------------------------------------------
# a mapping is refused before it is used
# ---------------------------------------------------------------------------

def test_a_complete_mapping_has_no_problems():
    assert validate_mapping("good", COMPLETE) == []


def test_a_mapping_that_cannot_see_a_delivery_is_refused():
    partial = {"PICKED_UP": ["p"], "RETURNING": ["r"], "RETURNED": ["rr"]}
    problems = validate_mapping("blind", partial)
    assert problems
    assert "DELIVERED" in problems[0].detail


def test_a_mapping_that_cannot_see_a_return_is_refused():
    partial = {"PICKED_UP": ["p"], "DELIVERED": ["d"]}
    detail = validate_mapping("blind", partial)[0].detail
    assert "RETURNED" in detail and "RETURNING" in detail


def test_an_empty_mapping_is_refused():
    assert validate_mapping("empty", {})


def test_a_word_claimed_by_two_statuses_is_refused():
    # Letting the last one win would make a parcel's fate depend on dictionary
    # ordering.
    problems = validate_mapping("ambiguous",
                                {"DELIVERED": ["done"], "RETURNED": ["done"]})
    assert problems and "mapped to both" in problems[0].detail


def test_a_canonical_status_the_platform_does_not_have_is_refused():
    # The canonical side is the platform's and is fixed; providers map onto it.
    problems = validate_mapping("inventive", {"TELEPORTED": ["zap"]})
    assert problems and "not a canonical courier status" in problems[0].detail


def test_every_shipped_courier_file_is_valid():
    """The files in config/couriers must all pass, or a delivery goes missing.

    Run here rather than at start-up so a broken mapping fails the unit tier
    in a second, instead of failing quietly in production as parcels that
    never arrive.
    """
    directory = Path(__file__).resolve().parents[2] / "config" / "couriers"
    files = sorted(directory.glob("*.json"))
    assert files, "no courier mappings are configured at all"

    for path in files:
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config.get("provider"), f"{path.name} has no provider slug"
        problems = validate_mapping(config["provider"], config.get("statuses"))
        assert not problems, \
            f"{path.name}: {[p.detail for p in problems]}"


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------

def test_an_exact_remittance_matches():
    result = reconcile_row({"reference": "SO-1", "collected_cents": 1200},
                           DELIVERED_ORDER)
    assert result.ok
    assert not result.needs_attention
    assert result.variance_cents == 0


def test_a_short_payment_is_flagged_not_absorbed():
    result = reconcile_row({"reference": "SO-1", "collected_cents": 1000},
                           DELIVERED_ORDER)
    assert result.outcome is ReconcileOutcome.SHORT
    assert result.needs_attention
    assert result.variance_cents == -200


def test_an_overpayment_is_flagged_too():
    # Not a windfall. It is somebody else's money or a misattributed row, and
    # keeping it quietly is how a reconciliation stops being one.
    result = reconcile_row({"reference": "SO-1", "collected_cents": 1500},
                           DELIVERED_ORDER)
    assert result.outcome is ReconcileOutcome.OVER
    assert result.needs_attention


def test_cash_for_an_order_nobody_has_is_never_booked():
    result = reconcile_row({"reference": "SO-X", "collected_cents": 900}, None)
    assert result.outcome is ReconcileOutcome.UNKNOWN_ORDER
    assert result.needs_attention


def test_cash_for_an_order_that_was_not_delivered_is_flagged():
    # Either the delivery callback was lost or the money is misattributed.
    # Both need a person.
    result = reconcile_row({"reference": "SO-1", "collected_cents": 1200},
                           {"expected_cents": 1200, "status": "DISPATCHED"})
    assert result.outcome is ReconcileOutcome.NOT_DELIVERED
    assert result.needs_attention


def test_a_resent_row_is_a_no_op_rather_than_a_second_payout():
    # Couriers resend whole files after a correction. Paying the seller twice
    # is the failure this prevents.
    result = reconcile_row({"reference": "SO-1", "collected_cents": 1200},
                           DELIVERED_ORDER, already_settled=True)
    assert result.outcome is ReconcileOutcome.DUPLICATE
    assert not result.needs_attention


@pytest.mark.parametrize("row", [
    {"collected_cents": 100},
    {"reference": "", "collected_cents": 100},
    {"reference": "SO-1"},
    {"reference": "SO-1", "collected_cents": "lots"},
    {"reference": "SO-1", "collected_cents": None},
])
def test_an_unusable_row_is_invalid_rather_than_zero(row):
    # Reading a missing amount as zero would silently book a delivery as
    # having collected nothing.
    assert reconcile_row(row, DELIVERED_ORDER).outcome is ReconcileOutcome.INVALID


def test_a_negative_remittance_is_refused():
    result = reconcile_row({"reference": "SO-1", "collected_cents": -50},
                           DELIVERED_ORDER)
    assert result.outcome is ReconcileOutcome.INVALID


# ---------------------------------------------------------------------------
# the run summary
# ---------------------------------------------------------------------------

def test_the_summary_counts_what_needs_a_person():
    results = [
        reconcile_row({"reference": "A", "collected_cents": 1200}, DELIVERED_ORDER),
        reconcile_row({"reference": "B", "collected_cents": 1000}, DELIVERED_ORDER),
        reconcile_row({"reference": "C", "collected_cents": 1200},
                      DELIVERED_ORDER, already_settled=True),
    ]
    summary = summarise_settlement(results)
    assert summary["rows"] == 3
    assert summary["matched"] == 1
    assert summary["needs_attention"] == 1     # the duplicate is benign


def test_variance_does_not_net_a_shortfall_against_an_overpayment():
    # A file short on one order and over on another is not balanced. Netting
    # them to zero hides two problems.
    results = [
        reconcile_row({"reference": "A", "collected_cents": 1000}, DELIVERED_ORDER),
        reconcile_row({"reference": "B", "collected_cents": 1400}, DELIVERED_ORDER),
    ]
    summary = summarise_settlement(results)
    assert summary["needs_attention"] == 2
    assert summary["outcomes"]["short"] == 1
    assert summary["outcomes"]["over"] == 1


def test_an_empty_run_summarises_cleanly():
    summary = summarise_settlement([])
    assert summary["rows"] == 0 and summary["needs_attention"] == 0
