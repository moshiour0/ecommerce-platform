"""
Dispatcher decision rules, as pure data.

Extracted from handle() and _advance_saga() so they can be tested without a
database, a broker, or a downstream service. Everything here answers a question
of the form "given this command, or this HTTP status, what should happen" --
no I/O, no clock.

The rules that matter most are the ones about *settling*. Settling a command
marks it processed and it is never retried; not settling releases the claim so
a later tick tries again. Getting that backwards in either direction is
expensive:

  * settling something that did not happen loses the work silently -- an
    unsettled refund is money the customer never gets back;
  * not settling something that can never succeed spins forever, which is how
    an open circuit turned into 165 claim/release cycles per second.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional


class SagaAck(str, Enum):
    """How order-saga answered an attempt to advance it."""

    APPLIED = "applied"              # 2xx: transition applied, or acked duplicate
    DEFERRED = "deferred"            # 409: valid but not applicable yet
    DEAD_LETTERED = "dead_lettered"  # 422: never valid, do not retry
    ERROR = "error"                  # anything else: transport or server fault


# Whether each acknowledgement settles the command. DEFERRED and ERROR must
# not settle or the delivery is lost; DEAD_LETTERED must settle or the
# dispatcher retries an event the saga will never accept, forever.
SETTLES: dict[SagaAck, bool] = {
    SagaAck.APPLIED: True,
    SagaAck.DEFERRED: False,
    SagaAck.DEAD_LETTERED: True,
    SagaAck.ERROR: False,
}


def classify_saga_response(status_code: int) -> SagaAck:
    """Map an order-saga HTTP status onto its meaning.

    The three rejection statuses are deliberately distinct; the saga returns
    409/422 precisely so this decision can be made without guessing.
    """
    if status_code < 300:
        return SagaAck.APPLIED
    if status_code == 409:
        return SagaAck.DEFERRED
    if status_code == 422:
        return SagaAck.DEAD_LETTERED
    return SagaAck.ERROR


def settles(ack: SagaAck) -> bool:
    return SETTLES[ack]


@dataclass(frozen=True)
class Route:
    """What a command does and how its outcomes map onto saga events."""

    target: Optional[str]          # breaker/service name; None = no downstream call
    success_status: Optional[int]  # status that counts as success
    on_success: Optional[str]      # saga event emitted on success
    on_failure: Optional[str]      # saga event emitted on failure
    settle_on_failure: bool = True # False = retry instead of reporting failure


# ---------------------------------------------------------------------------
# reservation planning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ItemReservation:
    product_id: str
    quantity: int


@dataclass(frozen=True)
class ReservationPlan:
    items: List[ItemReservation]
    error: Optional[str]  # None when the plan is usable

    @property
    def ok(self) -> bool:
        return self.error is None


def plan_item_reservations(items_payload: Any) -> ReservationPlan:
    """Turn a saga's items_payload into the reservations to make.

    This exists because the dispatcher used to read `items[0]` and reserve that
    one line. A three-item order held stock for one product and was charged for
    three, and nothing failed -- the saga completed, the customer paid, and two
    of the three products were never reserved.

    Three things happen here, and each is a rule rather than a formatting step:

    * Duplicates are coalesced. Two lines for the same product become one
      reservation for the sum. The reservation ledger has a unique index on
      (order_id, product_id) for live holds, so two separate reservations for
      one product in one order would be rejected by the database -- and a
      release would return only one of them.

    * Items are sorted by product id. Each reserve is its own transaction today,
      so this is not currently deadlock avoidance; it is determinism. A retried
      command reserves in the same sequence as the original, which makes the
      idempotency keys line up and makes a partial failure reproducible. If the
      reserves ever move into one transaction, the ordering is already right.

    * Anything unusable is rejected as a whole rather than partially reserved.
      A missing product id or a non-positive quantity means the order is
      malformed, and reserving the lines that happen to parse would leave stock
      held against an order that can never complete.
    """
    items = items_payload
    # The saga accepts a list or a dict, and cart-service once wrapped it.
    if isinstance(items, dict):
        items = items.get("items", items)
    if isinstance(items, dict):
        # {product_id: quantity}
        items = [{"product_id": k, "quantity": v} for k, v in items.items()]
    if not isinstance(items, list) or not items:
        return ReservationPlan([], "EmptyItemsPayload")

    totals: dict = {}
    for entry in items:
        if not isinstance(entry, dict):
            return ReservationPlan([], f"MalformedItem: {entry!r}")

        product_id = entry.get("product_id")
        if not product_id:
            return ReservationPlan([], "ItemMissingProductId")

        quantity = entry.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            return ReservationPlan([], f"NonIntegerQuantity: {quantity!r}")
        if quantity <= 0:
            return ReservationPlan([], f"NonPositiveQuantity: {quantity}")

        totals[str(product_id)] = totals.get(str(product_id), 0) + quantity

    return ReservationPlan(
        [ItemReservation(pid, qty) for pid, qty in sorted(totals.items())],
        None,
    )


def reservation_idempotency_key(message_id: Any, product_id: str) -> str:
    """One key per (command, product).

    A single key for the whole command would make the second line's reserve
    look like a retry of the first, and inventory-service would return the
    first line's cached result instead of reserving anything.
    """
    return f"{message_id}:{product_id}"


# Commands order-saga can emit, and what the dispatcher does with each.
ROUTES: dict[str, Route] = {
    "ReserveInventoryCommand": Route(
        target="inventory-service", success_status=200,
        on_success="InventoryReserved",
        # Out of stock (409) and unknown product (404) are business outcomes
        # with their own saga transition, not transport failures.
        on_failure="InventoryReservationFailed"),

    "ChargePaymentCommand": Route(
        target="payment-service", success_status=201,
        on_success="PaymentCharged",
        on_failure="PaymentFailed"),

    "RefundPaymentCommand": Route(
        target="payment-service", success_status=200,
        on_success="PaymentRefunded",
        # A failed refund is never reported as a saga outcome and never
        # settled: the command stays claimable so it retries. An outstanding
        # refund must not be abandoned because the provider was briefly down.
        on_failure=None, settle_on_failure=False),

    # The inverse of ReserveInventoryCommand (§5). This used to be a
    # self-acknowledgement -- target None, straight to InventoryReleased --
    # so the saga reached ROLLBACK_COMPLETED believing stock had been returned
    # while inventory-service still held it. 75 rows and 88 units were stranded
    # that way, and every failed order leaked more.
    "ReleaseInventoryCommand":    Route(
        target="inventory-service", success_status=200,
        on_success="InventoryReleased",
        # No failure event: an unreturned reservation must be retried, not
        # reported as a compensation that completed. Same reasoning as the
        # refund leg.
        on_failure=None, settle_on_failure=False),
    "CompensateInventoryCommand": Route(
        target="inventory-service", success_status=200,
        on_success="InventoryReleased",
        on_failure=None, settle_on_failure=False),
    "ConfirmOrderCommand":        Route(None, None, "OrderCompleted", None),

    # Emitted by the reaper when a PENDING saga times out with nothing to
    # compensate. Nothing to do, but it must settle so it is not re-claimed.
    "SagaTimedOut":               Route(None, None, None, None),
}


def route_for(command: str) -> Optional[Route]:
    """None means the command is unknown -- settle it rather than spin."""
    return ROUTES.get(command)


def events_emitted() -> frozenset:
    """Every saga event this dispatcher can produce.

    Used to assert the dispatcher and the saga agree on vocabulary: an event
    the saga does not know is answered 422 and dead-lettered, silently losing
    the step.
    """
    out = set()
    for r in ROUTES.values():
        out.update(e for e in (r.on_success, r.on_failure) if e)
    return frozenset(out)


def commands_handled() -> frozenset:
    return frozenset(ROUTES)
