"""
Unit tests for audit chain integrity.

These are mostly attempts to tamper with a log and get away with it: edit a
field, delete a record, reorder two, insert one after the fact, rewrite a hash
to match. Each should be caught, and the break should point at the earliest
damaged record rather than somewhere downstream of it.
"""

import copy

from conftest import audit_rules

GENESIS_HASH = audit_rules.GENESIS_HASH
canonical_bytes = audit_rules.canonical_bytes
compute_hash = audit_rules.compute_hash
link = audit_rules.link
verify_chain = audit_rules.verify_chain
chain_is_intact = audit_rules.chain_is_intact


def make_record(sequence, action="order.created", payload=None):
    return {
        "sequence": sequence,
        "actor": "saga-dispatcher",
        "action": action,
        "resource_type": "Order",
        "resource_id": f"order-{sequence}",
        "recorded_at": f"2026-08-16T12:00:{sequence:02d}+00:00",
        "payload": payload if payload is not None else {"amount_cents": 1500},
    }


def build_chain(count=4):
    """A well-formed chain of `count` records."""
    records = []
    prev = GENESIS_HASH
    for i in range(1, count + 1):
        record = make_record(i)
        record.update(link(prev, record))
        prev = record["record_hash"]
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# canonical form
# ---------------------------------------------------------------------------

def test_key_order_does_not_change_the_hash():
    # The record round trips through JSONB, which does not preserve key order.
    # If ordering mattered, every record would fail to verify after a restart.
    a = make_record(1)
    b = {k: a[k] for k in reversed(list(a.keys()))}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert compute_hash(GENESIS_HASH, a) == compute_hash(GENESIS_HASH, b)


def test_nested_payload_key_order_does_not_matter():
    a = make_record(1, payload={"amount_cents": 10, "currency": "USD"})
    b = make_record(1, payload={"currency": "USD", "amount_cents": 10})
    assert compute_hash(GENESIS_HASH, a) == compute_hash(GENESIS_HASH, b)


def test_unhashed_fields_are_ignored():
    # A local row id differs between replicas and must not affect the chain.
    a = make_record(1)
    b = dict(a, id="a-local-uuid", some_future_column=123)
    assert compute_hash(GENESIS_HASH, a) == compute_hash(GENESIS_HASH, b)


def test_every_hashed_field_actually_changes_the_hash():
    # Guards against a field being listed in HASHED_FIELDS but silently dropped
    # from the canonical form -- which would leave it editable without trace.
    base = make_record(1)
    baseline = compute_hash(GENESIS_HASH, base)
    for field in audit_rules.HASHED_FIELDS:
        tampered = dict(base)
        tampered[field] = "tampered" if field != "sequence" else 999
        assert compute_hash(GENESIS_HASH, tampered) != baseline, (
            f"changing {field} did not change the hash")


def test_the_previous_hash_is_part_of_the_hash():
    record = make_record(1)
    assert compute_hash(GENESIS_HASH, record) != compute_hash("a" * 64, record)


# ---------------------------------------------------------------------------
# a healthy chain
# ---------------------------------------------------------------------------

def test_a_well_formed_chain_verifies():
    assert verify_chain(build_chain()) == []
    assert chain_is_intact(build_chain()) is True


def test_an_empty_log_verifies():
    assert verify_chain([]) == []


def test_the_first_record_links_to_genesis():
    chain = build_chain(1)
    assert chain[0]["prev_hash"] == GENESIS_HASH


def test_link_accepts_none_as_the_start_of_a_chain():
    assert link(None, make_record(1))["prev_hash"] == GENESIS_HASH


# ---------------------------------------------------------------------------
# tampering
# ---------------------------------------------------------------------------

def test_editing_a_field_is_detected():
    chain = build_chain()
    chain[1]["payload"] = {"amount_cents": 999999}
    breaks = verify_chain(chain)
    assert breaks, "an edited payload verified"
    assert breaks[0].index == 1, "the break should point at the edited record"


def test_editing_and_recomputing_that_records_hash_is_still_detected():
    # The obvious next move after being caught: fix up the hash of the record
    # you edited. That repairs its own link and breaks the following one.
    chain = build_chain()
    chain[1]["payload"] = {"amount_cents": 999999}
    chain[1]["record_hash"] = compute_hash(chain[1]["prev_hash"], chain[1])
    breaks = verify_chain(chain)
    assert breaks, "an edited record with a recomputed hash verified"
    assert breaks[0].index == 2, "the break should surface at the next record"


def test_deleting_a_record_from_the_middle_is_detected():
    chain = build_chain()
    del chain[1]
    assert verify_chain(chain), "a deleted record went unnoticed"


def test_reordering_two_records_is_detected():
    chain = build_chain()
    chain[1], chain[2] = chain[2], chain[1]
    assert verify_chain(chain), "reordered records verified"


def test_inserting_a_record_after_the_fact_is_detected():
    chain = build_chain()
    forged = make_record(99, action="order.refunded")
    forged.update(link(chain[1]["record_hash"], forged))
    chain.insert(2, forged)
    assert verify_chain(chain), "an inserted record verified"


def test_truncating_the_head_is_detected():
    # Dropping the first record leaves the new first record pointing at a hash
    # that is not genesis.
    chain = build_chain()
    breaks = verify_chain(chain[1:])
    assert breaks and breaks[0].index == 0


def test_an_edit_blames_exactly_the_edited_record():
    # Following the chain as recorded keeps blame precise: the edited record
    # fails its own hash, and the untouched records after it -- which still
    # point at the hash actually stored -- are not accused. Substituting the
    # recomputed hash would bury one tampered row under a cascade of complaints
    # about rows nobody edited.
    chain = build_chain(5)
    chain[1]["actor"] = "someone-else"
    breaks = verify_chain(chain)
    assert [b.index for b in breaks] == [1], (
        f"expected exactly one break at the edited record, got "
        f"{[(b.index, b.reason) for b in breaks]}")


def test_sequence_going_backwards_is_reported():
    chain = build_chain(3)
    chain[2]["sequence"] = 1
    chain[2]["record_hash"] = compute_hash(chain[2]["prev_hash"], chain[2])
    reasons = " ".join(b.reason for b in verify_chain(chain))
    assert "backwards" in reasons or "repeated" in reasons


def test_a_sequence_gap_is_reported():
    records = []
    prev = GENESIS_HASH
    for i in (1, 2, 7):
        record = make_record(i)
        record.update(link(prev, record))
        prev = record["record_hash"]
        records.append(record)
    reasons = " ".join(b.reason for b in verify_chain(records))
    assert "gap" in reasons


def test_verification_does_not_mutate_the_records():
    chain = build_chain()
    before = copy.deepcopy(chain)
    verify_chain(chain)
    assert chain == before
