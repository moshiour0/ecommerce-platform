"""
Unit tests for search result decisions.

Almost all of these are about one number. Two services had a claim on the
price, and the read model resolved it by accident: whichever wrote last won,
and a product nobody had priced was served at 0.
"""

import pytest

from conftest import search_rules

effective_price = search_rules.effective_price


def test_pricing_wins_when_it_has_published():
    # pricing-service owns the effective price (§3); catalog's is a list price.
    price, origin = effective_price({"price_cents": 1200, "base_price_cents": 1500})
    assert (price, origin) == (1200, "pricing")


def test_catalog_base_is_used_until_pricing_publishes():
    # The gap this closes: a product created a moment ago, whose PriceUpdated
    # has not arrived, used to be served at 0.
    price, origin = effective_price({"base_price_cents": 1500})
    assert (price, origin) == (1500, "catalog-base")


def test_an_unpriced_product_is_not_free():
    # 0 is a price a customer can be charged. "We do not know yet" is not.
    price, origin = effective_price({"name": "New product"})
    assert price is None
    assert origin == "unpriced"


def test_a_genuine_zero_from_pricing_is_honoured():
    # Free is a legitimate price when a service actually said so.
    assert effective_price({"price_cents": 0}) == (0, "pricing")


def test_a_genuine_zero_base_is_honoured():
    assert effective_price({"base_price_cents": 0}) == (0, "catalog-base")


def test_a_null_price_falls_through_to_the_base():
    # An explicit null is absence, not a value.
    price, origin = effective_price({"price_cents": None, "base_price_cents": 900})
    assert (price, origin) == (900, "catalog-base")


def test_a_negative_price_is_refused_and_falls_through():
    # Nothing should produce one; if something does, it must not be shown.
    price, origin = effective_price({"price_cents": -5, "base_price_cents": 900})
    assert (price, origin) == (900, "catalog-base")


def test_both_negative_is_unpriced():
    assert effective_price({"price_cents": -1, "base_price_cents": -2})[1] == "unpriced"


@pytest.mark.parametrize("bad", ["1500", 15.0, True, False, [], {}])
def test_non_integer_prices_are_ignored(bad):
    # Rule 6: money is integer cents. A float or a string in the document means
    # something upstream is wrong, and rendering it would spread the error.
    price, origin = effective_price({"price_cents": bad, "base_price_cents": 900})
    assert (price, origin) == (900, "catalog-base")


def test_a_boolean_is_not_a_price():
    # True is an int in Python and would otherwise be shown as 1 cent.
    assert effective_price({"price_cents": True})[1] == "unpriced"


def test_an_empty_document_is_unpriced():
    assert effective_price({}) == (None, "unpriced")
