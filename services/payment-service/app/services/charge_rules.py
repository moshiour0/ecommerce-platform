"""
Charge decision rules, as pure functions.

Extracted from process_charge so the other half of the money path is testable
without a database or a payment provider. What stays in the service is the
insert, the lock and the commit; what lives here is the decision.

Three separate decisions hide inside process_charge, and conflating them is
how the idempotency guard came to violate the rule it exists to satisfy:

  1. what the provider said            -> authorize()
  2. what event that produces          -> event_for()
  3. what to do with a duplicate key   -> resolve_idempotency()
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

EVENT_CHARGED = "PaymentCharged"
EVENT_FAILED = "PaymentFailed"

# The simulated decline token. Real integrations replace authorize() wholesale;
# the mapping from provider outcome to ledger status and event stays.
DECLINE_TOKEN = "tok_fail"


def authorize(payment_token: Optional[str]) -> str:
    """Provider outcome for a token. Stand-in for a real PSP call."""
    return STATUS_FAILED if payment_token == DECLINE_TOKEN else STATUS_SUCCESS


def event_for(status: str) -> str:
    """Ledger status -> outbox event.

    These names are a cross-service contract: order-saga transitions on
    PaymentCharged and PaymentFailed, so a typo here is answered 422 and
    dead-lettered, stranding the saga until the reaper sweeps it.
    """
    return EVENT_CHARGED if status == STATUS_SUCCESS else EVENT_FAILED


def is_declined(status: str) -> bool:
    return status == STATUS_FAILED


class IdempotencyOutcome(str, Enum):
    PROCEED = "proceed"            # first time this key has been seen
    RETURN_CACHED = "return_cached"  # completed before; replay the result
    RETRY_LATER = "retry_later"    # same key still in flight elsewhere


@dataclass(frozen=True)
class IdempotencyDecision:
    outcome: IdempotencyOutcome
    detail: str = ""


def resolve_idempotency(
    key_was_inserted: bool,
    stored_result_id: Optional[object],
    cached_result_found: bool,
) -> IdempotencyDecision:
    """Decide what a request carrying an Idempotency-Key should do.

    Arguments describe what the database said, not how it was asked:
      key_was_inserted   -- the INSERT ... ON CONFLICT DO NOTHING added a row
      stored_result_id   -- result_id on the existing key row, if any
      cached_result_found -- that result_id still resolves to a ledger row

    Rule 4 says the guard is a cache, not a gate, and that returning an error
    for a legitimate retry is a protocol violation. Two of the three branches
    honour that directly. The third is subtler:

    A key row that exists with no result_id yet means another request holding
    the same key is still in flight -- it has inserted the key but not
    committed its outcome. There is no committed result to replay, so the only
    honest answers are "wait" or "try again". RETRY_LATER is not the violation
    Rule 4 forbids: that prohibition is about answering a *completed* retry
    with an error, which RETURN_CACHED handles. Callers map this to 409, which
    the dispatcher already treats as retryable.

    The genuinely dangerous alternative would be PROCEED, which charges the
    card a second time.
    """
    if key_was_inserted:
        return IdempotencyDecision(IdempotencyOutcome.PROCEED, "new key")

    if stored_result_id is not None and cached_result_found:
        return IdempotencyDecision(
            IdempotencyOutcome.RETURN_CACHED, "replaying previously committed result")

    if stored_result_id is None:
        return IdempotencyDecision(
            IdempotencyOutcome.RETRY_LATER,
            "same idempotency key is still in flight; no committed result yet")

    # A result_id that no longer resolves: the ledger row was pruned or removed
    # out from under the key. Charging again would double-charge, so this is
    # never PROCEED.
    return IdempotencyDecision(
        IdempotencyOutcome.RETRY_LATER,
        "idempotency key references a result that no longer exists")
