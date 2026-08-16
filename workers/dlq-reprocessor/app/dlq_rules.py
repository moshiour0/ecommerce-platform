"""
DLQ recovery decisions, as pure functions.

Implements the policy in §5: a poison message may be re-submitted to its
original topic after a backoff, up to three retries; messages that exhaust
those retries are escalated and written to the immutable audit log; and
schema-incompatible messages are never auto-retried, because no amount of
redelivery fixes a payload the consumer cannot decode.

Why schema failures are special
-------------------------------
Every other DLQ reason is a bet that the world has changed: the database was
down, a downstream service was deploying, a lock timed out. Replaying is
reasonable because the second attempt meets different conditions. A schema
incompatibility meets identical conditions every time -- the bytes and the
reader schema are both fixed -- so retrying is a loop that burns three
attempts, three backoff windows and an escalation to arrive exactly where it
started. Worse, it hides the real problem behind a delay: the schema needs
resolving by a person, and every automatic retry postpones that discovery.

Detection is by pattern over the error text recorded by the producer's DLQ
routing, because that string is all the DLQ carries. It is deliberately broad.
A false positive costs one message escalated to a human who replays it by hand;
a false negative costs an infinite-in-practice retry loop against a message
that cannot succeed. Those are not the same size of mistake.

Message identity
----------------
Attempts cannot be tracked in Kafka headers: the DLQ router in
python_common.kafka_client writes a fresh `error` header and preserves nothing
else, so a counter attached to a republished message is gone the moment it
fails again. Identity is therefore a content fingerprint -- the same bytes are
the same message, whatever offset they arrive at -- and the count lives beside
it in Redis.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# §5: "up to 3 retries" and "a configurable backoff (default: 1 hour)".
MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 3600

# How long an attempt counter outlives its last update. Comfortably longer than
# MAX_RETRIES backoff windows, so a message cannot come back after the count
# has quietly expired and get a fresh three attempts.
ATTEMPT_TTL_SECONDS = 7 * 24 * 60 * 60

# Substrings that mean "the consumer could not decode this", in any casing.
# Drawn from the deserializer and Schema Registry error strings this platform
# can actually produce.
SCHEMA_ERROR_PATTERNS = (
    "schema", "avro", "deserial", "incompatible", "magic byte",
    "unknown magic", "serializationerror", "registry",
)


class Action(str, Enum):
    REPUBLISH = "republish"  # send back to the original topic
    ESCALATE = "escalate"    # a person must look at this
    WAIT = "wait"            # backoff has not elapsed yet


@dataclass(frozen=True)
class Recovery:
    action: Action
    reason: str
    # Present on ESCALATE so the audit record says why without re-deriving it.
    schema_incompatible: bool = False


def message_fingerprint(payload: bytes) -> str:
    """Stable identity for a message across republishes.

    Content-addressed rather than offset-addressed: a republished message
    reappears at a new offset in a new partition, and counting attempts by
    offset would give every retry a fresh budget.
    """
    return hashlib.sha256(payload or b"").hexdigest()


def attempts_key(fingerprint: str) -> str:
    return f"dlq:attempts:{fingerprint}"


def is_schema_incompatible(error_text: Optional[str]) -> bool:
    """Whether a DLQ error describes a decoding failure rather than a fault.

    An empty or missing error is NOT treated as schema-incompatible: the DLQ
    router always writes one, so its absence means something unusual about the
    message's provenance, not that it failed to deserialize. Escalating those
    is handled by the retry budget instead.
    """
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in SCHEMA_ERROR_PATTERNS)


def plan_recovery(attempts: int, error_text: Optional[str],
                  seconds_since_last_attempt: Optional[float] = None,
                  max_retries: int = MAX_RETRIES,
                  backoff_seconds: int = DEFAULT_BACKOFF_SECONDS) -> Recovery:
    """Decide what to do with one message sitting in a DLQ.

    `attempts` counts republishes already made for this message, so a message
    never seen before arrives with 0.
    """
    if is_schema_incompatible(error_text):
        # Checked first and unconditionally: this must not depend on whether a
        # retry budget happens to remain.
        return Recovery(
            Action.ESCALATE,
            "schema-incompatible; requires manual schema resolution",
            schema_incompatible=True)

    if attempts >= max_retries:
        return Recovery(
            Action.ESCALATE,
            f"exhausted {attempts} of {max_retries} automatic retries")

    if (seconds_since_last_attempt is not None
            and seconds_since_last_attempt < backoff_seconds):
        remaining = int(backoff_seconds - seconds_since_last_attempt)
        return Recovery(Action.WAIT, f"backoff has {remaining}s remaining")

    return Recovery(
        Action.REPUBLISH,
        f"retry {attempts + 1} of {max_retries}")


def original_topic(dlq_topic: str) -> Optional[str]:
    """`dlq.Order.events` -> `Order.events`.

    Returns None for a topic that is not a DLQ, so the reprocessor can never
    republish a message onto the topic it just read it from -- which would be
    an unbounded loop at full speed rather than a bounded retry.
    """
    if not dlq_topic or not dlq_topic.startswith("dlq."):
        return None
    remainder = dlq_topic[len("dlq."):]
    return remainder or None


_SAFE_TOPIC = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_topic(topic: Optional[str]) -> bool:
    """Kafka's own character rules, checked before producing.

    A DLQ topic name arrives from a broker rather than from configuration, so
    it is treated as input.
    """
    return bool(topic) and bool(_SAFE_TOPIC.match(topic))
