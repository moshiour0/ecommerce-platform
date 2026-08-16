"""
Audit record integrity, as pure functions.

"Immutable audit records" cannot mean only that this service exposes no PUT or
DELETE. Anyone with a psql prompt can edit a row, and an audit log that cannot
tell you whether that happened is not evidence of anything -- it is just a
table people trust for no stated reason.

So each record carries the hash of the one before it. Changing any field of any
record changes its hash, which breaks every link after it, and the break points
at the earliest tampered record. Deleting a record from the middle, reordering
two, or inserting one after the fact all break the chain in the same detectable
way. Rewriting the whole chain to stay consistent is possible for anyone with
write access -- this is tamper-evidence, not tamper-proofing, and the honest
claim is the smaller one.

Hashing is over a canonical form: sorted keys, no insignificant whitespace,
UTF-8. Without that, two encodings of the same record hash differently and the
chain breaks for reasons that have nothing to do with tampering -- Python does
not promise dict ordering across processes, and JSONB does not preserve key
order at all.

Extracted so the chain can be tested without a database, matching
reservation_rules.py and media_rules.py.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# The hash a chain starts from. Not a real hash of anything -- it is a fixed
# anchor so the first record's link is defined rather than special-cased, and
# so a chain cannot be silently re-rooted by deleting record 1.
GENESIS_HASH = "0" * 64

# Fields that are covered by the hash. Anything outside this tuple is metadata
# that may legitimately differ between replicas (a local row id, say) and must
# not affect the chain. Adding a field here changes every hash, so it is a
# migration, not an edit.
HASHED_FIELDS = ("sequence", "actor", "action", "resource_type", "resource_id",
                 "recorded_at", "payload")


@dataclass(frozen=True)
class ChainBreak:
    """Where a chain stops verifying, and why."""

    index: int
    sequence: Optional[int]
    reason: str


def canonical_bytes(record: Dict[str, Any]) -> bytes:
    """The exact bytes a record hashes over.

    sort_keys makes the encoding independent of dict insertion order, which is
    what lets a record hashed in a web process verify in a different process
    after a round trip through JSONB. separators drops the whitespace json.dumps
    would otherwise vary. default=str keeps datetimes and UUIDs hashable without
    each caller having to remember to stringify them.
    """
    subset = {field: record.get(field) for field in HASHED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def compute_hash(prev_hash: str, record: Dict[str, Any]) -> str:
    """The hash linking this record to its predecessor."""
    digest = hashlib.sha256()
    digest.update((prev_hash or GENESIS_HASH).encode("utf-8"))
    digest.update(canonical_bytes(record))
    return digest.hexdigest()


def link(prev_hash: Optional[str], record: Dict[str, Any]) -> Dict[str, str]:
    """Both hashes a new record needs before it is written."""
    resolved_prev = prev_hash or GENESIS_HASH
    return {
        "prev_hash": resolved_prev,
        "record_hash": compute_hash(resolved_prev, record),
    }


def verify_chain(records: Sequence[Dict[str, Any]]) -> List[ChainBreak]:
    """Check a chain in order. Returns every break found, earliest first.

    Returns all breaks rather than stopping at the first, because a single
    edited record produces one hash mismatch while a re-sequenced range
    produces many, and telling those apart matters when deciding whether a log
    is salvageable.

    An empty log verifies. That is not the same as a log being complete -- this
    function cannot know how many records should exist, and nothing here
    detects truncation of the tail. Sequence gaps are reported; a chain that
    simply stops early looks identical to one that has not grown yet.
    """
    breaks: List[ChainBreak] = []
    expected_prev = GENESIS_HASH
    last_sequence: Optional[int] = None

    for index, record in enumerate(records):
        sequence = record.get("sequence")

        if record.get("prev_hash") != expected_prev:
            breaks.append(ChainBreak(
                index, sequence,
                f"prev_hash does not match the previous record's hash "
                f"(expected {expected_prev[:12]}..., "
                f"found {str(record.get('prev_hash'))[:12]}...)"))

        recomputed = compute_hash(record.get("prev_hash") or GENESIS_HASH, record)
        if record.get("record_hash") != recomputed:
            breaks.append(ChainBreak(
                index, sequence,
                "record_hash does not match the record's contents; "
                "a field was changed after it was written"))

        if last_sequence is not None and sequence is not None:
            if sequence <= last_sequence:
                breaks.append(ChainBreak(
                    index, sequence,
                    f"sequence went backwards or repeated "
                    f"({last_sequence} then {sequence})"))
            elif sequence != last_sequence + 1:
                breaks.append(ChainBreak(
                    index, sequence,
                    f"sequence gap: {last_sequence} then {sequence}"))

        last_sequence = sequence if sequence is not None else last_sequence
        # Follow the chain as recorded, not as recomputed, so blame stays
        # precise. An edited field breaks that record's own hash and nothing
        # else, because the records after it still point at the hash that is
        # actually stored. Substituting the recomputed hash here would make
        # every later record mismatch too, burying one tampered row under a
        # cascade of accusations against rows nobody touched.
        expected_prev = record.get("record_hash")

    return breaks


def chain_is_intact(records: Sequence[Dict[str, Any]]) -> bool:
    return not verify_chain(records)
