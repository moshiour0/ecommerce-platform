"""
Unit tests for the payment refund decision.

This is the money path. A mistake here either keeps a customer's money or
returns it twice, and neither is visible in a happy-path checkout test.

Run:  python -m pytest tests/unit -q
"""

import pytest

from conftest import refund_rules

LedgerEntry = refund_rules.LedgerEntry
RefundOutcome = refund_rules.RefundOutcome
plan_refund = refund_rules.plan_refund
net_position = refund_rules.net_position
STATUS_SUCCESS = refund_rules.STATUS_SUCCESS
STATUS_REFUNDED = refund_rules.STATUS_REFUNDED
STATUS_REFUND_NOOP = refund_rules.STATUS_REFUND_NOOP
SETTLED_STATUSES = refund_rules.SETTLED_STATUSES


def charge(cents: int) -> LedgerEntry:
    return LedgerEntry(status=STATUS_SUCCESS, amount_cents=cents)


# ---------------------------------------------------------------------------
# reversing a real charge
# ---------------------------------------------------------------------------

def test_reversal_is_the_exact_negative_of_the_charge():
    plan = plan_refund(prior=None, charge=charge(4999))
    assert plan.outcome is RefundOutcome.REVERSED
    assert plan.append_amount_cents == -4999
    assert plan.refunded is True
    assert plan.refunded_cents == 4999


def test_charge_plus_reversal_nets_to_zero():
    """The property the append-only ledger exists to make checkable."""
    plan = plan_refund(prior=None, charge=charge(15000))
    entries = [charge(15000), LedgerEntry(plan.append_status, plan.append_amount_cents)]
    assert net_position(entries) == 0


@pytest.mark.parametrize("cents", [1, 99, 4999, 15000, 2_147_483_647])
def test_reversal_nets_to_zero_at_any_amount(cents):
    plan = plan_refund(prior=None, charge=charge(cents))
    assert net_position([charge(cents),
                         LedgerEntry(plan.append_status, plan.append_amount_cents)]) == 0


def test_a_reversal_is_never_positive():
    """A positive 'reversal' would charge the customer a second time."""
    for cents in (1, 500, 999_999):
        plan = plan_refund(prior=None, charge=charge(cents))
        assert plan.append_amount_cents < 0, "reversal must be negative"


def test_reported_amount_is_a_magnitude_not_the_signed_entry():
    """refunded_cents is for humans and APIs; the sign lives in the ledger."""
    plan = plan_refund(prior=None, charge=charge(4999))
    assert plan.refunded_cents == 4999
    assert plan.append_amount_cents == -4999


def test_reversal_emits_so_the_saga_can_settle():
    assert plan_refund(prior=None, charge=charge(4999)).emit_event is True


# ---------------------------------------------------------------------------
# blind compensation -- refunding something that was never charged
# ---------------------------------------------------------------------------

def test_no_charge_is_a_successful_no_op():
    """The saga compensates both legs on timeout because it cannot know whether
    a charge landed before the acknowledgement was lost. Erroring here would
    strand the saga at TIMED_OUT forever."""
    plan = plan_refund(prior=None, charge=None)
    assert plan.outcome is RefundOutcome.NO_CHARGE
    assert plan.refunded is False
    assert plan.refunded_cents == 0


def test_no_charge_still_emits_so_the_saga_reaches_rollback_completed():
    """Not emitting is what leaves a timed-out saga stuck forever."""
    assert plan_refund(prior=None, charge=None).emit_event is True


def test_no_charge_records_a_zero_row_not_nothing():
    """The no-op is recorded so a later retry is cheap and auditable."""
    plan = plan_refund(prior=None, charge=None)
    assert plan.append_status == STATUS_REFUND_NOOP
    assert plan.append_amount_cents == 0


def test_no_charge_never_moves_money():
    plan = plan_refund(prior=None, charge=None)
    assert net_position([LedgerEntry(plan.append_status, plan.append_amount_cents)]) == 0


# ---------------------------------------------------------------------------
# idempotency -- from ledger state, not from a key
# ---------------------------------------------------------------------------

def test_second_attempt_after_a_reversal_appends_nothing():
    """The single most expensive bug available here: refunding twice."""
    prior = LedgerEntry(STATUS_REFUNDED, -4999)
    plan = plan_refund(prior=prior, charge=charge(4999))
    assert plan.outcome is RefundOutcome.ALREADY_SETTLED
    assert plan.append_status is None, "a retry must not append a second reversal"
    assert plan.append_amount_cents == 0


def test_second_attempt_reports_the_original_outcome():
    prior = LedgerEntry(STATUS_REFUNDED, -4999)
    plan = plan_refund(prior=prior, charge=charge(4999))
    assert plan.refunded is True
    assert plan.refunded_cents == 4999


def test_retry_after_a_no_op_reports_not_refunded():
    """A no-op retry must not claim money was returned."""
    plan = plan_refund(prior=LedgerEntry(STATUS_REFUND_NOOP, 0), charge=None)
    assert plan.outcome is RefundOutcome.ALREADY_SETTLED
    assert plan.refunded is False
    assert plan.refunded_cents == 0


def test_settled_order_never_emits_a_duplicate_event():
    for prior in (LedgerEntry(STATUS_REFUNDED, -4999), LedgerEntry(STATUS_REFUND_NOOP, 0)):
        assert plan_refund(prior=prior, charge=None).emit_event is False


def test_prior_settlement_wins_even_when_a_charge_is_present():
    """Ledger state decides, not the presence of a charge row."""
    plan = plan_refund(prior=LedgerEntry(STATUS_REFUNDED, -4999), charge=charge(4999))
    assert plan.outcome is RefundOutcome.ALREADY_SETTLED
    assert plan.append_status is None


def test_repeated_attempts_converge_and_never_move_more_money():
    """Simulate the reaper re-emitting with fresh idempotency keys.

    A key-based guard would not catch this: each attempt carries a different
    key. Only ledger state does.
    """
    ledger = [charge(4999)]
    for _ in range(10):
        prior = next((e for e in ledger if e.status in SETTLED_STATUSES), None)
        chg = next((e for e in ledger if e.status == STATUS_SUCCESS), None)
        plan = plan_refund(prior=prior, charge=chg)
        if plan.append_status:
            ledger.append(LedgerEntry(plan.append_status, plan.append_amount_cents))

    reversals = [e for e in ledger if e.status == STATUS_REFUNDED]
    assert len(reversals) == 1, f"money returned {len(reversals)} times"
    assert net_position(ledger) == 0


def test_repeated_attempts_on_an_uncharged_order_record_one_no_op():
    ledger = []
    for _ in range(10):
        prior = next((e for e in ledger if e.status in SETTLED_STATUSES), None)
        plan = plan_refund(prior=prior, charge=None)
        if plan.append_status:
            ledger.append(LedgerEntry(plan.append_status, plan.append_amount_cents))
    assert len(ledger) == 1
    assert net_position(ledger) == 0


# ---------------------------------------------------------------------------
# totality and purity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prior", [None,
                                   LedgerEntry(STATUS_REFUNDED, -100),
                                   LedgerEntry(STATUS_REFUND_NOOP, 0)])
@pytest.mark.parametrize("chg", [None, LedgerEntry(STATUS_SUCCESS, 100)])
def test_every_combination_yields_a_plan(prior, chg):
    plan = plan_refund(prior=prior, charge=chg)
    assert isinstance(plan.outcome, RefundOutcome)
    assert plan.refunded_cents >= 0, "reported amount is a magnitude"
    if plan.append_status is None:
        assert plan.append_amount_cents == 0


def test_plan_refund_is_pure():
    a = plan_refund(prior=None, charge=charge(4999))
    b = plan_refund(prior=None, charge=charge(4999))
    assert a == b


def test_only_a_reversal_reports_money_returned():
    """refunded=True must mean money actually moved back."""
    assert plan_refund(None, charge(1)).refunded is True
    assert plan_refund(None, None).refunded is False
    assert plan_refund(LedgerEntry(STATUS_REFUND_NOOP, 0), None).refunded is False


def test_settled_statuses_cover_both_terminal_ledger_outcomes():
    """If a status is missing here, its rows are invisible to the retry check
    and a second refund becomes possible."""
    assert set(SETTLED_STATUSES) == {STATUS_REFUNDED, STATUS_REFUND_NOOP}
    assert STATUS_SUCCESS not in SETTLED_STATUSES, "a charge is not a settlement"
