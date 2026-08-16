"""
Provider adapters for notification delivery.

There is no email, SMS or push provider in this platform, and inventing
credentials for one would produce a component that cannot run anywhere. So the
boundary is real and the implementation behind it is honest about being a stub:
it records what would have been sent and reports a result.

The stub reads an optional `simulate` field from the notification payload, so
every branch of notification_rules -- delivered, timeout, invalid_address,
unsubscribed -- can be exercised against the running worker rather than only in
unit tests. A real adapter added later implements the same one-method contract
and the retry logic above it does not change.
"""

import logging
from typing import Dict, Protocol

logger = logging.getLogger(__name__)


class Provider(Protocol):
    """One send attempt.

    Returns a result code that notification_rules.classify understands:
    "delivered", one of PERMANENT_FAILURES, one of RETRYABLE_FAILURES, or
    anything else -- which is treated as retryable-but-bounded.

    Must not raise for an ordinary delivery failure. A raised exception is a
    bug in the adapter, not a provider outcome, and the worker treats it
    differently.
    """

    def send(self, channel: str, recipient: str, payload: Dict) -> str: ...


class RecordingStubProvider:
    """Logs the message and returns the simulated outcome.

    Deliberately not named "MockProvider": this is what runs in every
    environment right now, and a name that sounds like test scaffolding invites
    someone to assume a real one is wired up somewhere.
    """

    name = "recording-stub"

    def send(self, channel: str, recipient: str, payload: Dict) -> str:
        outcome = payload.get("simulate", "delivered")
        logger.info(
            "stub provider: would send via %s to %s (template=%s) -> %s",
            channel, recipient, payload.get("template_name"), outcome,
            extra={"channel": channel, "outcome": outcome})
        return outcome


def get_provider(channel: str) -> Provider:
    """The adapter for a channel.

    One stub for all three today. The lookup exists so that wiring a real email
    provider does not require touching the worker loop.
    """
    return RecordingStubProvider()
