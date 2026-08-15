"""
Unit tests for the order saga state machine.

No database, no broker, no containers -- these run in milliseconds. Every bug
these cover was previously reachable only by starting 30 containers and driving
a real checkout, which is why several of them survived in production code for
weeks.

Run:  python -m pytest tests/unit -q
"""

import sys
from pathlib import Path

import pytest

# The service is not an installed package; add its root so `app.` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "order-saga"))

from app.services.transitions import (  # noqa: E402
    ALL_STATES,
    COMPENSATING_STATES,
    KNOWN_EVENT_TYPES,
    TERMINAL_STATES,
    TRANSITIONS,
    Outcome,
    resolve,
    PENDING,
    INVENTORY_RESERVED,
    PAID,
    ORDER_COMPLETED,
    FAILED,
    TIMED_OUT,
    ROLLBACK_COMPLETED,
)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_happy_path_walks_pending_to_completed():
    """PENDING -> INVENTORY_RESERVED -> PAID -> ORDER_COMPLETED."""
    d = resolve(PENDING, "InventoryReserved")
    assert d.outcome is Outcome.APPLY
    assert d.new_status == INVENTORY_RESERVED
    assert d.command == "ChargePaymentCommand"

    d = resolve(INVENTORY_RESERVED, "PaymentCharged")
    assert d.new_status == PAID
    assert d.command == "ConfirmOrderCommand"

    d = resolve(PAID, "OrderCompleted")
    assert d.new_status == ORDER_COMPLETED
    assert d.command is None, "confirming an order emits no further command"


# ---------------------------------------------------------------------------
# compensation -- the paths that cost money when wrong
# ---------------------------------------------------------------------------

def test_out_of_stock_settles_immediately_without_compensation():
    """Nothing was reserved, so there is nothing to release.

    Before this arm existed, inventory raised HTTP 400 and emitted nothing, so
    every oversubscribed order in a flash sale sat at PENDING until the reaper
    swept it fifteen minutes later.
    """
    d = resolve(PENDING, "InventoryReservationFailed")
    assert d.outcome is Outcome.APPLY
    assert d.new_status == ROLLBACK_COMPLETED
    assert d.command == "OrderFailed"


def test_declined_payment_releases_the_held_stock():
    d = resolve(INVENTORY_RESERVED, "PaymentFailed")
    assert d.new_status == FAILED
    assert d.command == "ReleaseInventoryCommand"


@pytest.mark.parametrize("state", sorted(COMPENSATING_STATES))
def test_inventory_release_settles_every_compensating_state(state):
    """FAILED and TIMED_OUT must both converge on ROLLBACK_COMPLETED.

    Without the TIMED_OUT arm a reaped saga could never settle, and 'released'
    was indistinguishable from 'leaked'.
    """
    d = resolve(state, "InventoryReleased")
    assert d.outcome is Outcome.APPLY
    assert d.new_status == ROLLBACK_COMPLETED


@pytest.mark.parametrize("state", [PAID, TIMED_OUT])
def test_refund_settles_a_charged_saga(state):
    """The money path. A charge with no route back is the worst defect here."""
    d = resolve(state, "PaymentRefunded")
    assert d.outcome is Outcome.APPLY
    assert d.new_status == ROLLBACK_COMPLETED


def test_every_compensating_state_has_at_least_one_exit():
    """A saga must never rest with compensations outstanding."""
    for state in COMPENSATING_STATES:
        exits = [e for (s, e) in TRANSITIONS if s == state]
        assert exits, f"{state} has no exit transition; sagas would strand there"


def test_every_forward_step_that_takes_something_can_give_it_back():
    """Rule 5: each forward step declares an inverse.

    Reserving stock must be releasable; charging must be refundable.
    """
    assert (INVENTORY_RESERVED, "PaymentFailed") in TRANSITIONS, "no release after a decline"
    assert any(e == "InventoryReleased" for (_, e) in TRANSITIONS), "stock can never be released"
    assert any(e == "PaymentRefunded" for (_, e) in TRANSITIONS), "money can never be returned"


# ---------------------------------------------------------------------------
# the three non-transition outcomes -- collapsing these destroyed events
# ---------------------------------------------------------------------------

def test_unknown_event_is_never_retryable():
    """An event outside the vocabulary can never become valid."""
    d = resolve(PENDING, "SomethingNobodyDefined")
    assert d.outcome is Outcome.UNKNOWN_EVENT
    assert d.new_status is None


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("event", sorted(KNOWN_EVENT_TYPES))
def test_known_event_at_terminal_state_is_a_late_duplicate(state, event):
    """Ack it. Erroring here makes a consumer redeliver forever."""
    assert resolve(state, event).outcome is Outcome.LATE_DUPLICATE


def test_out_of_order_event_is_retryable_not_lost():
    """PaymentCharged before InventoryReserved was applied.

    This is the S-3 defect: the original code logged the event and then
    committed, burning the idempotency key and destroying the delivery
    permanently. It must be retryable instead.
    """
    d = resolve(PENDING, "PaymentCharged")
    assert d.outcome is Outcome.OUT_OF_ORDER
    assert d.new_status is None


def test_the_three_rejection_outcomes_stay_distinct():
    """Collapsing any two of these reintroduces a real bug."""
    assert resolve(PENDING, "Nonsense").outcome is Outcome.UNKNOWN_EVENT
    assert resolve(ORDER_COMPLETED, "PaymentCharged").outcome is Outcome.LATE_DUPLICATE
    assert resolve(PENDING, "PaymentCharged").outcome is Outcome.OUT_OF_ORDER


# ---------------------------------------------------------------------------
# exhaustive sweep -- the whole state x event space
# ---------------------------------------------------------------------------

def test_every_state_event_pair_is_decided():
    """No combination may fall through undecided."""
    for state in ALL_STATES:
        for event in KNOWN_EVENT_TYPES:
            d = resolve(state, event)
            assert isinstance(d.outcome, Outcome)
            if d.outcome is Outcome.APPLY:
                assert d.new_status in ALL_STATES, f"{state}+{event} -> unknown state"


def test_terminal_states_never_transition():
    for state in TERMINAL_STATES:
        for event in KNOWN_EVENT_TYPES:
            assert resolve(state, event).outcome is not Outcome.APPLY, \
                f"{state} must be terminal but {event} moved it"


def test_transitions_never_target_an_undefined_state():
    for (src, event), (dst, _cmd) in TRANSITIONS.items():
        assert src in ALL_STATES, f"transition from undefined state {src}"
        assert dst in ALL_STATES, f"transition to undefined state {dst}"
        assert event in KNOWN_EVENT_TYPES, f"transition on unknown event {event}"


def test_no_transition_is_a_self_loop():
    """A self-loop would let a redelivered event re-emit its command forever."""
    for (src, event), (dst, _cmd) in TRANSITIONS.items():
        assert src != dst, f"{src} + {event} loops back to itself"


def test_resolve_is_pure():
    """Same inputs, same answer -- no hidden clock or state."""
    a = resolve(PENDING, "InventoryReserved")
    b = resolve(PENDING, "InventoryReserved")
    assert a == b


# ---------------------------------------------------------------------------
# reachability -- proves the machine can actually finish
# ---------------------------------------------------------------------------

def test_every_state_can_reach_a_terminal_state():
    """A state that cannot terminate is an order that can never close.

    PAID was exactly this before the reaper swept it: reachable, with no exit
    if OrderCompleted never arrived, so a charged order sat there forever.
    """
    reaches = dict.fromkeys(ALL_STATES, False)
    for t in TERMINAL_STATES:
        reaches[t] = True

    changed = True
    while changed:  # backwards reachability to a fixed point
        changed = False
        for (src, _e), (dst, _c) in TRANSITIONS.items():
            if reaches[dst] and not reaches[src]:
                reaches[src] = True
                changed = True

    stranded = sorted(s for s, ok in reaches.items() if not ok)
    assert not stranded, f"states that can never terminate: {stranded}"


def test_pending_reaches_both_completion_and_rollback():
    """Both a successful order and a compensated one must be expressible."""
    seen, frontier = set(), [PENDING]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for (src, _e), (dst, _c) in TRANSITIONS.items():
            if src == cur:
                frontier.append(dst)
    assert ORDER_COMPLETED in seen, "no path from PENDING to a completed order"
    assert ROLLBACK_COMPLETED in seen, "no path from PENDING to a rolled-back order"
