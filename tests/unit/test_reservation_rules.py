"""
Unit tests for the inventory reservation decision.

Stock is the one quantity here that must never go negative. Overselling five
units in a flash sale means five unfulfillable orders and five refunds;
under-selling merely disappoints. These tests pin the arithmetic and the
boundaries; tests/integration/inventory_contention.sh proves the lock actually
serialises concurrent buyers.

Run:  python -m pytest tests/unit -q
"""

import pytest

from conftest import reservation_rules, saga_transitions

StockLevel = reservation_rules.StockLevel
ReservationOutcome = reservation_rules.ReservationOutcome
plan_reservation = reservation_rules.plan_reservation
EVENT_RESERVED = reservation_rules.EVENT_RESERVED
EVENT_FAILED = reservation_rules.EVENT_FAILED


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def test_reserving_moves_units_between_columns():
    plan = plan_reservation(StockLevel(10, 0), 3)
    assert plan.outcome is ReservationOutcome.RESERVED
    assert plan.new_level == StockLevel(quantity_available=7, quantity_reserved=3)


@pytest.mark.parametrize("available,reserved,want", [
    (10, 0, 1), (10, 0, 10), (100, 50, 25), (1, 999, 1), (5, 5, 5),
])
def test_total_stock_is_conserved(available, reserved, want):
    """Reserving never creates or destroys units."""
    before = StockLevel(available, reserved)
    plan = plan_reservation(before, want)
    assert plan.new_level.total == before.total, "stock appeared or vanished"


def test_available_never_goes_negative():
    for available in range(0, 6):
        for want in range(1, 8):
            plan = plan_reservation(StockLevel(available, 0), want)
            if plan.new_level is not None:
                assert plan.new_level.quantity_available >= 0, \
                    f"oversold: {available} available, {want} requested"


def test_reserved_only_ever_increases_on_success():
    before = StockLevel(10, 4)
    plan = plan_reservation(before, 3)
    assert plan.new_level.quantity_reserved > before.quantity_reserved


# ---------------------------------------------------------------------------
# boundaries -- where off-by-one becomes an oversell
# ---------------------------------------------------------------------------

def test_requesting_exactly_all_remaining_stock_succeeds():
    plan = plan_reservation(StockLevel(5, 0), 5)
    assert plan.outcome is ReservationOutcome.RESERVED
    assert plan.new_level == StockLevel(0, 5)


def test_requesting_one_more_than_available_fails():
    plan = plan_reservation(StockLevel(5, 0), 6)
    assert plan.outcome is ReservationOutcome.INSUFFICIENT
    assert plan.new_level is None, "a failed reservation must not alter stock"


def test_zero_stock_refuses_everything():
    plan = plan_reservation(StockLevel(0, 12), 1)
    assert plan.outcome is ReservationOutcome.INSUFFICIENT


def test_already_reserved_units_are_not_available_again():
    """quantity_reserved is committed elsewhere; only available may be sold."""
    plan = plan_reservation(StockLevel(2, 98), 3)
    assert plan.outcome is ReservationOutcome.INSUFFICIENT, \
        "reserved units were treated as sellable"


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

def test_missing_product_is_not_found_not_zero_stock():
    """Auto-creating stock for an unknown product is the backdoor that made a
    flash sale impossible to sell out."""
    plan = plan_reservation(None, 1)
    assert plan.outcome is ReservationOutcome.NOT_FOUND
    assert plan.new_level is None
    assert plan.event is None, "a missing product is not a saga outcome"


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_non_positive_quantities_are_rejected(bad):
    plan = plan_reservation(StockLevel(10, 0), bad)
    assert plan.outcome is ReservationOutcome.INVALID
    assert plan.new_level is None


def test_a_zero_quantity_request_cannot_silently_succeed():
    """Reserving nothing while reporting success would let a saga proceed to
    payment with no stock held."""
    assert plan_reservation(StockLevel(10, 0), 0).reserved is False


def test_insufficient_stock_emits_rather_than_going_silent():
    """Silence here stranded every oversubscribed order at PENDING until the
    reaper swept it fifteen minutes later."""
    plan = plan_reservation(StockLevel(1, 0), 5)
    assert plan.event == EVENT_FAILED, "the saga would never learn stock ran out"


def test_successful_reservation_emits_reserved():
    assert plan_reservation(StockLevel(5, 0), 1).event == EVENT_RESERVED


# ---------------------------------------------------------------------------
# contract with order-saga
# ---------------------------------------------------------------------------

def test_both_events_are_known_to_the_saga():
    known = set(saga_transitions.KNOWN_EVENT_TYPES)
    for event in (EVENT_RESERVED, EVENT_FAILED):
        assert event in known, f"{event} unknown to order-saga"


def test_both_events_actually_move_the_saga():
    """A known event with no transition is answered 409 forever."""
    with_transitions = {e for (_s, e) in saga_transitions.TRANSITIONS}
    for event in (EVENT_RESERVED, EVENT_FAILED):
        assert event in with_transitions, f"{event} has no saga transition"


def test_the_two_outcomes_lead_somewhere_different():
    """Success and failure must not collapse onto the same saga path."""
    t = saga_transitions.TRANSITIONS
    reserved_targets = {d for (_s, e), (d, _c) in t.items() if e == EVENT_RESERVED}
    failed_targets = {d for (_s, e), (d, _c) in t.items() if e == EVENT_FAILED}
    assert reserved_targets and failed_targets
    assert reserved_targets != failed_targets


# ---------------------------------------------------------------------------
# sequential drain -- the flash sale, without concurrency
# ---------------------------------------------------------------------------

def test_stock_drains_to_exactly_zero_and_then_refuses():
    """Twenty buyers, five units, one each. Five win and the rest are refused,
    with no unit sold twice. The concurrent version of this is in
    tests/integration/inventory_contention.sh."""
    level = StockLevel(5, 0)
    granted = 0
    for _ in range(20):
        plan = plan_reservation(level, 1)
        if plan.reserved:
            granted += 1
            level = plan.new_level

    assert granted == 5, f"{granted} buyers served from 5 units"
    assert level == StockLevel(0, 5)
    assert level.total == 5, "total stock changed while draining"


def test_partial_quantities_cannot_oversell_the_last_unit():
    level = StockLevel(4, 0)
    assert plan_reservation(level, 3).reserved is True
    level = plan_reservation(level, 3).new_level      # 1 left
    assert plan_reservation(level, 2).reserved is False, "sold 2 from 1 remaining"
    assert plan_reservation(level, 1).reserved is True


def test_plan_reservation_is_pure():
    a = plan_reservation(StockLevel(10, 0), 3)
    b = plan_reservation(StockLevel(10, 0), 3)
    assert a == b


def test_a_plan_never_mutates_its_input():
    before = StockLevel(10, 2)
    plan_reservation(before, 4)
    assert before == StockLevel(10, 2), "input level was mutated in place"


# ---------------------------------------------------------------------------
# lock contention -- busy is not broken
# ---------------------------------------------------------------------------

is_lock_contention = reservation_rules.is_lock_contention


class FakeLockError(Exception):
    """Stands in for asyncpg's LockNotAvailableError."""
    def __init__(self, sqlstate=None):
        super().__init__("canceling statement due to lock timeout")
        self.sqlstate = sqlstate


def test_sqlstate_55P03_is_contention():
    assert is_lock_contention(FakeLockError(sqlstate="55P03")) is True


def test_driver_exception_name_is_recognised_without_sqlstate():
    class LockNotAvailableError(Exception):
        pass
    assert is_lock_contention(LockNotAvailableError()) is True


def test_contention_is_found_through_a_wrapper():
    """SQLAlchemy wraps driver errors; the SQLSTATE lives on .orig."""
    class Wrapper(Exception):
        def __init__(self, orig):
            super().__init__("DBAPIError")
            self.orig = orig
    assert is_lock_contention(Wrapper(FakeLockError(sqlstate="55P03"))) is True


@pytest.mark.parametrize("exc", [
    ValueError("nope"),
    FakeLockError(sqlstate="23505"),   # unique violation
    FakeLockError(sqlstate="40001"),   # serialization failure
    RuntimeError("boom"),
])
def test_other_errors_are_not_contention(exc):
    """Misclassifying a real fault as contention would answer 409 and hide it."""
    assert is_lock_contention(exc) is False


def test_detection_terminates_on_a_self_referential_cause():
    """A malformed exception chain must not hang the request."""
    class Loop(Exception):
        pass
    e = Loop()
    e.orig = e
    assert is_lock_contention(e) is False
