"""
Refund decision rules, as pure functions.

Extracted from process_refund so the money path can be tested without a
database. What remains in the service is locking, querying and persistence;
what lives here is the decision: given what the ledger already contains, what
should this refund attempt do.

Two properties matter more than anything else, and both are invisible in a
happy-path test:

  * the ledger is append-only and must net to zero after a reversal -- a
    refund never mutates the original charge, so a bug here is discoverable
    forever rather than overwritten;
  * a refund is idempotent from ledger state, not from an idempotency key. The
    reaper re-emits compensations with fresh keys, so a key-based guard would
    happily refund the same charge twice.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

STATUS_SUCCESS = "SUCCESS"
STATUS_REFUNDED = "REFUNDED"
STATUS_REFUND_NOOP = "REFUND_NOOP"

# A settled order is one a refund has already resolved, either by reversing a
# charge or by recording that there was nothing to reverse.
SETTLED_STATUSES = (STATUS_REFUNDED, STATUS_REFUND_NOOP)


class RefundOutcome(str, Enum):
    ALREADY_SETTLED = "already_settled"  # a prior attempt resolved this order
    NO_CHARGE = "no_charge"              # nothing was ever charged; blind no-op
    REVERSED = "reversed"                # a real charge was reversed


@dataclass(frozen=True)
class LedgerEntry:
    """The only fields the decision needs. Keeps the rules ORM-independent."""

    status: str
    amount_cents: int


@dataclass(frozen=True)
class RefundPlan:
    outcome: RefundOutcome
    refunded: bool               # did money actually go back to the customer
    refunded_cents: int          # magnitude, always >= 0, for reporting
    append_status: Optional[str] # ledger row to append; None = append nothing
    append_amount_cents: int     # signed; negative for a real reversal
    emit_event: bool             # emit PaymentRefunded so the saga can settle
    detail: str


def plan_refund(prior: Optional[LedgerEntry], charge: Optional[LedgerEntry]) -> RefundPlan:
    """Decide what a refund attempt should do.

    `prior`  -- an existing REFUNDED or REFUND_NOOP row for this order, if any
    `charge` -- an existing SUCCESS row for this order, if any

    Pure and total: every combination yields a plan, and the same inputs always
    yield the same plan.
    """
    if prior is not None:
        # A retry. Report what happened the first time and touch nothing --
        # appending here is what a second refund looks like.
        return RefundPlan(
            outcome=RefundOutcome.ALREADY_SETTLED,
            refunded=prior.status == STATUS_REFUNDED,
            refunded_cents=abs(prior.amount_cents),
            append_status=None,
            append_amount_cents=0,
            emit_event=False,
            detail="Already settled — returning prior outcome",
        )

    if charge is None:
        # Blind compensation: the saga compensates both legs on timeout because
        # it cannot know whether a charge landed before the ack was lost.
        # Erroring here would strand the saga at TIMED_OUT forever, so this
        # succeeds as a no-op -- and still emits, so the saga can settle.
        return RefundPlan(
            outcome=RefundOutcome.NO_CHARGE,
            refunded=False,
            refunded_cents=0,
            append_status=STATUS_REFUND_NOOP,
            append_amount_cents=0,
            emit_event=True,
            detail="No charge found for this order — recorded as no-op",
        )

    # Append a reversing entry; the original charge is never mutated.
    return RefundPlan(
        outcome=RefundOutcome.REVERSED,
        refunded=True,
        refunded_cents=charge.amount_cents,
        append_status=STATUS_REFUNDED,
        append_amount_cents=-charge.amount_cents,
        emit_event=True,
        detail=f"Refunded {charge.amount_cents} cents",
    )


def net_position(entries) -> int:
    """Sum of signed amounts for an order.

    Zero means the customer is square. This is the property the append-only
    ledger exists to make checkable.
    """
    return sum(e.amount_cents for e in entries)
