"""
Unit tests for reindex decisions.

Two things are being defended. First, that a backfill cannot erase fields it
does not own -- price and stock live in the same document and are written by a
different path. Second, that a backfill running alongside live CDC does not
overwrite something newer than the snapshot it is reading.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import reindex_rules

build_document = reindex_rules.build_document
should_index = reindex_rules.should_index
parse_timestamp = reindex_rules.parse_timestamp
next_cursor = reindex_rules.next_cursor
is_complete = reindex_rules.is_complete
Cursor = reindex_rules.Cursor

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=1)
LATER = NOW + timedelta(hours=1)


def catalog_row(**overrides):
    row = {
        "product_id": "p-1",
        "sku": "SKU-1",
        "name": "Keyboard",
        "description": "A keyboard",
        "is_active": True,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# field ownership — the expensive mistake
# ---------------------------------------------------------------------------

def test_a_reindex_never_writes_price_or_stock():
    # The document is assembled from three services. Writing a whole document
    # from catalog rows erases every price and stock level in the index, and
    # does it quietly: the documents still exist and still look plausible.
    row = catalog_row(price_cents=9999, quantity_available=42)
    doc = build_document(row)
    for field in reindex_rules.FOREIGN_FIELDS:
        assert field not in doc, f"a reindex would have overwritten {field}"


def test_the_document_contains_the_catalog_fields():
    doc = build_document(catalog_row())
    for field in ("product_id", "sku", "name", "description", "is_active"):
        assert field in doc


def test_no_catalog_field_is_secretly_foreign():
    # Guards the two constants against drifting into each other.
    assert not set(reindex_rules.CATALOG_FIELDS) & set(reindex_rules.FOREIGN_FIELDS)


def test_null_columns_are_omitted_rather_than_written_as_none():
    # Writing None would overwrite an indexed value with nothing, which is the
    # same erasure in a smaller form.
    doc = build_document(catalog_row(description=None))
    assert "description" not in doc


def test_unknown_columns_are_ignored():
    doc = build_document(catalog_row(internal_note="do not index"))
    assert "internal_note" not in doc


def test_timestamps_are_serialised():
    doc = build_document(catalog_row())
    assert isinstance(doc["updated_at"], str)


def test_the_document_carries_a_catalog_owned_timestamp():
    # The guard reads this rather than updated_at, which pricing and inventory
    # also write. Without it, a product whose price changed after creation has
    # an indexed updated_at newer than its catalog row forever, and the backfill
    # skips it every pass -- two documents were permanently unrepairable that
    # way before this field existed.
    doc = build_document(catalog_row())
    assert doc[reindex_rules.CATALOG_TIMESTAMP_FIELD] == doc["updated_at"]


def test_the_catalog_timestamp_is_not_a_foreign_field():
    assert reindex_rules.CATALOG_TIMESTAMP_FIELD not in reindex_rules.FOREIGN_FIELDS


def test_sku_and_is_active_are_carried():
    # They are real columns as of migration 009; before it they were referenced
    # by the mapping and the event but stored nowhere.
    doc = build_document(catalog_row(sku="SKU-1", is_active=False))
    assert doc["sku"] == "SKU-1"
    assert doc["is_active"] is False


# ---------------------------------------------------------------------------
# freshness guard
# ---------------------------------------------------------------------------

def test_a_newer_source_row_is_indexed():
    assert should_index(NOW, EARLIER) is True


def test_a_newer_indexed_document_is_left_alone():
    # CDC has already delivered something more recent than this snapshot. The
    # backfill would be a regression lasting until the product next changes.
    assert should_index(EARLIER, NOW) is False


def test_equal_timestamps_are_rewritten():
    # Harmless, and skipping on equality would permanently strand a document
    # that was half-written at that instant.
    assert should_index(NOW, NOW) is True


def test_a_missing_indexed_document_is_indexed():
    assert should_index(NOW, None) is True
    assert should_index(NOW, "") is True


def test_an_unparseable_indexed_timestamp_is_indexed():
    # A damaged document is exactly what a reindex is for. Refusing to write it
    # would make the backfill silently incomplete.
    for garbage in ("not-a-date", "2026-13-45", 12345, {}):
        assert should_index(NOW, garbage) is True, f"{garbage!r} blocked a repair"


def test_a_source_row_without_a_timestamp_is_still_indexed():
    assert should_index(None, NOW) is True


def test_iso_strings_compare_the_same_as_datetimes():
    assert should_index("2026-08-16T12:00:00+00:00", "2026-08-16T11:00:00+00:00") is True
    assert should_index("2026-08-16T11:00:00+00:00", "2026-08-16T12:00:00+00:00") is False


def test_the_z_suffix_is_understood():
    # Elasticsearch returns dates with Z; Postgres returns +00:00.
    assert parse_timestamp("2026-08-16T12:00:00Z") == NOW


def test_a_naive_timestamp_is_treated_as_utc():
    # Comparing a naive and an aware datetime raises, so this must not leak.
    assert parse_timestamp("2026-08-16T12:00:00") == NOW
    assert should_index("2026-08-16T12:00:00", "2026-08-16T11:00:00Z") is True


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

def test_the_cursor_advances_to_the_last_row():
    rows = [catalog_row(product_id="p-1"),
            catalog_row(product_id="p-2", updated_at=LATER)]
    cursor = next_cursor(rows, Cursor())
    assert cursor.product_id == "p-2"
    assert cursor.updated_at == LATER


def test_an_empty_batch_leaves_the_cursor_alone():
    # Otherwise an exhausted scan resets and re-reads the table forever.
    current = Cursor(updated_at=NOW, product_id="p-9")
    assert next_cursor([], current) is current


def test_a_fresh_cursor_starts_at_the_beginning():
    assert Cursor().at_start is True
    assert Cursor(updated_at=NOW, product_id="p-1").at_start is False


def test_a_short_batch_means_the_scan_is_done():
    assert is_complete([catalog_row()] * 3, batch_size=500) is True
    assert is_complete([], batch_size=500) is True


def test_a_full_batch_means_there_is_more():
    assert is_complete([catalog_row()] * 500, batch_size=500) is False
