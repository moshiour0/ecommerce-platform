"""
What happens to seller orders when the order above them ends.

The saga and its seller orders are two state machines, and the arrow between
them only ever pointed one way. `InventoryReserved` pushed PENDING children to
INVENTORY_RESERVED; nothing pushed anything when the saga failed, timed out or
rolled back.

Measured on this platform: fourteen seller orders sitting at PENDING under
sagas that had already released their stock and reached ROLLBACK_COMPLETED.

That is not a stock leak -- the units were back -- and it is worse in a quieter
way. Every seller dashboard showed work waiting to be done for orders that no
longer existed, and `derive_order_status`, which reports the least advanced
child, answered PENDING for an order that had definitively ended. A seller
could have opened their queue and started packing one.
"""

import pytest

# Loaded through conftest rather than by putting services/order-saga on
# sys.path: both order-saga and saga-dispatcher expose a package called `app`,
# and whichever is imported first wins for the whole session.
from conftest import cod_rules, split_rules

ACTIONS = cod_rules.ACTIONS
SAGA_STATES_ENDING_THE_ORDER = cod_rules.SAGA_STATES_ENDING_THE_ORDER
cancellable_child_statuses = cod_rules.cancellable_child_statuses
cancellation_reason = cod_rules.cancellation_reason
children_to_cancel = cod_rules.children_to_cancel
SellerOrderStatus = split_rules.SellerOrderStatus
derive_order_status = split_rules.derive_order_status

S = SellerOrderStatus


# ---------------------------------------------------------------------------
# which parents end their children
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("saga_status", sorted(SAGA_STATES_ENDING_THE_ORDER))
def test_an_ended_parent_cancels_its_waiting_children(saga_status):
    assert children_to_cancel(saga_status, ["PENDING", "PENDING"]) == [0, 1]


def test_failed_and_timed_out_count_as_ended_not_just_rollback_completed():
    """The children must stop looking live when the parent gives up.

    Waiting for ROLLBACK_COMPLETED would leave them live for the whole
    compensation window, which is exactly when a seller is most likely to look
    at their queue and act on one.
    """
    assert "FAILED" in SAGA_STATES_ENDING_THE_ORDER
    assert "TIMED_OUT" in SAGA_STATES_ENDING_THE_ORDER
    assert "ROLLBACK_COMPLETED" in SAGA_STATES_ENDING_THE_ORDER


@pytest.mark.parametrize("saga_status", ["PENDING", "INVENTORY_RESERVED",
                                         "PAID", "ORDER_COMPLETED"])
def test_a_healthy_parent_cancels_nothing(saga_status):
    """The dangerous inverse.

    ORDER_COMPLETED matters most: under COD it means "the saga handed off",
    not "the buyer has the goods". Its children are live work on a courier's
    timescale and cancelling them would cancel real orders in flight.
    """
    assert children_to_cancel(saga_status, ["PENDING", "CONFIRMED"]) == []


# ---------------------------------------------------------------------------
# which children can be ended
# ---------------------------------------------------------------------------

def test_cancellable_states_come_from_cod_rules_not_a_copy():
    """Restating the list here is how two modules start disagreeing."""
    assert cancellable_child_statuses() == frozenset(ACTIONS["cancel"])


def test_a_dispatched_parcel_is_never_cancelled():
    """A saga cannot undo a van.

    The parcel is physically in transit; marking it CANCELLED would make the
    record disagree with the world, and the courier would still deliver it.
    """
    assert children_to_cancel("ROLLBACK_COMPLETED", ["DISPATCHED"]) == []


@pytest.mark.parametrize("status", ["DISPATCHED", "RTO_IN_TRANSIT",
                                    "DELIVERED", "SETTLED", "RETURNED",
                                    "CANCELLED"])
def test_only_pre_dispatch_children_are_touched(status):
    assert children_to_cancel("ROLLBACK_COMPLETED", [status]) == []


def test_mixed_children_are_handled_individually():
    """One cancellable child next to one in transit."""
    statuses = ["PENDING", "DISPATCHED", "CONFIRMED", "DELIVERED"]
    assert children_to_cancel("ROLLBACK_COMPLETED", statuses) == [0, 2]


def test_indices_map_back_to_the_rows_given():
    """Positions, not statuses -- the caller owns what a row is."""
    statuses = ["DELIVERED", "PENDING"]
    assert children_to_cancel("TIMED_OUT", statuses) == [1]


def test_an_unknown_child_status_is_left_alone():
    """Not ours to guess at. Skipping is safer than assuming cancellable."""
    assert children_to_cancel("ROLLBACK_COMPLETED",
                              ["PENDING", "SOMETHING_NEW"]) == [0]


def test_no_children_is_not_an_error():
    assert children_to_cancel("ROLLBACK_COMPLETED", []) == []


# ---------------------------------------------------------------------------
# what the seller is told
# ---------------------------------------------------------------------------

def test_a_cancellation_carries_a_reason():
    """CANCELLED with no reason is the thing a seller opens a ticket about."""
    assert cancellation_reason("ROLLBACK_COMPLETED") == "order rollback completed"
    assert cancellation_reason("TIMED_OUT") == "order timed out"
    assert cancellation_reason("FAILED") == "order failed"


# ---------------------------------------------------------------------------
# the reported consequence
# ---------------------------------------------------------------------------

def test_the_order_stops_reporting_itself_as_pending():
    """The visible half of the bug.

    derive_order_status reports the least advanced child, so a single stranded
    PENDING child made a rolled-back order read as PENDING. Once the children
    are cancelled the order reads CANCELLED, which is what happened to it.
    """
    assert derive_order_status(["PENDING", "PENDING"]) == S.PENDING.value
    assert derive_order_status(["CANCELLED", "CANCELLED"]) == S.CANCELLED.value


def test_cancelling_children_does_not_disturb_a_live_sibling_order():
    """Cancelled children are excluded from the derived status, not dominant.

    An order with one cancelled seller and one still shipping reads as the
    live one -- the cancellation must not pin the whole order at CANCELLED.
    """
    assert derive_order_status(["CANCELLED", "DISPATCHED"]) == S.DISPATCHED.value
