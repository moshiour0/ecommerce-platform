"""
Unit tests for notification delivery decisions.

Two things are being pinned: that a permanent failure is never retried no
matter how many attempts remain, and that the retry schedule actually backs off
and actually carries jitter. Both are the sort of thing that looks fine in code
review and is discovered in production by a provider falling over twice.
"""

import pytest

from conftest import notification_rules

Disposition = notification_rules.Disposition
classify = notification_rules.classify
backoff_delay = notification_rules.backoff_delay
is_valid_channel = notification_rules.is_valid_channel
MAX_ATTEMPTS = notification_rules.MAX_ATTEMPTS

# Deterministic "random" sources, so a schedule can be asserted exactly.
NO_JITTER = lambda: 0.0        # noqa: E731 -- the minimum of the jitter window
FULL_JITTER = lambda: 0.999    # noqa: E731 -- effectively the maximum
MID_JITTER = lambda: 0.5       # noqa: E731


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_delivered_is_terminal():
    d = classify("delivered", attempt=1, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.DELIVERED
    assert d.terminal
    assert d.delay_seconds is None


@pytest.mark.parametrize("code", sorted(notification_rules.PERMANENT_FAILURES))
def test_permanent_failures_are_never_retried(code):
    # Even on the first attempt with four left, these must not be tried again.
    d = classify(code, attempt=1, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.FAILED, f"{code} was retried"
    assert d.delay_seconds is None


def test_unsubscribed_is_permanent_specifically():
    # Retrying this one is not merely wasteful: it is the exact thing the
    # recipient asked us to stop doing.
    assert classify("unsubscribed", 1, NO_JITTER).disposition is Disposition.FAILED


@pytest.mark.parametrize("code", sorted(notification_rules.RETRYABLE_FAILURES))
def test_transient_failures_are_retried(code):
    d = classify(code, attempt=1, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.RETRY
    assert d.delay_seconds is not None and d.delay_seconds > 0


def test_an_unrecognised_result_is_retried_not_abandoned():
    # Fail toward trying again: wrongly giving up on a real notification costs
    # more than a few wasted attempts, and max_attempts keeps it bounded.
    d = classify("something_new_from_the_provider", attempt=1,
                 random_fraction=NO_JITTER)
    assert d.disposition is Disposition.RETRY
    assert "unrecognised" in d.detail


def test_attempts_are_exhausted_eventually():
    d = classify("timeout", attempt=MAX_ATTEMPTS, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.FAILED
    assert "giving up" in d.detail


def test_the_last_allowed_attempt_still_retries():
    d = classify("timeout", attempt=MAX_ATTEMPTS - 1, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.RETRY


def test_exhaustion_does_not_override_permanent():
    # Both conditions are true at once; the reason reported should be the
    # permanent one, because that is what someone reading the log needs.
    d = classify("invalid_address", attempt=MAX_ATTEMPTS, random_fraction=NO_JITTER)
    assert d.disposition is Disposition.FAILED
    assert "permanent" in d.detail


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------

def test_backoff_grows_with_each_attempt():
    delays = [backoff_delay(a, NO_JITTER) for a in range(1, 6)]
    assert delays == sorted(delays), f"backoff did not grow: {delays}"
    assert delays[0] < delays[-1]


def test_backoff_is_capped():
    # Without a cap, attempt 30 schedules a retry years away.
    assert backoff_delay(30, FULL_JITTER) <= notification_rules.MAX_DELAY_SECONDS


def test_a_huge_attempt_number_does_not_explode():
    # Guards the intermediate 2**n, not just the final value.
    assert backoff_delay(10_000, FULL_JITTER) <= notification_rules.MAX_DELAY_SECONDS


def test_jitter_actually_varies_the_delay():
    # The whole point: two notifications failing at the same instant must not
    # be scheduled to retry at the same instant.
    assert backoff_delay(3, NO_JITTER) != backoff_delay(3, FULL_JITTER)


def test_jitter_stays_within_its_window():
    # Equal jitter: never below half the window, never above it.
    for attempt in range(1, 8):
        low = backoff_delay(attempt, NO_JITTER)
        high = backoff_delay(attempt, FULL_JITTER)
        mid = backoff_delay(attempt, MID_JITTER)
        assert low <= mid <= high
        assert low >= 1, "a retry must not be scheduled immediately"
        assert high <= 2 * low + 1, (
            f"jitter window is wider than half the delay at attempt {attempt}")


def test_delay_is_never_zero_or_negative():
    for attempt in (0, -5, 1, 2, 50):
        assert backoff_delay(attempt, NO_JITTER) >= 1


def test_backoff_returns_whole_seconds():
    assert isinstance(backoff_delay(3, MID_JITTER), int)


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

def test_known_channels_are_valid():
    for channel in ("email", "sms", "push"):
        assert is_valid_channel(channel)


def test_unknown_channels_are_not():
    for channel in ("fax", "", "EMAIL", None, "carrier_pigeon"):
        assert not is_valid_channel(channel)
