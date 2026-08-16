"""
Unit tests for read-model field ownership.

The bug this module exists to prevent was written three times in this
repository, so these tests are mostly attempts to write somebody else's field
and be told no.
"""

import pytest

from conftest import read_model

CATALOG = read_model.CATALOG
PRICING = read_model.PRICING
INVENTORY = read_model.INVENTORY
OwnershipError = read_model.OwnershipError
validate_product_write = read_model.validate_product_write
product_update_body = read_model.product_update_body
fields_owned_by = read_model.fields_owned_by
foreign_fields = read_model.foreign_fields


class FakeEs:
    """Records the call instead of making it."""

    def __init__(self):
        self.calls = []

    def update(self, index, id, body):
        self.calls.append({"index": index, "id": id, "body": body})
        return {"result": "updated"}


# ---------------------------------------------------------------------------
# the three bugs, as tests
# ---------------------------------------------------------------------------

def test_catalog_cannot_write_the_price():
    # reindex-worker came one line from doing exactly this, which would have
    # rolled every product back to its creation price.
    with pytest.raises(OwnershipError, match="pricing-service"):
        validate_product_write(CATALOG, ["name", "price_cents"])


def test_catalog_cannot_write_stock():
    with pytest.raises(OwnershipError, match="inventory-service"):
        validate_product_write(CATALOG, ["name", "quantity_available"])


def test_pricing_cannot_write_catalog_fields():
    # The healer wrote name, description and is_active from a script, forcing
    # every deactivated product back to active.
    with pytest.raises(OwnershipError):
        validate_product_write(PRICING, ["price_cents", "is_active"])


def test_inventory_cannot_write_the_price():
    with pytest.raises(OwnershipError):
        validate_product_write(INVENTORY, ["quantity_available", "price_cents"])


def test_the_error_names_the_real_owner():
    # So whoever hits this knows who to talk to rather than deleting the check.
    with pytest.raises(OwnershipError) as exc:
        validate_product_write(CATALOG, ["price_cents"])
    assert "pricing-service" in str(exc.value)


# ---------------------------------------------------------------------------
# what each owner may write
# ---------------------------------------------------------------------------

def test_each_owner_can_write_its_own_fields():
    validate_product_write(CATALOG, ["sku", "name", "description", "is_active",
                                     "base_price_cents", "catalog_updated_at"])
    validate_product_write(PRICING, ["price_cents"])
    validate_product_write(INVENTORY, ["quantity_available"])


def test_the_shared_timestamp_is_writable_by_everyone():
    # Which is exactly why it cannot be used to tell whose write was last, and
    # why catalog_updated_at exists.
    for owner in (CATALOG, PRICING, INVENTORY):
        validate_product_write(owner, ["updated_at"])


def test_no_field_has_two_owners():
    # The property the whole module is for. A field appearing in two owners'
    # sets is the ambiguity that made price_cents unresolvable.
    owned = [fields_owned_by(o) - {"updated_at"}
             for o in (CATALOG, PRICING, INVENTORY)]
    for i, a in enumerate(owned):
        for b in owned[i + 1:]:
            assert not (a & b), f"fields claimed twice: {a & b}"


def test_owned_and_foreign_partition_the_document():
    for owner in (CATALOG, PRICING, INVENTORY):
        assert not (fields_owned_by(owner) & foreign_fields(owner))
        assert (fields_owned_by(owner) | foreign_fields(owner)
                == set(read_model.PRODUCT_FIELD_OWNERS))


def test_an_empty_write_is_allowed():
    validate_product_write(CATALOG, [])


# ---------------------------------------------------------------------------
# typos
# ---------------------------------------------------------------------------

def test_unknown_fields_are_refused():
    # Elasticsearch maps new fields dynamically, so a typo does not fail -- it
    # creates a second field beside the real one and nothing looks wrong until
    # a search returns nothing.
    with pytest.raises(OwnershipError, match="nobody owns"):
        validate_product_write(INVENTORY, ["quantity_avaliable"])


def test_an_unknown_writer_is_refused():
    with pytest.raises(OwnershipError, match="unknown writer"):
        validate_product_write("some-new-service", ["name"])


# ---------------------------------------------------------------------------
# the write itself
# ---------------------------------------------------------------------------

def test_the_body_is_always_a_partial_upsert():
    body = product_update_body(PRICING, {"price_cents": 1500})
    assert body["doc_as_upsert"] is True
    assert body["doc"] == {"price_cents": 1500}
    # No key that would replace the document.
    assert "document" not in body


def test_the_body_copies_its_input():
    fields = {"price_cents": 1500}
    body = product_update_body(PRICING, fields)
    body["doc"]["price_cents"] = 9999
    assert fields["price_cents"] == 1500


def test_write_product_sends_a_partial_update():
    es = FakeEs()
    read_model.write_product(es, "p-1", PRICING, {"price_cents": 1500})
    call = es.calls[0]
    assert call["id"] == "p-1"
    assert call["index"] == read_model.PRODUCTS_INDEX
    assert call["body"]["doc_as_upsert"] is True


def test_write_product_always_records_the_product_id():
    # Upserts from pricing and inventory used to omit it, leaving documents
    # whose entire content was a stock level and a timestamp.
    es = FakeEs()
    read_model.write_product(es, "p-1", INVENTORY, {"quantity_available": 3})
    assert es.calls[0]["body"]["doc"]["product_id"] == "p-1"


def test_write_product_refuses_a_foreign_field_before_calling_elasticsearch():
    es = FakeEs()
    with pytest.raises(OwnershipError):
        read_model.write_product(es, "p-1", INVENTORY, {"price_cents": 1})
    assert es.calls == [], "a refused write still reached Elasticsearch"


def test_there_is_no_whole_document_write_function():
    # The point of the module. If one existed it would eventually be used --
    # that is the entire history of this file.
    exported = [n for n in dir(read_model) if not n.startswith("_")]
    for forbidden in ("index_product", "replace_product", "put_product",
                      "set_product"):
        assert forbidden not in exported
