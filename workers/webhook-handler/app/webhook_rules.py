"""
PSP webhook admission decisions, as pure functions.

This is layer 1 of the four in §4 of the architecture document, plus the
vocabulary the other three report in. Layers 2 to 4 are I/O -- Redis, Postgres,
the outbox -- and live in main.py; what is here is everything that can be
decided from the request alone, which is exactly the part worth testing
exhaustively.

The four layers, and why the order is not negotiable
---------------------------------------------------
  1. HMAC-SHA256 over the raw body. Nothing else runs first, because an
     unsigned request must not be allowed to consume a Redis key, a database
     row, or a connection from the pool. Rejecting cheaply is the point.
  2. Redis SET NX with a 7-day TTL. The fast path for the common case, which is
     a PSP retrying a webhook it already delivered.
  3. Postgres INSERT ... ON CONFLICT DO NOTHING. The durable path, because
     Redis is memory and a flush would turn every past event into a new one.
  4. Emission to the payment-service outbox, in the same transaction as 3.

Layers 2 and 3 are not redundant. Redis alone loses its memory; Postgres alone
puts every duplicate retry through a write transaction. Together the common
case is cheap and the guarantee is durable.

Signature scheme
----------------
The header is `t=<unix seconds>,v1=<hex hmac>`, and the signed payload is
`<t>.<raw body>` -- the scheme Stripe uses, chosen because it is well
understood rather than because a PSP here requires it. The timestamp is inside
the signed material, which is what makes the freshness check meaningful: an
attacker replaying an old body cannot move it forward without invalidating the
signature.

Comparison is constant time. A byte-by-byte early return leaks how much of a
forged signature was correct, which is enough to construct a valid one given
patience.
"""

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

# How far a webhook's timestamp may be from now, in seconds. Generous enough
# for clock skew and a slow queue at the PSP, short enough that a captured
# request is not replayable tomorrow.
DEFAULT_TOLERANCE_SECONDS = 300

# Layer 2/3 dedup window. Longer than any PSP's retry schedule, so a retry
# always lands inside a window where we still remember the original.
DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60

SIGNATURE_SCHEME = "v1"


class Admission(str, Enum):
    ACCEPT = "accept"                    # signature good, proceed to dedup
    MISSING_SIGNATURE = "missing"        # no header at all
    MALFORMED_SIGNATURE = "malformed"    # header present but unparseable
    BAD_SIGNATURE = "bad_signature"      # did not verify against the secret
    STALE = "stale"                      # outside the replay tolerance


@dataclass(frozen=True)
class SignatureCheck:
    admission: Admission
    detail: str
    timestamp: Optional[int] = None

    @property
    def accepted(self) -> bool:
        return self.admission is Admission.ACCEPT


def parse_signature_header(header: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Pull the timestamp and v1 signature out of `t=...,v1=...`.

    Unknown parts are ignored rather than rejected, so a PSP adding a v2
    alongside v1 does not break this handler. An unparseable timestamp yields
    None, which the caller treats as malformed -- never as zero, which would
    be a timestamp in 1970 and would fail the freshness check for the wrong
    reason.
    """
    if not header:
        return None, None

    timestamp: Optional[int] = None
    signature: Optional[str] = None

    for part in header.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == SIGNATURE_SCHEME:
            signature = value

    return timestamp, signature


def expected_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    """The HMAC a genuine sender would have produced."""
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    return hmac.new(secret.encode("utf-8"), signed_payload,
                    hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, header: Optional[str],
                     now: int,
                     tolerance: int = DEFAULT_TOLERANCE_SECONDS) -> SignatureCheck:
    """Layer 1: decide whether this request is genuinely from the PSP.

    `raw_body` must be the bytes as received. Re-serializing parsed JSON before
    hashing changes key order and whitespace, and the signature then fails for
    every honest request -- a mistake that presents as "the PSP is sending bad
    signatures".
    """
    if not header:
        return SignatureCheck(Admission.MISSING_SIGNATURE,
                              "no signature header")

    timestamp, signature = parse_signature_header(header)

    if timestamp is None or not signature:
        return SignatureCheck(Admission.MALFORMED_SIGNATURE,
                              "signature header is missing t or v1")

    # Freshness before comparison: a replayed request with a genuine old
    # signature verifies perfectly, so the signature check alone cannot catch
    # it. Both directions are bounded -- a timestamp far in the future is
    # either a broken clock or an attempt to mint something replayable later.
    age = now - timestamp
    if age > tolerance:
        return SignatureCheck(Admission.STALE,
                              f"timestamp is {age}s old, tolerance is {tolerance}s",
                              timestamp)
    if age < -tolerance:
        return SignatureCheck(Admission.STALE,
                              f"timestamp is {-age}s in the future, "
                              f"tolerance is {tolerance}s",
                              timestamp)

    expected = expected_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, signature):
        return SignatureCheck(Admission.BAD_SIGNATURE,
                              "signature does not match", timestamp)

    return SignatureCheck(Admission.ACCEPT, "signature verified", timestamp)


def dedup_key(event_id: str) -> str:
    """Redis key for layer 2. Namespaced so it cannot collide with a cart."""
    return f"webhook:seen:{event_id}"
