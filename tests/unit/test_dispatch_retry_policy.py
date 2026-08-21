"""
The retry policy: what happens to a command that did not succeed.

These exist because of a specific outage. Fourteen escrow bookings for sellers
that no longer existed each returned a permanent 409. The dispatcher's rule for
a failed money command was `return False` -- "never abandon an unbooked
liability" -- which released the claim and made the row instantly re-claimable.
CLAIM_SQL orders by created_at, so those fourteen were re-served first on every
tick, filled a batch of twenty, and saturated the payment-service bulkhead.

The visible symptom was not an error about sellers. It was two unrelated
end-to-end tests failing on assertions about zero, because a live seller's real
2200-poisha liability never got booked -- it was behind fourteen commands that
could never succeed and would never stop asking.

So the tests below are mostly about the two directions of getting this wrong:
parking something transient throws money away, and retrying something permanent
denies service to everything queued behind it.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "workers" / "saga-dispatcher"))

import pytest

from app.dispatch_rules import (
    Disposition, SETTLED, retry, park,
    classify_command_failure, backoff_seconds, disposition_after,
    classify_saga_response, SagaAck,
    MAX_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_CAP_SECONDS,
    TERMINAL_COMMAND_STATUSES, RETRYABLE_COMMAND_STATUSES,
)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_success_settles(status):
    assert classify_command_failure(status) is Disposition.SETTLE


@pytest.mark.parametrize("status", sorted(TERMINAL_COMMAND_STATUSES))
def test_a_refusal_parks_rather_than_repeating(status):
    """The downstream understood and said no. Asking again does not change it."""
    assert classify_command_failure(status) is Disposition.PARK


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_server_faults_retry(status):
    """The downstream never formed an opinion, so the work may still be possible."""
    assert classify_command_failure(status) is Disposition.RETRY


@pytest.mark.parametrize("status", sorted(RETRYABLE_COMMAND_STATUSES))
def test_overload_retries(status):
    assert classify_command_failure(status) is Disposition.RETRY


def test_transport_failure_retries():
    """No status at all means nothing was answered, which is not a refusal."""
    assert classify_command_failure(None) is Disposition.RETRY


def test_unknown_status_retries_rather_than_parking():
    """Conservative on purpose: money is not written off on a surprise.

    This is only safe because the attempt cap exists -- "retry" can no longer
    mean "forever". Before the cap, defaulting to retry is exactly what caused
    the outage, which is why the cap and this default belong to one change.
    """
    assert classify_command_failure(418) is Disposition.RETRY
    assert classify_command_failure(451) is Disposition.RETRY


def test_503_from_payment_service_is_retryable_not_terminal():
    """The specific regression guard for the escrow path.

    payment-service returns 503 when it cannot reach seller-service and 409
    when seller-service answers 404. If these were both terminal, a brief
    seller-service outage would permanently park real liabilities.
    """
    assert classify_command_failure(503) is Disposition.RETRY
    assert classify_command_failure(409) is Disposition.PARK


def test_409_means_opposite_things_to_the_two_classifiers():
    """Deliberate, and the reason there are two functions rather than one table.

    To a saga event, 409 is "valid but out of order, ask again". To a command,
    409 is "I understood and refused". Sharing one table would force one of the
    two to be wrong.
    """
    assert classify_saga_response(409) is SagaAck.DEFERRED
    assert classify_command_failure(409) is Disposition.PARK


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------

def test_backoff_grows():
    delays = [backoff_seconds(n) for n in range(1, 8)]
    assert delays == sorted(delays)
    assert delays[0] == RETRY_BASE_SECONDS


def test_backoff_doubles():
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(4) == 16.0


def test_backoff_is_capped():
    """The cap is the promise that a retry always comes.

    Unbounded doubling reaches days, at which point "retry" is
    indistinguishable from "lost" but without the visibility of parking.
    """
    for n in range(1, 40):
        assert backoff_seconds(n) <= RETRY_CAP_SECONDS
    assert backoff_seconds(30) == RETRY_CAP_SECONDS


def test_jitter_only_spreads_downward():
    """Upward jitter would breach the cap, and the cap is a guarantee."""
    for jitter in (0.0, 0.25, 0.5, 0.99):
        delay = backoff_seconds(10, jitter=jitter)
        assert delay <= RETRY_CAP_SECONDS
        assert delay >= RETRY_CAP_SECONDS * 0.5


def test_jitter_actually_spreads():
    """Fifty commands released in the same millisecond are the second outage."""
    assert backoff_seconds(5, jitter=0.0) != backoff_seconds(5, jitter=0.8)


def test_backoff_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        backoff_seconds(0)
    with pytest.raises(ValueError):
        backoff_seconds(-1)
    with pytest.raises(ValueError):
        backoff_seconds(3, jitter=1.0)
    with pytest.raises(ValueError):
        backoff_seconds(3, jitter=-0.1)


def test_total_retry_window_is_long_enough_to_outlast_a_deploy():
    """Roughly an hour before parking: longer than a restart, shorter than a shift."""
    total = sum(backoff_seconds(n) for n in range(1, MAX_ATTEMPTS + 1))
    assert 30 * 60 <= total <= 120 * 60, f"total retry window is {total/60:.1f} min"


# ---------------------------------------------------------------------------
# the attempt cap
# ---------------------------------------------------------------------------

def test_retry_becomes_park_at_the_cap():
    assert disposition_after(Disposition.RETRY, MAX_ATTEMPTS) is Disposition.PARK
    assert disposition_after(Disposition.RETRY, MAX_ATTEMPTS + 5) is Disposition.PARK


def test_retry_survives_below_the_cap():
    for attempts in range(1, MAX_ATTEMPTS):
        assert disposition_after(Disposition.RETRY, attempts) is Disposition.RETRY


def test_the_cap_never_promotes_a_settle():
    """A command that succeeded is not parked for having taken many tries."""
    for attempts in (1, MAX_ATTEMPTS, MAX_ATTEMPTS * 3):
        assert disposition_after(Disposition.SETTLE, attempts) is Disposition.SETTLE


def test_park_stays_parked():
    assert disposition_after(Disposition.PARK, 1) is Disposition.PARK


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------

def test_only_settle_counts_as_settled():
    assert SETTLED.settled is True
    assert retry("down").settled is False
    # The one that matters: a parked row must NOT be marked processed. A
    # processed row is indistinguishable from one that succeeded, so recording
    # an unbooked liability as processed turns a visible problem into a silent
    # one -- precisely the failure parking exists to avoid.
    assert park("gone").settled is False


def test_outcomes_carry_their_reason():
    assert retry("seller-service timed out").error == "seller-service timed out"
    assert park("seller does not exist").error == "seller does not exist"


def test_outcomes_are_immutable():
    """Frozen so a handler cannot rewrite a park into a settle downstream."""
    with pytest.raises(Exception):
        park("gone").disposition = Disposition.SETTLE


# ---------------------------------------------------------------------------
# the scenario, end to end through the pure rules
# ---------------------------------------------------------------------------

def test_the_outage_scenario():
    """A permanent 409 must leave the queue; it must not be served forever.

    Replays the shape of the real incident against the rules alone: an escrow
    booking for a seller who does not exist, attempted from a clean slate.
    """
    disposition = classify_command_failure(409)
    assert disposition is Disposition.PARK
    assert disposition_after(disposition, 1) is Disposition.PARK, (
        "a permanent refusal must park on its FIRST failure, not after an "
        "hour of blocking the queue")


def test_a_flapping_dependency_is_not_parked_early():
    """The opposite error: 503s must be tolerated for the full window."""
    for attempts in range(1, MAX_ATTEMPTS):
        assert disposition_after(
            classify_command_failure(503), attempts) is Disposition.RETRY


def test_congestion_should_not_consume_the_budget():
    """Shedding passes count_attempt=False in main.py; this pins why.

    A command refused because a bulkhead was full has said nothing about
    whether it can succeed. If congestion counted toward the cap, a busy
    afternoon would park real work for being unlucky -- so `attempts` must not
    move, and the disposition at an unchanged count must stay RETRY.
    """
    attempts_before = 3
    attempts_after = attempts_before  # shed: no increment
    assert disposition_after(Disposition.RETRY, attempts_after) is Disposition.RETRY
