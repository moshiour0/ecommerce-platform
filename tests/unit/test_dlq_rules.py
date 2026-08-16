"""
Unit tests for DLQ recovery.

The rule that matters most is negative: a schema-incompatible message must
never be auto-retried, whatever its retry budget says. The rest pin the retry
count, the backoff gate, and the topic handling that stops a message being
republished onto the queue it was just read from.
"""

import pytest

from conftest import dlq_rules

Action = dlq_rules.Action
plan_recovery = dlq_rules.plan_recovery
is_schema_incompatible = dlq_rules.is_schema_incompatible
original_topic = dlq_rules.original_topic
is_valid_topic = dlq_rules.is_valid_topic
message_fingerprint = dlq_rules.message_fingerprint
MAX_RETRIES = dlq_rules.MAX_RETRIES
BACKOFF = dlq_rules.DEFAULT_BACKOFF_SECONDS

TRANSIENT = "ConnectionError: could not connect to inventory-service"


# ---------------------------------------------------------------------------
# schema incompatibility — never retried
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    "SchemaRegistryError: incompatible schema",
    "avro deserialization failed",
    "Unknown magic byte!",
    "SerializationError: bad payload",
    "Incompatible reader schema",
    "AVRO DESERIALIZATION FAILED",  # casing must not matter
])
def test_schema_errors_are_escalated_never_retried(error):
    d = plan_recovery(attempts=0, error_text=error)
    assert d.action is Action.ESCALATE, f"{error!r} was retried"
    assert d.schema_incompatible is True


def test_schema_check_beats_a_full_retry_budget():
    # The budget is irrelevant: replaying meets identical conditions every time
    # because the bytes and the reader schema are both fixed.
    d = plan_recovery(attempts=0, error_text="avro decode error",
                      seconds_since_last_attempt=999_999)
    assert d.action is Action.ESCALATE


def test_transient_errors_are_not_schema_errors():
    for error in (TRANSIENT, "lock_timeout exceeded", "503 from payment-service",
                  "asyncio.TimeoutError"):
        assert not is_schema_incompatible(error), f"{error!r} misread as schema"


def test_a_missing_error_is_not_assumed_to_be_schema():
    # The DLQ router always writes an error header, so its absence is unusual
    # provenance rather than a decode failure. The retry budget handles it.
    for error in (None, ""):
        assert is_schema_incompatible(error) is False


# ---------------------------------------------------------------------------
# retry budget
# ---------------------------------------------------------------------------

def test_a_new_message_is_republished():
    d = plan_recovery(attempts=0, error_text=TRANSIENT)
    assert d.action is Action.REPUBLISH
    assert "1 of 3" in d.reason


def test_the_last_allowed_retry_still_republishes():
    d = plan_recovery(attempts=MAX_RETRIES - 1, error_text=TRANSIENT)
    assert d.action is Action.REPUBLISH


def test_exhausted_retries_escalate():
    d = plan_recovery(attempts=MAX_RETRIES, error_text=TRANSIENT)
    assert d.action is Action.ESCALATE
    assert d.schema_incompatible is False
    assert "exhausted" in d.reason


def test_an_over_counted_message_still_escalates():
    # Defensive: a counter that somehow ran past the max must not wrap around
    # into a fresh budget.
    assert plan_recovery(attempts=99, error_text=TRANSIENT).action is Action.ESCALATE


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------

def test_backoff_defers_a_recent_attempt():
    d = plan_recovery(attempts=1, error_text=TRANSIENT,
                      seconds_since_last_attempt=60)
    assert d.action is Action.WAIT
    assert "remaining" in d.reason


def test_backoff_elapsed_allows_a_retry():
    d = plan_recovery(attempts=1, error_text=TRANSIENT,
                      seconds_since_last_attempt=BACKOFF + 1)
    assert d.action is Action.REPUBLISH


def test_backoff_is_configurable():
    d = plan_recovery(attempts=1, error_text=TRANSIENT,
                      seconds_since_last_attempt=30, backoff_seconds=10)
    assert d.action is Action.REPUBLISH


def test_no_timing_information_does_not_block_a_retry():
    # A message with no recorded attempt time is being seen for the first time.
    assert plan_recovery(attempts=0, error_text=TRANSIENT,
                         seconds_since_last_attempt=None).action is Action.REPUBLISH


def test_exhaustion_is_checked_before_backoff():
    # Otherwise a message that is out of retries would sit in WAIT forever
    # instead of reaching a human.
    d = plan_recovery(attempts=MAX_RETRIES, error_text=TRANSIENT,
                      seconds_since_last_attempt=1)
    assert d.action is Action.ESCALATE


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------

def test_the_original_topic_is_recovered():
    assert original_topic("dlq.Order.events") == "Order.events"
    assert original_topic("dlq.inventory.events") == "inventory.events"


def test_a_non_dlq_topic_yields_none():
    # Guards the loop: republishing a message onto the topic it was read from
    # is not a bounded retry, it is a broker-speed cycle.
    for topic in ("Order.events", "", None, "notdlq.Order.events"):
        assert original_topic(topic) is None


def test_a_bare_dlq_prefix_yields_none():
    assert original_topic("dlq.") is None


def test_topic_names_are_validated_before_producing():
    assert is_valid_topic("Order.events")
    assert is_valid_topic("inventory-events_v2")
    for bad in ("", None, "topic with spaces", "topic/../etc", "a;b"):
        assert not is_valid_topic(bad), f"{bad!r} passed validation"


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def test_identical_payloads_share_a_fingerprint():
    # A republished message reappears at a new offset; counting attempts by
    # offset would hand every retry a fresh budget.
    assert message_fingerprint(b'{"a":1}') == message_fingerprint(b'{"a":1}')


def test_different_payloads_do_not():
    assert message_fingerprint(b'{"a":1}') != message_fingerprint(b'{"a":2}')


def test_an_empty_payload_still_fingerprints():
    assert len(message_fingerprint(b"")) == 64
    assert len(message_fingerprint(None)) == 64
