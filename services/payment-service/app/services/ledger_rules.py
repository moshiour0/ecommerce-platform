"""
The escrow ledger, as pure functions.

Under cash on delivery the platform never touches the buyer's money at
checkout. A courier collects it at the door, holds it for days, and remits it
in a batch. Between the door and the payout the platform owes a seller money it
does not yet have — and that is a liability, which means it belongs in a ledger
from the moment of delivery rather than being computed at payout time by
summing orders (§3d).

The difference is not bookkeeping pedantry. A number derived from orders at
payout time answers "what do we think we owe" and cannot answer "what did we
owe last Tuesday", "why is this seller's balance different from the sum of
their orders", or "which of these two numbers is wrong". A ledger answers all
three because every change is an entry with a reason attached.

Double entry, and the invariant
-------------------------------
Every event produces a *set* of entries that sums to zero. Debits are positive
and credits negative, so `sum(amounts) == 0` is the whole invariant and it is
checkable on every write. An unbalanced set is refused rather than stored:
money that appears from nowhere in a ledger is worse than a failed request,
because the failure is loud and the imbalance is not.

Rounding
--------
Commission is a percentage of an integer amount, so it rounds. The rule is that
**one side is computed and the other is derived**:

    commission = amount * rate // 10000
    seller     = amount - commission

Computing both from the rate independently is how a poisha goes missing on
roughly half of all orders — each side rounds on its own and the two no longer
sum to what the courier collected. Deriving the second side makes the identity
true by construction rather than by luck.

Flooring the commission rounds in the seller's favour. That is a policy choice
rather than an arithmetic one, and it is the right way round: a marketplace
that rounds fractions towards itself is doing something it would not want to
explain.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class Account(str, Enum):
    """The four accounts a COD order touches.

    Deliberately few. A chart of accounts grows to fit an accounting
    department; this one exists to answer what the platform holds and what it
    owes, and every extra account is another place for the two to disagree.
    """

    # Asset. Cash a courier has collected and not yet remitted. The platform is
    # owed this by the courier.
    COURIER_RECEIVABLE = "COURIER_RECEIVABLE"

    # Asset. Cash actually in the platform's account.
    CASH = "CASH"

    # Liability. What the platform owes sellers. This is the escrow balance.
    SELLER_PAYABLE = "SELLER_PAYABLE"

    # Income. The platform's commission, recognised at delivery.
    COMMISSION_REVENUE = "COMMISSION_REVENUE"


class EntryReason(str, Enum):
    DELIVERY = "DELIVERY"        # courier collected at the door
    SETTLEMENT = "SETTLEMENT"    # courier remitted to the platform
    PAYOUT = "PAYOUT"            # platform paid the seller


class LedgerError(ValueError):
    """An entry set that must not be written."""


@dataclass(frozen=True)
class Entry:
    """One line. Positive is a debit, negative is a credit."""

    account: Account
    amount_cents: int
    reason: EntryReason
    seller_id: Optional[str] = None
    seller_order_id: Optional[str] = None
    detail: str = ""

    @property
    def is_debit(self) -> bool:
        return self.amount_cents > 0


def split_commission(amount_cents: int, rate_bps: int) -> tuple:
    """(commission, seller_share), summing to amount_cents exactly.

    `rate_bps` is basis points -- 1250 is 12.5% -- because a percentage held as
    a float reintroduces the binary floating point Rule 6 forbids, and does it
    in the one place where a fraction of a unit is money somebody notices.

    The seller's share is derived rather than computed, so the two always sum
    to what the courier collected whatever the rate does.
    """
    if amount_cents < 0:
        raise LedgerError(f"amount must not be negative, got {amount_cents}")
    if not 0 <= rate_bps <= 10000:
        raise LedgerError(
            f"commission rate {rate_bps} bps is outside 0-10000; a rate above "
            f"100% would make the seller owe the platform for selling")

    commission = amount_cents * rate_bps // 10000
    return commission, amount_cents - commission


def assert_balanced(entries: List[Entry]) -> None:
    """Refuse an entry set that does not sum to zero.

    Called before every write. Money appearing from nowhere in a ledger is
    worse than a rejected request: the rejection is loud and the imbalance is
    silent until somebody reconciles a bank statement.
    """
    if not entries:
        raise LedgerError("an empty entry set is not a transaction")

    total = sum(entry.amount_cents for entry in entries)
    if total != 0:
        raise LedgerError(
            f"entries do not balance: they sum to {total}, not 0. "
            f"{[(e.account.value, e.amount_cents) for e in entries]}")


def plan_delivery(seller_id: str, seller_order_id: str, collected_cents: int,
                  commission_rate_bps: int) -> List[Entry]:
    """The buyer paid the courier at the door.

    Three lines: the courier now owes the platform the cash, the platform now
    owes the seller their share, and the platform has earned its commission.

    Booked at delivery rather than at settlement because that is when the
    obligation arises. Waiting for the courier to remit would mean the platform
    owes sellers money that appears nowhere in its own books for days, which is
    exactly the period when somebody asks how much is outstanding.
    """
    if collected_cents <= 0:
        raise LedgerError(
            f"a delivery must collect a positive amount, got {collected_cents}")

    commission, seller_share = split_commission(collected_cents,
                                                commission_rate_bps)

    entries = [
        Entry(Account.COURIER_RECEIVABLE, collected_cents, EntryReason.DELIVERY,
              seller_id, seller_order_id,
              f"courier collected {collected_cents}"),
        Entry(Account.SELLER_PAYABLE, -seller_share, EntryReason.DELIVERY,
              seller_id, seller_order_id,
              f"owed to seller after {commission_rate_bps}bps commission"),
    ]
    if commission:
        entries.append(
            Entry(Account.COMMISSION_REVENUE, -commission, EntryReason.DELIVERY,
                  seller_id, seller_order_id,
                  f"commission at {commission_rate_bps}bps"))

    assert_balanced(entries)
    return entries


def plan_settlement(seller_id: str, seller_order_id: str,
                    remitted_cents: int) -> List[Entry]:
    """The courier handed the cash over.

    Two lines, and neither touches what the seller is owed: settlement moves
    money from one platform asset to another. The seller's balance was decided
    at delivery and does not change because a courier was slow.

    Deliberately does not verify the amount against the delivery. That check
    belongs to reconciliation in fulfillment-service, which classifies a short
    payment as an alert; booking a short remittance here as if it were correct
    would hide the shortfall inside a balanced pair of entries.
    """
    if remitted_cents <= 0:
        raise LedgerError(
            f"a settlement must remit a positive amount, got {remitted_cents}")

    entries = [
        Entry(Account.CASH, remitted_cents, EntryReason.SETTLEMENT,
              seller_id, seller_order_id, f"courier remitted {remitted_cents}"),
        Entry(Account.COURIER_RECEIVABLE, -remitted_cents,
              EntryReason.SETTLEMENT, seller_id, seller_order_id,
              "clearing the courier receivable"),
    ]
    assert_balanced(entries)
    return entries


def plan_payout(seller_id: str, amount_cents: int) -> List[Entry]:
    """The platform paid the seller.

    Not attached to a seller order: a payout settles a balance across many
    orders, and pretending otherwise would make every payout an arbitrary
    allocation across the orders it happened to cover.
    """
    if amount_cents <= 0:
        raise LedgerError(
            f"a payout must be a positive amount, got {amount_cents}")

    entries = [
        Entry(Account.SELLER_PAYABLE, amount_cents, EntryReason.PAYOUT,
              seller_id, None, f"paid out {amount_cents}"),
        Entry(Account.CASH, -amount_cents, EntryReason.PAYOUT,
              seller_id, None, "cash leaving the platform"),
    ]
    assert_balanced(entries)
    return entries


def balance_of(entries: List[Entry], account: Account) -> int:
    """The signed balance of one account.

    Debits positive. A liability like SELLER_PAYABLE therefore reads negative
    when the platform owes money, which is correct double-entry and confusing
    to look at, so `seller_owed` exists for the one anybody actually asks for.
    """
    return sum(e.amount_cents for e in entries if e.account is account)


def seller_owed(entries: List[Entry], seller_id: str = None) -> int:
    """What the platform still owes, as a positive number.

    The question a seller asks and the number a payout run needs, with the
    sign flipped out of accounting convention and into ordinary language.
    """
    relevant = [e for e in entries
                if e.account is Account.SELLER_PAYABLE
                and (seller_id is None or e.seller_id == seller_id)]
    return -sum(e.amount_cents for e in relevant)


def is_ledger_consistent(entries: List[Entry]) -> bool:
    """Whether the whole ledger balances.

    The single check worth running over everything ever written. If it fails,
    some transaction was stored unbalanced and the platform's books are wrong
    by exactly that amount.
    """
    return sum(e.amount_cents for e in entries) == 0
