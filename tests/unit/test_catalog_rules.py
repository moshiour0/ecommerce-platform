"""
Unit tests for catalog decisions.

The point of most of these is one bug: the ProductCreated event carried neither
`sku` nor `is_active`, so every product in the read model had a null SKU and was
active regardless of what was asked for. Nothing failed anywhere -- the API
accepted the field and dropped it, the indexer read it and got None. The test
that would have caught it is the payload-completeness one at the bottom.
"""

import pytest

from conftest import catalog_rules

normalize_sku = catalog_rules.normalize_sku
build_product_event = catalog_rules.build_product_event
InvalidSku = catalog_rules.InvalidSku
PRODUCT_EVENT_FIELDS = catalog_rules.PRODUCT_EVENT_FIELDS


class FakeProduct:
    """Just the attributes the event is built from."""

    def __init__(self, **kw):
        self.id = kw.get("id", "11111111-1111-1111-1111-111111111111")
        self.seller_id = kw.get("seller_id",
                                "00000000-0000-0000-0000-000000000001")
        self.category_id = kw.get("category_id",
                                  "22222222-2222-2222-2222-222222222222")
        self.sku = kw.get("sku", "SKU-ABC123")
        self.name = kw.get("name", "Keyboard")
        self.description = kw.get("description", "Tactile")
        self.price_cents = kw.get("price_cents", 15000)
        self.is_active = kw.get("is_active", True)


# ---------------------------------------------------------------------------
# sku normalisation
# ---------------------------------------------------------------------------

def test_a_sku_is_upper_cased():
    # sku is an Elasticsearch keyword, so "abc-1" and "ABC-1" would be two
    # different products to search and one product to a human -- and a
    # uniqueness constraint on the raw string would happily allow both.
    assert normalize_sku("abc-1") == "ABC-1"


def test_surrounding_whitespace_is_trimmed():
    assert normalize_sku("  SKU-1  ") == "SKU-1"


@pytest.mark.parametrize("sku", ["SKU-123", "AB", "A1._-X", "PRODUCT.1_A-2",
                                 "9LIVES"])
def test_valid_skus_pass(sku):
    assert normalize_sku(sku) == sku


@pytest.mark.parametrize("sku", [None, "", "   ", "A", "-LEADING",
                                 "_LEADING", ".LEADING", "HAS SPACE",
                                 "HAS/SLASH", "SEMI;COLON", "QUOTE'S",
                                 "A" * 65])
def test_invalid_skus_are_rejected(sku):
    with pytest.raises(InvalidSku):
        normalize_sku(sku)


def test_the_length_bounds_are_inclusive():
    assert normalize_sku("AB") == "AB"
    assert normalize_sku("A" * 64) == "A" * 64


def test_a_missing_sku_says_so():
    with pytest.raises(InvalidSku, match="required"):
        normalize_sku(None)


# ---------------------------------------------------------------------------
# the event payload — the bug this module exists for
# ---------------------------------------------------------------------------

def test_the_event_carries_every_field_the_read_model_needs():
    # The regression, stated directly. stream-processor indexes sku and
    # is_active from this payload; when they were absent it indexed None and a
    # default, and no error was raised anywhere.
    payload = build_product_event(FakeProduct(), "2026-08-16T12:00:00+00:00")
    for field in PRODUCT_EVENT_FIELDS:
        assert field in payload, f"the read model would index null for {field}"


def test_the_event_carries_the_sku():
    payload = build_product_event(FakeProduct(sku="SKU-XYZ"), "t")
    assert payload["sku"] == "SKU-XYZ"


def test_the_event_carries_is_active_false():
    # The one that could not previously be expressed at all: a caller asking
    # for an inactive product got an active one, silently.
    payload = build_product_event(FakeProduct(is_active=False), "t")
    assert payload["is_active"] is False


def test_the_event_is_built_from_the_row_not_the_request():
    # Building it from the request is what let is_active be reported in the
    # read model while nothing had stored it.
    product = FakeProduct(name="Stored Name", is_active=False)
    payload = build_product_event(product, "t")
    assert payload["name"] == "Stored Name"
    assert payload["is_active"] is False


def test_ids_are_serialised_as_strings():
    # They travel through JSONB and Avro; a UUID object is not JSON.
    payload = build_product_event(FakeProduct(), "t")
    assert isinstance(payload["id"], str)
    assert isinstance(payload["category_id"], str)


def test_a_dropped_field_fails_loudly():
    # Deleting a key from the payload builder must break a write rather than
    # quietly produce nulls in search results. Simulated by a row missing the
    # attribute entirely.
    class Incomplete(FakeProduct):
        def __init__(self):
            super().__init__()
            del self.sku

    with pytest.raises(AttributeError):
        build_product_event(Incomplete(), "t")


def test_null_description_survives_as_none():
    payload = build_product_event(FakeProduct(description=None), "t")
    assert payload["description"] is None


# ---------------------------------------------------------------------------
# multi-tenancy: every product has an owner
# ---------------------------------------------------------------------------

def test_the_event_carries_the_seller():
    # Without it the read model cannot tell whose product this is: no seller
    # filter, no shop page, no per-seller ranking signal.
    payload = build_product_event(FakeProduct(seller_id="seller-9"), "t")
    assert payload["seller_id"] == "seller-9"


def test_seller_id_is_part_of_the_event_contract():
    assert "seller_id" in PRODUCT_EVENT_FIELDS


def test_the_platform_seller_is_a_fixed_sentinel():
    # Products that existed before sellers did belong to it. It is deliberately
    # greppable: every reference is a place that still assumes one tenant.
    assert catalog_rules.PLATFORM_SELLER_ID == "00000000-0000-0000-0000-000000000001"


def test_the_seller_is_serialised_as_a_string():
    # It travels through JSONB and Avro; a UUID object is not JSON.
    payload = build_product_event(FakeProduct(), "t")
    assert isinstance(payload["seller_id"], str)
