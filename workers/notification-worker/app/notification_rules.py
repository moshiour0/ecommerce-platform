"""
Notification delivery decisions, as pure functions.

notification-service owns notification *state*; this worker owns *delivery*
(§3b). The split matters here because delivery is the part that fails in
interesting ways: providers time out, rate limit, and reject addresses, and
each of those wants a different answer.

Two decisions live here, and both are the kind that are wrong in production for
months if nobody tests them.

Retryable is not the same as failed
-----------------------------------
A timeout means try again. An invalid address means never try again -- retrying
it burns quota, and on some providers repeated sends to a dead address damage
sender reputation for every other message. An unsubscribe is worse: retrying is
not just useless, it is the thing the recipient explicitly asked you to stop
doing. So failure classification is explicit, and anything unrecognised is
treated as retryable-but-bounded rather than permanent, because wrongly giving
up on a real notification is the more expensive mistake and max_attempts stops
it from being unbounded.

Backoff carries jitter
----------------------
Rule 5 requires backoff and jitter on all retry logic. Exponential backoff
alone synchronises every failed notification onto the same retry schedule: a
provider that goes down for a minute gets the entire backlog delivered in one
burst the moment it recovers, which is how a recovering provider is knocked
over a second time. Jitter spreads them.

The random source is injected rather than imported, so the schedule is testable
without patching the module -- the same reason checkout_lock takes a clock.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

# Providers are slow and lossy; five attempts over roughly ten minutes is
# enough to ride out a restart without pretending a dead address will revive.
MAX_ATTEMPTS = 5

BASE_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 300

VALID_CHANNELS = frozenset({"email", "sms", "push"})

# Provider failures that will never succeed on retry.
PERMANENT_FAILURES = frozenset({
    "invalid_address",   # malformed or nonexistent recipient
    "unsubscribed",      # the recipient asked us to stop
    "blocked",           # provider refuses this recipient
    "template_missing",  # our fault, and no amount of retrying fixes it
})

# Failures that are expected and temporary.
RETRYABLE_FAILURES = frozenset({
    "timeout", "rate_limited", "provider_error", "connection_error",
})


class Disposition(str, Enum):
    DELIVERED = "delivered"
    RETRY = "retry"        # try again after a delay
    FAILED = "failed"      # terminal: either permanent, or attempts exhausted


@dataclass(frozen=True)
class DeliveryDecision:
    disposition: Disposition
    delay_seconds: Optional[int]  # None unless RETRY
    detail: str

    @property
    def terminal(self) -> bool:
        return self.disposition in (Disposition.DELIVERED, Disposition.FAILED)


def is_valid_channel(channel: str) -> bool:
    return channel in VALID_CHANNELS


def backoff_delay(attempt: int, random_fraction: Callable[[], float],
                  base: int = BASE_DELAY_SECONDS,
                  cap: int = MAX_DELAY_SECONDS) -> int:
    """Seconds to wait before attempt number `attempt + 1`.

    Equal jitter: half the window is fixed and half is random. Full jitter
    (uniform over the whole window) spreads better but can schedule a retry
    almost immediately, which against a rate-limited provider just spends
    another attempt for nothing. Half-fixed guarantees the delay actually grows.

    `random_fraction` returns a float in [0, 1). Injected so a test can pin the
    schedule exactly instead of asserting a range.
    """
    if attempt < 1:
        attempt = 1

    # Cap the exponent before shifting, or a large attempt count produces an
    # enormous intermediate that is then thrown away by min().
    exponent = min(attempt - 1, 16)
    window = min(cap, base * (2 ** exponent))

    half = window / 2
    return int(half + half * random_fraction())


def classify(result_code: str, attempt: int,
             random_fraction: Callable[[], float],
             max_attempts: int = MAX_ATTEMPTS) -> DeliveryDecision:
    """Decide what to do with one provider result.

    `attempt` is the attempt that just completed, counting from 1.
    """
    if result_code == "delivered":
        return DeliveryDecision(Disposition.DELIVERED, None, "delivered")

    if result_code in PERMANENT_FAILURES:
        # Never retried, regardless of how many attempts remain.
        return DeliveryDecision(
            Disposition.FAILED, None,
            f"permanent failure: {result_code}")

    if attempt >= max_attempts:
        return DeliveryDecision(
            Disposition.FAILED, None,
            f"giving up after {attempt} attempts, last failure: {result_code}")

    detail = (f"retrying after {result_code}"
              if result_code in RETRYABLE_FAILURES
              else f"retrying after unrecognised result {result_code!r}")
    return DeliveryDecision(
        Disposition.RETRY,
        backoff_delay(attempt, random_fraction),
        detail)
