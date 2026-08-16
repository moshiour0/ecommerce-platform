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
from typing import Optional


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
