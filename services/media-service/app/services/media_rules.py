"""
Media lifecycle decisions, as pure functions.

This service owns metadata and state, not bytes: its database is media_meta_db
and the bytes live in an object store, reached by pre-signed URLs so they
never pass through any service. An asset here is that record, plus the two
questions this module answers -- may it be served, and to whom?

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


def plan_registration(filename: str, content_type: str, size_bytes: int,
                      allowed: Optional[Set[str]] = None) -> Decision:
    """Validate an upload's metadata and place it in quarantine.

    `allowed` overrides the default content-type allowlist, because what is
    acceptable depends on what the asset is for: a KYC document may be a
    PDF and a listing photo may not.
    """
    permitted = ALLOWED_CONTENT_TYPES if allowed is None else allowed
    if not filename or not filename.strip():
        return Decision(Outcome.INVALID, None, None, "filename is required")

    if content_type not in permitted:
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


# ---------------------------------------------------------------------------
# what an asset is for
# ---------------------------------------------------------------------------
#
# The Media Center seam. Media Center itself is not built -- see
# MARKETPLACE_ROADMAP.md §3.6 -- but the purpose taxonomy is here now because
# it decides who may read an asset, and retrofitting that onto a store full of
# untyped files means auditing every one of them.


class AssetPurpose(str, Enum):
    PRODUCT_IMAGE = "product_image"      # a listing photo: public once clean
    SELLER_DOCUMENT = "seller_document"  # KYC, trade licence, bank details
    TRY_ON_SOURCE = "try_on_source"      # a photo of a person's body
    POST_MEDIA = "post_media"            # shared to the social feed on purpose


# Purposes whose contents must never be handed out by a public endpoint, even
# once scanned clean.
#
# seller_document is obvious: it is identity papers and bank details.
#
# try_on_source is the one worth stating explicitly, because it does not look
# like a secret in a database and it is the most sensitive thing this platform
# will ever hold. It is a photograph of a person's body, uploaded so software
# can put clothes on it. Treating it like a product image because both are
# JPEGs is exactly the mistake that ends a company. Only the uploader and the
# try-on pipeline may read one, and it never becomes servable by being scanned.
CONFIDENTIAL_PURPOSES: Set[AssetPurpose] = {
    AssetPurpose.SELLER_DOCUMENT,
    AssetPurpose.TRY_ON_SOURCE,
}

# Documents are the only purpose that may be a PDF; a listing photo that is a
# PDF is a mistake, and a body photo that is one is something stranger.
DOCUMENT_CONTENT_TYPES: Set[str] = {"application/pdf"}


def is_confidential(purpose) -> bool:
    """Whether an asset's contents are private to its owner.

    Unknown purposes are confidential. A purpose this code does not recognise
    was added by someone who did not update this function, and the failure that
    matters is the one where a new kind of upload is public by default.
    """
    try:
        purpose = AssetPurpose(purpose)
    except ValueError:
        return True
    return purpose in CONFIDENTIAL_PURPOSES


def is_publicly_servable(status, purpose) -> bool:
    """Whether an asset may be handed to any caller.

    Both conditions, and neither is sufficient: scanned clean AND not
    confidential. A KYC document that passes a virus scan is still a KYC
    document.
    """
    return is_servable(status) and not is_confidential(purpose)


def allowed_content_types(purpose) -> Set[str]:
    """The content types acceptable for one purpose."""
    try:
        purpose = AssetPurpose(purpose)
    except ValueError:
        return set()
    if purpose is AssetPurpose.SELLER_DOCUMENT:
        return ALLOWED_CONTENT_TYPES | DOCUMENT_CONTENT_TYPES
    return set(ALLOWED_CONTENT_TYPES)


def storage_key(purpose, owner_id: str, asset_id: str) -> str:
    """Where an asset's bytes live in the object store.

    Partitioned by purpose first so the bucket can carry a different policy per
    prefix -- public read on products/, deny-all on documents/ and try-on/ --
    without needing to know anything about individual objects. A flat namespace
    would make that policy per-object, which is unmanageable at any scale.
    """
    try:
        purpose = AssetPurpose(purpose)
    except ValueError:
        raise InvalidPurpose(f"unknown purpose {purpose!r}")

    prefix = {
        AssetPurpose.PRODUCT_IMAGE: "products",
        AssetPurpose.SELLER_DOCUMENT: "documents",
        AssetPurpose.TRY_ON_SOURCE: "try-on",
        AssetPurpose.POST_MEDIA: "posts",
    }[purpose]
    return f"{prefix}/{owner_id}/{asset_id}"


class InvalidPurpose(ValueError):
    pass


def plan_registration_for(purpose, filename: str, content_type: str,
                          size_bytes: int) -> Decision:
    """plan_registration, restricted to what this purpose accepts."""
    try:
        AssetPurpose(purpose)
    except ValueError:
        return Decision(Outcome.INVALID, None, None,
                        f"unknown purpose {purpose!r}")

    return plan_registration(filename, content_type, size_bytes,
                             allowed=allowed_content_types(purpose))
