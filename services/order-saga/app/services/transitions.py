"""
The order saga state machine, as pure data.

Extracted from advance_saga's if/elif chain so it can be tested without a
database, a broker, or a running container. The transitions are the part of
this platform where mistakes cost money -- a missing arm stranded every
oversubscribed order at PENDING for fifteen minutes, and an absent refund path
meant a charged card with no route back -- yet exercising them required the
full 30-container stack and about two minutes.

The orchestrator still owns persistence, locking and payload construction.
This module owns only the question "given where the saga is and what just
happened, what should happen next", which is answerable in microseconds.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

PENDING = "PENDING"
INVENTORY_RESERVED = "INVENTORY_RESERVED"
PAID = "PAID"
ORDER_COMPLETED = "ORDER_COMPLETED"
FAILED = "FAILED"
TIMED_OUT = "TIMED_OUT"
ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"

ALL_STATES = frozenset({
    PENDING, INVENTORY_RESERVED, PAID,
    ORDER_COMPLETED, FAILED, TIMED_OUT, ROLLBACK_COMPLETED,
})

# No transition leaves these. A known event arriving here is a late duplicate.
TERMINAL_STATES = frozenset({ORDER_COMPLETED, ROLLBACK_COMPLETED})

# A saga must not rest in these: each has compensations outstanding.
COMPENSATING_STATES = frozenset({FAILED, TIMED_OUT})

KNOWN_EVENT_TYPES = frozenset({
    "InventoryReserved",
    "InventoryReservationFailed",
    "PaymentCharged",
    "PaymentFailed",
    "PaymentRefunded",
    "OrderCompleted",
    "InventoryReleased",
})


class Outcome(str, Enum):
    """What the caller should do with this event."""

    APPLY = "apply"                  # a real transition; persist it
    LATE_DUPLICATE = "late_duplicate"  # already terminal; ack, do not re-apply
    OUT_OF_ORDER = "out_of_order"      # valid event, not yet applicable; retry
    UNKNOWN_EVENT = "unknown_event"    # never valid; dead-letter it


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    new_status: Optional[str] = None
    command: Optional[str] = None

    @property
    def emits_command(self) -> bool:
        return self.command is not None


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
# (current_status, event_type) -> (new_status, command emitted or None)
#
# Written as data rather than branches so the whole machine is visible at once.
# Reading the old if/elif chain, it was genuinely hard to notice that nothing
# handled InventoryReservationFailed, or that PaymentRefunded had no arm at all.
TRANSITIONS: dict[tuple[str, str], tuple[str, Optional[str]]] = {
    # forward path
    (PENDING, "InventoryReserved"):            (INVENTORY_RESERVED, "ChargePaymentCommand"),
    (INVENTORY_RESERVED, "PaymentCharged"):    (PAID, "ConfirmOrderCommand"),
    (PAID, "OrderCompleted"):                  (ORDER_COMPLETED, None),

    # inventory could not be reserved: nothing was taken, so nothing to
    # compensate -- terminal immediately.
    (PENDING, "InventoryReservationFailed"):   (ROLLBACK_COMPLETED, "OrderFailed"),

    # payment declined after stock was held: release it.
    (INVENTORY_RESERVED, "PaymentFailed"):     (FAILED, "ReleaseInventoryCommand"),

    # compensation acknowledgements. Both FAILED (declined) and TIMED_OUT
    # (reaped) converge on ROLLBACK_COMPLETED; without the TIMED_OUT arms a
    # reaped saga could never settle and "released" was indistinguishable
    # from "leaked".
    (FAILED, "InventoryReleased"):             (ROLLBACK_COMPLETED, None),
    (TIMED_OUT, "InventoryReleased"):          (ROLLBACK_COMPLETED, None),
    (PAID, "PaymentRefunded"):                 (ROLLBACK_COMPLETED, None),
    (TIMED_OUT, "PaymentRefunded"):            (ROLLBACK_COMPLETED, None),
}


def resolve(current_status: str, event_type: str) -> Decision:
    """Decide what an event means for a saga in a given state.

    Pure: no I/O, no clock, no randomness. The three non-transition outcomes
    are deliberately distinct because collapsing them is what destroyed events
    -- an unknown type must never be retried forever, an out-of-order event
    must never be acknowledged, and a late duplicate must never be an error.
    """
    if event_type not in KNOWN_EVENT_TYPES:
        return Decision(Outcome.UNKNOWN_EVENT)

    entry = TRANSITIONS.get((current_status, event_type))
    if entry is not None:
        new_status, command = entry
        return Decision(Outcome.APPLY, new_status=new_status, command=command)

    if current_status in TERMINAL_STATES:
        return Decision(Outcome.LATE_DUPLICATE)

    return Decision(Outcome.OUT_OF_ORDER)
