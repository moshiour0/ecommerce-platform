"""
Unit tests for the payment charge decision.

The other half of the money path. A refund bug keeps money that should go
back; a charge bug takes money twice. This file is mostly about the second.

Run:  python -m pytest tests/unit -q
"""

import pytest

from conftest import charge_rules, saga_transitions

authorize = charge_rules.authorize
event_for = charge_rules.event_for
is_declined = charge_rules.is_declined
resolve_idempotency = charge_rules.resolve_idempotency
IdempotencyOutcome = charge_rules.IdempotencyOutcome
STATUS_SUCCESS = charge_rules.STATUS_SUCCESS
STATUS_FAILED = charge_rules.STATUS_FAILED
EVENT_CHARGED = charge_rules.EVENT_CHARGED
EVENT_FAILED = charge_rules.EVENT_FAILED
DECLINE_TOKEN = charge_rules.DECLINE_TOKEN


# ---------------------------------------------------------------------------
# provider outcome
# ---------------------------------------------------------------------------

def test_decline_token_fails():
    assert authorize(DECLINE_TOKEN) == STATUS_FAILED
    assert is_declined(authorize(DECLINE_TOKEN))


@pytest.mark.parametrize("token", ["tok_visa", "internal_saga_bypass", "", None, "tok_fail_but_not"])
def test_every_other_token_succeeds(token):
    assert authorize(token) == STATUS_SUCCESS
    assert not is_declined(authorize(token))


def test_decline_match_is_exact_not_a_prefix():
    """A prefix match would decline any token starting with tok_fail."""
    assert authorize("tok_failure") == STATUS_SUCCESS
    assert authorize("tok_fail ") == STATUS_SUCCESS


def test_authorize_is_pure():
    assert authorize("tok_visa") == authorize("tok_visa")


# ---------------------------------------------------------------------------
# status -> event, a cross-service contract
# ---------------------------------------------------------------------------

def test_success_and_failure_map_to_distinct_events():
    assert event_for(STATUS_SUCCESS) == EVENT_CHARGED
    assert event_for(STATUS_FAILED) == EVENT_FAILED
    assert event_for(STATUS_SUCCESS) != event_for(STATUS_FAILED)


def test_both_charge_events_are_known_to_the_saga():
    """A name the saga does not know is answered 422 and dead-lettered, so the
    saga strands until the reaper sweeps it. Neither service can see this
    alone."""
    known = set(saga_transitions.KNOWN_EVENT_TYPES)
    for status in (STATUS_SUCCESS, STATUS_FAILED):
        assert event_for(status) in known, f"{event_for(status)} unknown to order-saga"


def test_both_charge_events_actually_move_the_saga():
    """Being a known event is not enough -- it must have a transition, or the
    saga answers 409 forever and the order never progresses."""
    events_with_transitions = {e for (_s, e) in saga_transitions.TRANSITIONS}
    for status in (STATUS_SUCCESS, STATUS_FAILED):
        assert event_for(status) in events_with_transitions, \
            f"{event_for(status)} has no saga transition"


def test_an_unrecognised_status_never_reports_success():
    """Fail closed: anything that is not an explicit success is a failure."""
    for status in ("PENDING", "UNKNOWN", "", "success"):
        assert event_for(status) == EVENT_FAILED


# ---------------------------------------------------------------------------
# idempotency -- where a duplicate becomes a double charge
# ---------------------------------------------------------------------------

def test_a_new_key_proceeds():
    d = resolve_idempotency(key_was_inserted=True, stored_result_id=None,
                            cached_result_found=False)
    assert d.outcome is IdempotencyOutcome.PROCEED


def test_a_completed_retry_replays_the_result():
    """Rule 4: the guard is a cache, not a gate. A completed retry gets the
    original result, never an error."""
    d = resolve_idempotency(key_was_inserted=False, stored_result_id="abc",
                            cached_result_found=True)
    assert d.outcome is IdempotencyOutcome.RETURN_CACHED


def test_a_completed_retry_never_charges_again():
    d = resolve_idempotency(key_was_inserted=False, stored_result_id="abc",
                            cached_result_found=True)
    assert d.outcome is not IdempotencyOutcome.PROCEED


def test_an_in_flight_duplicate_never_proceeds():
    """The concurrency case, and the most expensive one available here.

    Two requests share a key; the first inserted it but has not committed its
    result. PROCEED would authorize the card a second time.
    """
    d = resolve_idempotency(key_was_inserted=False, stored_result_id=None,
                            cached_result_found=False)
    assert d.outcome is IdempotencyOutcome.RETRY_LATER
    assert d.outcome is not IdempotencyOutcome.PROCEED


def test_a_dangling_result_reference_never_proceeds():
    """result_id points at a ledger row that no longer exists. Charging again
    would double-charge, so this must not proceed either."""
    d = resolve_idempotency(key_was_inserted=False, stored_result_id="gone",
                            cached_result_found=False)
    assert d.outcome is IdempotencyOutcome.RETRY_LATER
    assert d.outcome is not IdempotencyOutcome.PROCEED


def test_proceed_happens_only_when_the_key_is_genuinely_new():
    """Exhaustive over the decision inputs: the single PROCEED must be the
    branch that actually inserted the key."""
    proceeds = []
    for inserted in (True, False):
        for result_id in (None, "abc"):
            for found in (True, False):
                d = resolve_idempotency(inserted, result_id, found)
                if d.outcome is IdempotencyOutcome.PROCEED:
                    proceeds.append((inserted, result_id, found))
    assert all(p[0] is True for p in proceeds), \
        f"a duplicate key was allowed to charge again: {proceeds}"


def test_every_input_combination_is_decided():
    for inserted in (True, False):
        for result_id in (None, "abc"):
            for found in (True, False):
                d = resolve_idempotency(inserted, result_id, found)
                assert isinstance(d.outcome, IdempotencyOutcome)
                assert d.detail, "every decision explains itself"


def test_resolve_idempotency_is_pure():
    a = resolve_idempotency(False, "abc", True)
    b = resolve_idempotency(False, "abc", True)
    assert a == b


# ---------------------------------------------------------------------------
# Rule 4 as written
# ---------------------------------------------------------------------------

def test_rule_4_a_completed_retry_is_never_an_error():
    """'Returning HTTP 409 on a legitimate retry is a protocol violation.'

    A legitimate retry is one whose original attempt committed. That case must
    replay, and it does.
    """
    d = resolve_idempotency(key_was_inserted=False, stored_result_id="abc",
                            cached_result_found=True)
    assert d.outcome is IdempotencyOutcome.RETURN_CACHED


def test_retry_later_is_reserved_for_genuinely_incomplete_work():
    """RETRY_LATER maps to 409 at the API, so it must never cover a case with
    a committed result -- that would be the Rule 4 violation."""
    completed = resolve_idempotency(False, "abc", True)
    assert completed.outcome is not IdempotencyOutcome.RETRY_LATER
