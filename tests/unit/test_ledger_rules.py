"""
Unit tests for the escrow ledger.

Two properties, and both are the kind that fail silently.

**Every transaction balances.** A ledger that stores an unbalanced entry set
is wrong by exactly that amount forever, and nothing notices until somebody
reconciles a bank statement months later. So `assert_balanced` runs before
every write and these tests try to get past it.

**Rounding never loses a unit.** Commission is a percentage of an integer, so
it rounds on most orders. Computing the commission and the seller's share
independently from the rate makes them disagree with the collected amount on
roughly half of all orders -- a poisha at a time, in the platform's favour or
the seller's depending on the remainder. The share is therefore *derived*, and
the exhaustive test below is the proof rather than the intention.
"""

import pytest

from conftest import ledger_rules

Account = ledger_rules.Account
EntryReason = ledger_rules.EntryReason
LedgerError = ledger_rules.LedgerError
Entry = ledger_rules.Entry

split_commission = ledger_rules.split_commission
assert_balanced = ledger_rules.assert_balanced
plan_delivery = ledger_rules.plan_delivery
plan_settlement = ledger_rules.plan_settlement
plan_payout = ledger_rules.plan_payout
balance_of = ledger_rules.balance_of
seller_owed = ledger_rules.seller_owed
is_ledger_consistent = ledger_rules.is_ledger_consistent

RATE = 1250  # 12.5%


# ---------------------------------------------------------------------------
# rounding
# ---------------------------------------------------------------------------

def test_commission_and_share_always_sum_to_what_was_collected():
    """Exhaustive over a wide range, because this is the failure mode.

    Computing both sides from the rate independently loses or gains a unit
    whenever the percentage does not divide exactly -- which is most of the
    time. Deriving the share makes the identity true by construction, and this
    asserts it rather than trusting the reasoning.
    """
    rates = (0, 1, 7, 250, 999, 1250, 3333, 5000, 9999, 10000)
    for amount in range(0, 2000):
        for rate in rates:
            commission, share = split_commission(amount, rate)
            assert commission + share == amount, \
                f"{amount} at {rate}bps split into {commission}+{share}"


def test_rounding_favours_the_seller():
    # 12.5% of 1250 is 156.25. The platform takes 156, not 157: a marketplace
    # that rounds fractions towards itself is doing something it would not
    # want to explain.
    commission, share = split_commission(1250, 1250)
    assert commission == 156
    assert share == 1094


def test_neither_side_is_ever_negative():
    for amount in (0, 1, 99, 100000):
        for rate in (0, 1, 5000, 10000):
            commission, share = split_commission(amount, rate)
            assert commission >= 0 and share >= 0


def test_a_zero_rate_gives_the_seller_everything():
    assert split_commission(1000, 0) == (0, 1000)


def test_a_full_rate_gives_the_seller_nothing():
    assert split_commission(1000, 10000) == (1000, 0)


@pytest.mark.parametrize("rate", [-1, 10001, 20000])
def test_a_rate_outside_zero_to_one_hundred_percent_is_refused(rate):
    # Above 100% the seller would owe the platform for making a sale.
    with pytest.raises(LedgerError, match="outside 0-10000"):
        split_commission(1000, rate)


def test_a_negative_amount_is_refused():
    with pytest.raises(LedgerError, match="not be negative"):
        split_commission(-1, RATE)


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------

def test_every_planned_transaction_balances():
    for entries in (plan_delivery("s", "so", 1250, RATE),
                    plan_settlement("s", "so", 1250),
                    plan_payout("s", 1094)):
        assert sum(e.amount_cents for e in entries) == 0


def test_an_unbalanced_set_is_refused():
    entries = [Entry(Account.CASH, 100, EntryReason.DELIVERY),
               Entry(Account.SELLER_PAYABLE, -90, EntryReason.DELIVERY)]
    with pytest.raises(LedgerError, match="do not balance"):
        assert_balanced(entries)


def test_the_imbalance_is_named_in_the_error():
    # An operator reading this needs the number, not just the fact.
    entries = [Entry(Account.CASH, 100, EntryReason.DELIVERY)]
    with pytest.raises(LedgerError, match="sum to 100"):
        assert_balanced(entries)


def test_an_empty_set_is_not_a_transaction():
    with pytest.raises(LedgerError, match="empty entry set"):
        assert_balanced([])


def test_balance_holds_at_every_rate():
    for rate in (0, 1, 333, 1250, 9999, 10000):
        entries = plan_delivery("s", "so", 999, rate)
        assert sum(e.amount_cents for e in entries) == 0, \
            f"delivery at {rate}bps did not balance"


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def test_delivery_books_what_the_courier_owes_and_what_the_seller_is_owed():
    entries = plan_delivery("seller-1", "so-1", 1250, RATE)
    assert balance_of(entries, Account.COURIER_RECEIVABLE) == 1250
    assert balance_of(entries, Account.SELLER_PAYABLE) == -1094
    assert balance_of(entries, Account.COMMISSION_REVENUE) == -156


def test_seller_owed_reads_as_a_positive_number():
    # SELLER_PAYABLE is a liability and reads negative in double entry, which
    # is correct and confusing. This is the number a seller asks for.
    assert seller_owed(plan_delivery("s", "so", 1250, RATE)) == 1094


def test_a_zero_commission_books_no_revenue_line():
    entries = plan_delivery("s", "so", 1000, 0)
    assert not [e for e in entries if e.account is Account.COMMISSION_REVENUE]
    assert sum(e.amount_cents for e in entries) == 0


def test_every_delivery_line_carries_the_seller_and_the_order():
    # A ledger entry nobody can attribute is a ledger entry nobody can dispute.
    for entry in plan_delivery("seller-1", "so-1", 500, RATE):
        assert entry.seller_id == "seller-1"
        assert entry.seller_order_id == "so-1"
        assert entry.reason is EntryReason.DELIVERY


@pytest.mark.parametrize("amount", [0, -1, -1000])
def test_a_delivery_must_collect_something(amount):
    with pytest.raises(LedgerError, match="positive amount"):
        plan_delivery("s", "so", amount, RATE)


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------

def test_settlement_moves_money_between_platform_assets_only():
    entries = plan_settlement("s", "so", 1250)
    assert balance_of(entries, Account.CASH) == 1250
    assert balance_of(entries, Account.COURIER_RECEIVABLE) == -1250


def test_settlement_does_not_change_what_the_seller_is_owed():
    # The seller's balance was decided at delivery. A slow courier is the
    # platform's problem, not a reason to owe the seller less.
    delivery = plan_delivery("s", "so", 1250, RATE)
    after = delivery + plan_settlement("s", "so", 1250)
    assert seller_owed(after) == seller_owed(delivery) == 1094


def test_a_short_remittance_still_balances_and_is_not_reconciliation():
    # Deliberately bookable. Whether it *should* be booked is
    # fulfillment-service's reconciliation decision; hiding a shortfall inside
    # a balanced pair here would be the wrong place to catch it.
    entries = plan_settlement("s", "so", 1000)
    assert sum(e.amount_cents for e in entries) == 0
    assert balance_of(entries, Account.COURIER_RECEIVABLE) == -1000


def test_a_settlement_must_remit_something():
    with pytest.raises(LedgerError, match="positive amount"):
        plan_settlement("s", "so", 0)


# ---------------------------------------------------------------------------
# payout
# ---------------------------------------------------------------------------

def test_a_payout_clears_the_liability_and_moves_cash_out():
    entries = plan_payout("seller-1", 1094)
    assert balance_of(entries, Account.SELLER_PAYABLE) == 1094
    assert balance_of(entries, Account.CASH) == -1094


def test_a_payout_is_not_attached_to_one_order():
    # It settles a balance across many; pretending otherwise makes every
    # payout an arbitrary allocation.
    for entry in plan_payout("seller-1", 500):
        assert entry.seller_order_id is None


def test_a_payout_must_be_positive():
    with pytest.raises(LedgerError, match="positive amount"):
        plan_payout("s", 0)


# ---------------------------------------------------------------------------
# the whole lifecycle
# ---------------------------------------------------------------------------

def test_a_full_cycle_leaves_only_the_commission_as_cash():
    entries = (plan_delivery("s", "so", 1250, RATE)
               + plan_settlement("s", "so", 1250)
               + plan_payout("s", 1094))

    assert is_ledger_consistent(entries)
    assert balance_of(entries, Account.COURIER_RECEIVABLE) == 0
    assert balance_of(entries, Account.SELLER_PAYABLE) == 0
    assert balance_of(entries, Account.CASH) == 156
    assert balance_of(entries, Account.COMMISSION_REVENUE) == -156
    assert seller_owed(entries) == 0


def test_two_sellers_balances_do_not_mix():
    entries = (plan_delivery("seller-a", "so-a", 1000, RATE)
               + plan_delivery("seller-b", "so-b", 2000, RATE))
    assert seller_owed(entries, "seller-a") == 875
    assert seller_owed(entries, "seller-b") == 1750
    assert seller_owed(entries) == 2625


def test_the_ledger_stays_consistent_across_many_mixed_events():
    entries = []
    for i in range(1, 60):
        entries += plan_delivery(f"s{i % 5}", f"so{i}", 100 + i * 7,
                                 (i * 137) % 10001)
        if i % 3 == 0:
            entries += plan_settlement(f"s{i % 5}", f"so{i}", 100 + i * 7)
    assert is_ledger_consistent(entries), \
        "the ledger drifted out of balance across a mixed sequence"


def test_paying_out_more_than_is_owed_still_balances_but_shows_negative():
    # The ledger will not stop an overpayment -- it is not an authorisation
    # system -- but the resulting balance is visibly wrong rather than hidden,
    # which is what a ledger is for.
    entries = plan_delivery("s", "so", 1000, RATE) + plan_payout("s", 5000)
    assert is_ledger_consistent(entries)
    assert seller_owed(entries) < 0
