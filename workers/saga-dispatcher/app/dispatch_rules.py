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


# ---------------------------------------------------------------------------
# retry policy: what to do with a command that did not succeed
# ---------------------------------------------------------------------------
#
# The header of this module already warns that "not settling something that can
# never succeed spins forever". That warning was only half-heeded: the saga
# path got 422/DEAD_LETTERED, and the command paths -- escrow, seller-order
# stock movements, refunds -- were left with a bare `return False` and the
# comment "never abandon an unbooked liability".
#
# Never abandoning turned out to mean never progressing. A command whose claim
# is released is immediately re-claimable, and CLAIM_SQL orders by created_at,
# so the *oldest* failures are claimed first on every tick. Fourteen escrow
# bookings for sellers that no longer exist -- each one a permanent 409 -- were
# re-claimed ahead of all live work every two seconds, filled a batch of
# twenty, and saturated the payment-service bulkhead until legitimate bookings
# were shed. Measured on a real run: a live seller's 2200-poisha liability was
# never booked, because fourteen dead ones were ahead of it in the queue.
#
# So a failure needs three answers, not two:
#
#   SETTLE -- done, or it can never apply and there is nothing to keep.
#   RETRY  -- transient. Try again, but *later*, and later must grow.
#   PARK   -- it will never succeed. Stop claiming it, keep it, and make it
#             loud. Parking is not abandoning: the row, its payload and its
#             last error stay on the outbox where a person can find them.
#
# The distinction between RETRY and PARK is worth being careful about in both
# directions. Parking something transient discards real money. Retrying
# something permanent denies service to everything behind it -- which is the
# more insidious failure, because it presents as unrelated tests timing out
# rather than as an error about the thing that is actually broken.


class Disposition(str, Enum):
    """What should happen to a command after an attempt."""

    SETTLE = "settle"  # mark processed; never claimed again
    RETRY = "retry"    # stays claimable, but not before next_attempt_at
    PARK = "park"      # stops being claimed; kept and surfaced for a human


@dataclass(frozen=True)
class Outcome:
    """The result of attempting one command."""

    disposition: Disposition
    error: Optional[str] = None

    @property
    def settled(self) -> bool:
        """Whether the dispatcher is finished with this row.

        Parked rows are finished in the sense that nothing will retry them,
        but they are deliberately *not* marked processed: a processed row is
        indistinguishable from one that succeeded, and an unbooked liability
        must never look like a booked one.
        """
        return self.disposition is Disposition.SETTLE


SETTLED = Outcome(Disposition.SETTLE)


def retry(error: str) -> Outcome:
    return Outcome(Disposition.RETRY, error)


def park(error: str) -> Outcome:
    return Outcome(Disposition.PARK, error)


# A downstream that answered with one of these has understood the request and
# refused it. Refusals do not become acceptances by being repeated.
#
# 409 is on this list for commands and deliberately NOT for saga events, where
# it means "valid but out of order, ask again". The same number means opposite
# things to the two callers, which is exactly why these are two functions
# rather than one shared table.
TERMINAL_COMMAND_STATUSES = frozenset({400, 404, 409, 410, 422})

# Overload and transport faults. The downstream never formed an opinion.
RETRYABLE_COMMAND_STATUSES = frozenset({408, 425, 429})


def classify_command_failure(status_code: Optional[int]) -> Disposition:
    """Decide what a failed downstream call earns.

    Unknown is retryable on purpose. A status nobody anticipated is not
    evidence that the work is impossible, and the cap on attempts below means
    "retry" can no longer mean "forever" -- so the conservative answer is now
    safe to give. It was not safe before this cap existed, which is how the
    bare `return False` came to be written.
    """
    if status_code is None:
        return Disposition.RETRY  # transport fault; nothing was answered
    if 200 <= status_code < 300:
        return Disposition.SETTLE
    if status_code in RETRYABLE_COMMAND_STATUSES:
        return Disposition.RETRY
    if status_code >= 500:
        return Disposition.RETRY
    if status_code in TERMINAL_COMMAND_STATUSES:
        return Disposition.PARK
    return Disposition.RETRY


# Retry pacing. Doubling from two seconds, capped at five minutes, gives
# roughly fifty minutes of trying across MAX_ATTEMPTS before a command parks.
# That is long enough to outlast a deploy, a failover or a slow migration, and
# short enough that a genuinely broken command stops blocking within the hour.
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 300.0
MAX_ATTEMPTS = 15


def backoff_seconds(attempts: int,
                    base: float = RETRY_BASE_SECONDS,
                    cap: float = RETRY_CAP_SECONDS,
                    jitter: float = 0.0) -> float:
    """How long before this command may be claimed again.

    `jitter` is a caller-supplied fraction in [0, 1) rather than a random draw,
    so this function stays pure and testable. main.py passes random.random().
    Without it, a downstream that drops fifty commands at once gets all fifty
    back in the same millisecond, and the recovery attempt is itself the second
    outage.
    """
    if attempts < 1:
        raise ValueError(f"attempts starts at 1, got {attempts}")
    if not 0.0 <= jitter < 1.0:
        raise ValueError(f"jitter must be within [0, 1), got {jitter}")

    delay = min(cap, base * (2 ** (attempts - 1)))
    # Jitter spreads downward only. Spreading upward would let the cap be
    # exceeded, and the cap is the promise that a retry always comes.
    return delay * (1.0 - 0.5 * jitter)


def disposition_after(outcome_disposition: Disposition, attempts: int,
                      max_attempts: int = MAX_ATTEMPTS) -> Disposition:
    """Apply the attempt cap to a retry.

    A command that has been retried MAX_ATTEMPTS times has had its hour. It
    parks -- not because the platform has decided it is impossible, but because
    the evidence is now indistinguishable from impossible, and the cost of
    continuing to guess is paid by every command queued behind it.
    """
    if outcome_disposition is Disposition.RETRY and attempts >= max_attempts:
        return Disposition.PARK
    return outcome_disposition
