"""
Unit tests for reindex decisions.

Two things are being defended. First, that a backfill cannot erase fields it
does not own -- price and stock live in the same document and are written by a
different path. Second, that a backfill running alongside live CDC does not
overwrite something newer than the snapshot it is reading.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import read_model, reindex_rules

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

def test_the_catalog_list_price_is_carried_under_its_own_name():
    # One writer per field. catalog's price is base_price_cents; pricing's is
    # price_cents. Writing catalog's number into price_cents is what made the
    # field ambiguous in the first place.
    doc = build_document(catalog_row(base_price_cents=1500))
    assert doc["base_price_cents"] == 1500
    assert "price_cents" not in doc


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


# ---------------------------------------------------------------------------
# agreement with the shared ownership table
# ---------------------------------------------------------------------------

def test_the_catalog_field_list_matches_the_shared_table():
    # This module keeps its own constant so it stays dependency-free, which
    # makes drift possible. This is the test that makes drift loud: adding a
    # field here that catalog does not own would let a backfill erase it.
    assert set(reindex_rules.CATALOG_FIELDS) <= read_model.fields_owned_by(
        read_model.CATALOG)


def test_the_foreign_field_list_matches_the_shared_table():
    assert set(reindex_rules.FOREIGN_FIELDS) == read_model.foreign_fields(
        read_model.CATALOG)


def test_the_catalog_timestamp_is_catalog_owned_in_the_shared_table():
    assert read_model.PRODUCT_FIELD_OWNERS[
        reindex_rules.CATALOG_TIMESTAMP_FIELD] == read_model.CATALOG


# ---------------------------------------------------------------------------
# reaping parentless documents
# ---------------------------------------------------------------------------

is_reapable = reindex_rules.is_reapable
GRACE = reindex_rules.DEFAULT_REAP_GRACE_SECONDS
OLD = NOW - timedelta(seconds=GRACE + 60)


def orphan_doc(**overrides):
    """A document conjured by a price or inventory upsert: no catalog data."""
    doc = {"quantity_available": 3, "updated_at": OLD.isoformat()}
    doc.update(overrides)
    return doc


def test_an_old_parentless_document_is_reapable():
    assert is_reapable(orphan_doc(), NOW).reap is True


def test_a_document_with_a_sku_is_never_reaped():
    # sku is the catalog marker; its presence means this is a real product.
    d = is_reapable(orphan_doc(sku="SKU-1"), NOW)
    assert d.reap is False
    assert "catalog" in d.reason


def test_a_document_the_backfill_has_touched_is_never_reaped():
    d = is_reapable(orphan_doc(catalog_updated_at=OLD.isoformat()), NOW)
    assert d.reap is False


def test_a_young_parentless_document_is_kept():
    # The out-of-order case: a PriceUpdated whose ProductCreated is seconds
    # behind. Deleting this destroys a price that arrived correctly.
    recent = (NOW - timedelta(seconds=30)).isoformat()
    d = is_reapable(orphan_doc(updated_at=recent), NOW)
    assert d.reap is False
    assert "grace" in d.reason


def test_the_grace_boundary_keeps_rather_than_reaps():
    at_edge = (NOW - timedelta(seconds=GRACE - 1)).isoformat()
    assert is_reapable(orphan_doc(updated_at=at_edge), NOW).reap is False
    past_edge = (NOW - timedelta(seconds=GRACE + 1)).isoformat()
    assert is_reapable(orphan_doc(updated_at=past_edge), NOW).reap is True


def test_the_grace_period_is_configurable():
    recent = (NOW - timedelta(seconds=30)).isoformat()
    assert is_reapable(orphan_doc(updated_at=recent), NOW, grace_seconds=10).reap


def test_a_document_with_no_timestamp_is_kept_by_default():
    # An unknown age is not an old age. Without a timestamp there is no way to
    # tell a permanent leftover from one that arrived a moment ago.
    d = is_reapable({"quantity_available": 3}, NOW)
    assert d.reap is False
    assert "age is unknown" in d.reason


def test_an_undated_document_can_be_reaped_on_request():
    # These exist: a ProductCreated carrying nothing but an id indexes as a
    # product_id, a default is_active and nulls everywhere else. They can never
    # age out, so without an opt-in they are immortal.
    d = is_reapable({"product_id": "p-1", "is_active": True, "updated_at": None},
                    NOW, allow_undated=True)
    assert d.reap is True


def test_the_undated_opt_in_still_respects_catalog_markers():
    # The flag relaxes the age check, not the ownership one. A real product
    # must survive it.
    d = is_reapable({"sku": "SKU-1", "updated_at": None}, NOW, allow_undated=True)
    assert d.reap is False


def test_an_unparseable_timestamp_is_kept():
    assert is_reapable(orphan_doc(updated_at="not-a-date"), NOW).reap is False


def test_an_empty_sku_does_not_count_as_catalog_data():
    # A stripped field is absence, not confirmation -- the e2e healer used to
    # produce exactly this shape.
    assert is_reapable(orphan_doc(sku=""), NOW).reap is True


def test_a_completely_empty_document_is_kept():
    # No markers and no timestamp: nothing here justifies a delete.
    assert is_reapable({}, NOW).reap is False
