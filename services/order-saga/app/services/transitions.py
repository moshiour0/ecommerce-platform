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
    COMPENSATE = "compensate"          # ack; state unchanged; release what is held


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


# Cash on delivery does not charge anything at checkout, so the card table's
# INVENTORY_RESERVED -> ChargePaymentCommand arm is not merely unnecessary
# there, it is wrong: it charges a card that was never presented for goods
# nobody has received.
#
# Under COD the saga's own job ends once stock is held. Everything after that
# -- confirmation, dispatch, delivery, settlement -- happens to the seller
# orders on a courier's timescale, and the buyer-facing status is derived from
# them (cod_rules, and ARCHITECTURE_STATE_FINAL.md §3d). So the saga hands off
# and completes.
#
# ORDER_COMPLETED here means "the saga finished its work", not "the buyer has
# their goods". Those were the same statement for a card order and are days
# apart for a COD one, which is exactly why the buyer-facing status is not
# read from this column.
COD_TRANSITIONS: dict[tuple[str, str], tuple[str, Optional[str]]] = {
    (PENDING, "InventoryReserved"): (ORDER_COMPLETED, None),
}


# ---------------------------------------------------------------------------
# resources that arrived after the saga stopped waiting for them
# ---------------------------------------------------------------------------
# Deliberately NOT part of TRANSITIONS, because these are not transitions. The
# saga is already exactly where it belongs; what is new is that something is
# being held on its behalf and has to be given back.
#
# Modelling them as transitions would have made each one a self-loop, and
# `test_no_transition_is_a_self_loop` is right to forbid those: a state that
# transitions to itself re-emits its command on every redelivery. Separating
# the table keeps that invariant intact and says what is actually happening.
#
# The race this exists for is ordinary. The reaper times out a saga sitting at
# PENDING and, seeing no reservation recorded against it, correctly concludes
# there is nothing to compensate. Meanwhile the reserve the saga never heard
# about succeeds at inventory-service. The units are now held by an order that
# can never complete, and the InventoryReserved that would have said so
# resolved to OUT_OF_ORDER -- retried forever, applied never.
#
# Measured on this platform before the fix: twelve sagas at TIMED_OUT, twelve
# units held, and twelve commands that had been asking about it every few
# seconds since the orders were created.
LATE_RESOURCE_COMPENSATIONS: dict[tuple[str, str], str] = {
    # Reaped before the reservation landed. Give the stock back; the existing
    # (TIMED_OUT, "InventoryReleased") arm then closes the saga out.
    (TIMED_OUT, "InventoryReserved"):          "ReleaseInventoryCommand",

    # FAILED is only reachable from INVENTORY_RESERVED, so this is a duplicate
    # rather than news -- but a duplicate that used to resolve to OUT_OF_ORDER
    # and spin. /release is keyed by order_id and settles whatever is held, so
    # re-issuing it is a no-op rather than a double release.
    (FAILED, "InventoryReserved"):             "ReleaseInventoryCommand",

    # And after a rollback has completed. Terminal, so this would otherwise be
    # acknowledged as a late duplicate and the units would stay held silently
    # -- the quietest version of the same leak.
    (ROLLBACK_COMPLETED, "InventoryReserved"): "ReleaseInventoryCommand",
}

# ORDER_COMPLETED is deliberately absent from the table above. Under COD a
# completed saga means "the saga handed off", not "the buyer has the goods":
# the stock is legitimately held until delivery consumes it or a return
# releases it (§3d). Releasing there would put goods already on a courier's
# van back on sale. This is asserted by a test rather than left to the reader.


def resolve(current_status: str, event_type: str,
            payment_method: str = "CARD") -> Decision:
    """Decide what an event means for a saga in a given state.

    Pure: no I/O, no clock, no randomness. The three non-transition outcomes
    are deliberately distinct because collapsing them is what destroyed events
    -- an unknown type must never be retried forever, an out-of-order event
    must never be acknowledged, and a late duplicate must never be an error.

    `payment_method` selects the forward path. It defaults to CARD so that
    every existing caller and every existing test keeps the behaviour it was
    written against; only an order explicitly marked COD takes the other one.
    The compensation arms are shared, because releasing stock and failing an
    order mean the same thing however the order was going to be paid for.
    """
    if event_type not in KNOWN_EVENT_TYPES:
        return Decision(Outcome.UNKNOWN_EVENT)

    if payment_method == "COD":
        entry = COD_TRANSITIONS.get((current_status, event_type))
        if entry is not None:
            new_status, command = entry
            return Decision(Outcome.APPLY, new_status=new_status,
                            command=command)

    entry = TRANSITIONS.get((current_status, event_type))
    if entry is not None:
        new_status, command = entry
        return Decision(Outcome.APPLY, new_status=new_status, command=command)

    # Before the terminal check, on purpose. A resource held on behalf of a
    # saga that has finished still has to be returned, and answering "late
    # duplicate" here is what let twelve units sit held indefinitely.
    command = LATE_RESOURCE_COMPENSATIONS.get((current_status, event_type))
    if command is not None:
        return Decision(Outcome.COMPENSATE, new_status=None, command=command)

    if current_status in TERMINAL_STATES:
        return Decision(Outcome.LATE_DUPLICATE)

    return Decision(Outcome.OUT_OF_ORDER)
