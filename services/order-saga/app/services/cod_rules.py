"""
The cash-on-delivery lifecycle of a seller order, as pure functions.

Under a card flow the money is captured before fulfilment and the risk is a
chargeback. Under COD nothing is captured at checkout: the buyer pays a courier
days later, the courier remits to the platform later still, and the risk is
that nobody answers the door. That inverts the order lifecycle rather than
adding a branch to it, which is why this is its own module and its own
vocabulary (ARCHITECTURE_STATE_FINAL.md §3d).

Two things here are easy to get wrong and expensive to get wrong.

**After dispatch there is no compensation, only a return.**
A saga cannot undo a van. `cancel` is therefore refused once a parcel has left,
and the route back is RTO_IN_TRANSIT -> RETURNED, which is a forward path that
happens to end where it started. The shipping is spent either way.

**Delivery is what consumes stock, not checkout.**
Reserve moves units from available to reserved. A dispatched parcel is still
returnable, so it stays reserved. Only when the buyer takes it does the stock
actually leave -- and only then may it be retired without going back on sale.
Getting this wrong in either direction is visible in the warehouse: consuming
at dispatch loses the goods that come back, and releasing on delivery puts sold
goods back on the shelf.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set

from .split_rules import SellerOrderStatus


class InventoryEffect(str, Enum):
    """What a transition implies for stock.

    Named rather than inferred at the call site, because "did this transition
    put the goods back?" is exactly the question a reader of the orchestrator
    should not have to answer from context.
    """

    NONE = "none"
    RELEASE = "release"   # back to available: the goods are on the shelf
    CONSUME = "consume"   # out of reserved and gone: the buyer has them


@dataclass(frozen=True)
class Transition:
    to: SellerOrderStatus
    event: str
    effect: InventoryEffect
    requires_reason: bool = False


class Outcome(str, Enum):
    OK = "ok"
    INVALID = "invalid"          # the request itself is not acceptable
    NOT_ALLOWED = "not_allowed"  # a real action, not from this state


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    to: Optional[SellerOrderStatus]
    event: Optional[str]
    effect: InventoryEffect
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def http_status(self) -> int:
        return 422 if self.outcome is Outcome.INVALID else 409


EVENT_CONFIRMED = "SellerOrderConfirmed"
EVENT_DISPATCHED = "SellerOrderDispatched"
EVENT_DELIVERED = "SellerOrderDelivered"
EVENT_SETTLED = "SellerOrderSettled"
EVENT_RTO_STARTED = "SellerOrderRtoStarted"
EVENT_RETURNED = "SellerOrderReturned"
EVENT_CANCELLED = "SellerOrderCancelled"

S = SellerOrderStatus

# action -> {from state: Transition}
#
# The table is the specification. Every guard below reads from it rather than
# re-stating it, so there is one description of the machine rather than two.
ACTIONS: Dict[str, Dict[SellerOrderStatus, Transition]] = {
    "confirm": {
        # The seller accepts the order. Only once stock is actually held --
        # confirming an order whose reservation failed promises goods that
        # were never set aside.
        S.INVENTORY_RESERVED: Transition(
            S.CONFIRMED, EVENT_CONFIRMED, InventoryEffect.NONE),
    },
    "dispatch": {
        # Handed to a courier. Stock stays reserved: the parcel is out of the
        # building and still returnable, so it is neither on the shelf nor
        # gone.
        S.CONFIRMED: Transition(
            S.DISPATCHED, EVENT_DISPATCHED, InventoryEffect.NONE),
    },
    "deliver": {
        # The buyer took it and paid the courier. The one place stock is
        # consumed.
        S.DISPATCHED: Transition(
            S.DELIVERED, EVENT_DELIVERED, InventoryEffect.CONSUME),
    },
    "settle": {
        # The courier remitted the cash and it reconciled. Only now is the
        # seller payable -- which is why DELIVERED is not terminal.
        S.DELIVERED: Transition(
            S.SETTLED, EVENT_SETTLED, InventoryEffect.NONE),
    },
    "mark_rto": {
        # Refused at the door, or undeliverable. A forward state, not a
        # rollback: the goods are physically moving back.
        S.DISPATCHED: Transition(
            S.RTO_IN_TRANSIT, EVENT_RTO_STARTED, InventoryEffect.NONE,
            requires_reason=True),
    },
    "complete_return": {
        # Back with the seller and checked in. Now the goods are on a shelf
        # again, so the units go back to available.
        S.RTO_IN_TRANSIT: Transition(
            S.RETURNED, EVENT_RETURNED, InventoryEffect.RELEASE),
    },
    "cancel": {
        # Only before dispatch. Every one of these releases the hold, and
        # PENDING is included because a cancel racing a reservation must still
        # be safe -- release is a no-op when nothing is held.
        S.PENDING: Transition(
            S.CANCELLED, EVENT_CANCELLED, InventoryEffect.RELEASE,
            requires_reason=True),
        S.INVENTORY_RESERVED: Transition(
            S.CANCELLED, EVENT_CANCELLED, InventoryEffect.RELEASE,
            requires_reason=True),
        S.CONFIRMED: Transition(
            S.CANCELLED, EVENT_CANCELLED, InventoryEffect.RELEASE,
            requires_reason=True),
    },
}

# Nothing leaves these.
TERMINAL: Set[SellerOrderStatus] = {S.SETTLED, S.RETURNED, S.CANCELLED}


def _as_status(value) -> Optional[SellerOrderStatus]:
    if isinstance(value, SellerOrderStatus):
        return value
    try:
        return SellerOrderStatus(value)
    except (ValueError, TypeError):
        return None


def plan_transition(action: str, status, reason: str = "") -> Decision:
    """What this action does to a seller order in this state.

    An unknown action is INVALID and an action from the wrong state is
    NOT_ALLOWED, because they are different instructions to the caller: the
    first is a bug in the client and the second is a race that a retry might
    legitimately resolve.
    """
    table = ACTIONS.get(action)
    if table is None:
        return Decision(Outcome.INVALID, None, None, InventoryEffect.NONE,
                        f"unknown action {action!r}")

    current = _as_status(status)
    if current is None:
        return Decision(Outcome.NOT_ALLOWED, None, None, InventoryEffect.NONE,
                        f"unrecognised seller order status {status!r}")

    transition = table.get(current)
    if transition is None:
        allowed = ", ".join(sorted(s.value for s in table)) or "no state"
        return Decision(
            Outcome.NOT_ALLOWED, None, None, InventoryEffect.NONE,
            f"cannot {action} a seller order that is {current.value}; "
            f"allowed from: {allowed}")

    if transition.requires_reason and not (reason or "").strip():
        return Decision(Outcome.INVALID, None, None, InventoryEffect.NONE,
                        f"{action} requires a reason")

    return Decision(Outcome.OK, transition.to, transition.event,
                    transition.effect,
                    (reason or "").strip())


def is_terminal(status) -> bool:
    return _as_status(status) in TERMINAL


def may_cancel(status) -> bool:
    """Whether this seller order can still be called off.

    Its own function because it is the question a buyer's "cancel order"
    button asks, and the answer has to be available before the button is
    drawn rather than as a 409 after it is pressed.
    """
    return _as_status(status) in ACTIONS["cancel"]


def reachable(status) -> Set[SellerOrderStatus]:
    current = _as_status(status)
    return {t.to for table in ACTIONS.values()
            for frm, t in table.items() if frm == current}


def actions_from(status) -> Set[str]:
    current = _as_status(status)
    return {action for action, table in ACTIONS.items() if current in table}
