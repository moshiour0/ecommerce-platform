"""
Media lifecycle decisions, as pure functions.

This service owns metadata and state, not bytes: its database is media_meta_db,
and the platform has no object store. An asset here is a record of something
uploaded elsewhere, plus the question this module answers -- may it be served?

Quarantine is the default, not a state something is moved into
------------------------------------------------------------
Newly registered media is QUARANTINED before anything has looked at it, and
only a completed clean scan makes it servable. The inverse design -- servable
until a scanner objects -- means every asset is public during the window
between upload and scan, which is exactly when an unscanned upload is most
interesting to whoever uploaded it. So `is_servable` is a whitelist of one
state, and every unknown or unexpected status answers False.

INFECTED is terminal. A scanner that says clean after saying infected is more
likely to be wrong, or to have been made to say so, than the first result was;
promoting on the second answer would let a caller retry a scan until it gets
the verdict it wants. Re-scanning a CLEAN asset is allowed and revokes
servability for the duration, because a re-scan implies doubt.

Extracted so the transition table can be tested without a database, matching
reservation_rules.py and charge_rules.py.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Set


class MediaStatus(str, Enum):
    QUARANTINED = "quarantined"  # registered, never yet cleared -- not servable
    SCANNING = "scanning"        # a scan is in flight -- not servable
    CLEAN = "clean"              # scanned and cleared -- the only servable state
    INFECTED = "infected"        # scanner objected -- terminal, never servable
    DELETED = "deleted"          # withdrawn -- terminal


TERMINAL: Set[MediaStatus] = {MediaStatus.INFECTED, MediaStatus.DELETED}

# Outbox event types. Consumers (audit-service above all) rely on these names.
EVENT_REGISTERED = "MediaRegistered"
EVENT_SCAN_STARTED = "MediaScanStarted"
EVENT_PUBLISHED = "MediaPublished"
EVENT_QUARANTINED = "MediaQuarantined"
EVENT_DELETED = "MediaDeleted"

# Default-deny on type as well as on state. An allowlist means a new format is
# a deliberate decision rather than something that arrives by being uploaded.
ALLOWED_CONTENT_TYPES: Set[str] = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4",
}

MAX_BYTES = 25 * 1024 * 1024  # 25 MiB


class Outcome(str, Enum):
    OK = "ok"
    INVALID = "invalid"            # the request itself is not acceptable
    NOT_ALLOWED = "not_allowed"    # a real asset, but not from this state


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    new_status: Optional[MediaStatus]
    event: Optional[str]
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


def is_servable(status) -> bool:
    """Whether an asset may be handed to a caller.

    A whitelist of exactly one status. Anything unrecognised -- a status written
    by an older version, a typo, a row hand-edited in psql -- is not servable,
    because the cost of wrongly serving unscanned media is not symmetric with
    the cost of wrongly withholding it.
    """
    return status == MediaStatus.CLEAN or status == MediaStatus.CLEAN.value


def plan_registration(filename: str, content_type: str, size_bytes: int) -> Decision:
    """Validate an upload's metadata and place it in quarantine."""
    if not filename or not filename.strip():
        return Decision(Outcome.INVALID, None, None, "filename is required")

    if content_type not in ALLOWED_CONTENT_TYPES:
        return Decision(
            Outcome.INVALID, None, None,
            f"content type {content_type!r} is not allowed")

    if size_bytes <= 0:
        return Decision(
            Outcome.INVALID, None, None,
            f"size must be positive, got {size_bytes}")

    if size_bytes > MAX_BYTES:
        return Decision(
            Outcome.INVALID, None, None,
            f"size {size_bytes} exceeds the {MAX_BYTES} byte limit")

    return Decision(Outcome.OK, MediaStatus.QUARANTINED, EVENT_REGISTERED,
                    "registered and quarantined pending scan")


def plan_scan_start(current: MediaStatus) -> Decision:
    """Begin a scan. Allowed from quarantine, and from clean as a re-scan."""
    if current in TERMINAL:
        return Decision(Outcome.NOT_ALLOWED, None, None,
                        f"{current.value} is terminal")

    if current is MediaStatus.SCANNING:
        # Not an error: the scanner asking twice is ordinary, and the asset is
        # already in the state the caller wants. Returning a conflict here would
        # make an honest retry look like a fault.
        return Decision(Outcome.OK, MediaStatus.SCANNING, None,
                        "scan already in progress")

    return Decision(Outcome.OK, MediaStatus.SCANNING, EVENT_SCAN_STARTED,
                    "scan started; asset is not servable while it runs")


def plan_scan_result(current: MediaStatus, infected: bool) -> Decision:
    """Apply a scanner verdict.

    Only meaningful while a scan is in flight. A verdict arriving for an asset
    nobody asked to scan is refused rather than applied: it is either a
    duplicate of a result already handled, or a caller trying to set the status
    directly through the scanner endpoint.
    """
    if current in TERMINAL:
        # An infected verdict repeating itself is a duplicate delivery, not a
        # problem. Anything else aimed at a terminal asset is refused.
        if current is MediaStatus.INFECTED and infected:
            return Decision(Outcome.OK, MediaStatus.INFECTED, None,
                            "already quarantined as infected")
        return Decision(Outcome.NOT_ALLOWED, None, None,
                        f"{current.value} is terminal")

    if current is not MediaStatus.SCANNING:
        return Decision(Outcome.NOT_ALLOWED, None, None,
                        f"no scan in progress (status is {current.value})")

    if infected:
        return Decision(Outcome.OK, MediaStatus.INFECTED, EVENT_QUARANTINED,
                        "scanner reported infected; quarantined permanently")

    return Decision(Outcome.OK, MediaStatus.CLEAN, EVENT_PUBLISHED,
                    "scanned clean; asset is now servable")


def plan_delete(current: MediaStatus) -> Decision:
    """Withdraw an asset.

    Deleting an infected asset is allowed and is the ordinary way to clear one
    out; deleting an already-deleted asset succeeds as a no-op so a retried
    DELETE does not surface as an error.
    """
    if current is MediaStatus.DELETED:
        return Decision(Outcome.OK, MediaStatus.DELETED, None, "already deleted")

    return Decision(Outcome.OK, MediaStatus.DELETED, EVENT_DELETED, "deleted")
