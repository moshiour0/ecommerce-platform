"""
Unit tests for the listing-permission decision in catalog-service.

Two things are being pinned here, and they are different in kind.

The first is a truthiness guard. `may_list_products` arrives over HTTP as
JSON, and this is the one boolean in the platform where reading a string,
a missing key or a null as permission puts unverified goods on sale. So the
check is `is True` and these tests feed it every near-miss they can.

The second is what happens when the answer does not arrive at all. Catalog
fails closed, which is the opposite of the usual availability instinct, and
the tests state the three outcomes as three different HTTP statuses because
they are three different instructions to the caller: 403 will never succeed,
404 is a bad id, 503 should be retried.
"""

import pytest

from conftest import catalog_rules

decide_listing = catalog_rules.decide_listing
ListingOutcome = catalog_rules.ListingOutcome

ALLOWED = {"seller_id": "s", "status": "active",
           "may_list_products": True, "may_receive_orders": True}


# ---------------------------------------------------------------------------
# the happy answer
# ---------------------------------------------------------------------------

def test_an_active_seller_may_list():
    decision = decide_listing(ALLOWED)
    assert decision.ok
    assert decision.outcome is ListingOutcome.ALLOWED


# ---------------------------------------------------------------------------
# the truthiness guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    False,          # the honest negative
    "false",        # a string, which is truthy in Python
    "true",         # a string, which is *also* not True
    None,
    0,
    1,              # truthy, still not True
    "yes",
    [],
    {},
])
def test_only_a_real_boolean_true_is_permission(value):
    # `if permission.get("may_list_products"):` would pass for "false", "true",
    # 1 and "yes" -- four ways for a serialisation change upstream to start
    # admitting listings nobody approved.
    decision = decide_listing({"status": "active", "may_list_products": value})
    assert not decision.ok, f"{value!r} was treated as permission to list"


def test_a_missing_key_is_not_permission():
    # An older seller-service, or a partial response, must not read as yes.
    assert not decide_listing({"status": "active"}).ok


def test_an_empty_body_is_not_permission():
    assert not decide_listing({}).ok


# ---------------------------------------------------------------------------
# the three ways to say no, which are not the same no
# ---------------------------------------------------------------------------

def test_a_refused_seller_is_403_and_names_the_status():
    decision = decide_listing({"status": "suspended", "may_list_products": False})
    assert decision.outcome is ListingOutcome.REFUSED
    assert decision.http_status == 403
    # The seller dashboard shows this. "Forbidden" alone is a support ticket.
    assert "suspended" in decision.detail


def test_an_unknown_seller_is_404_not_403():
    # A typo'd id and a suspended shop are different problems, and a caller
    # that cannot tell them apart shows the wrong message for both.
    decision = decide_listing(None)
    assert decision.outcome is ListingOutcome.UNKNOWN_SELLER
    assert decision.http_status == 404


def test_an_unreachable_seller_service_is_503_and_refuses():
    # Fails closed. Creating a product is a cold path -- no buyer request
    # touches seller-service -- so refusing for the minutes it is down costs a
    # retry, while accepting costs the whole control.
    decision = decide_listing(None, "seller-service circuit is open")
    assert not decision.ok
    assert decision.outcome is ListingOutcome.UNAVAILABLE
    assert decision.http_status == 503


def test_the_three_refusals_have_three_different_statuses():
    # 403 must never be retried, 503 must be. Collapsing them makes a client
    # either retry forever against a suspension or give up on a blip.
    statuses = {
        decide_listing({"status": "banned", "may_list_products": False}).http_status,
        decide_listing(None).http_status,
        decide_listing(None, "down").http_status,
    }
    assert statuses == {403, 404, 503}


# ---------------------------------------------------------------------------
# an absent answer is not a negative answer
# ---------------------------------------------------------------------------

def test_unreachable_wins_over_a_body():
    # If the transport failed, whatever body happens to be around is stale or
    # invented. Reporting it as a refusal would tell a perfectly good seller
    # they are suspended.
    decision = decide_listing(ALLOWED, "seller-service unreachable (ConnectError)")
    assert decision.outcome is ListingOutcome.UNAVAILABLE
    assert not decision.ok


def test_an_empty_unreachable_reason_does_not_trigger_the_unavailable_path():
    # "" means the call succeeded. Only a non-empty reason is a failure, or
    # every successful check would report the service as down.
    assert decide_listing(ALLOWED, "").ok


def test_the_unavailable_detail_says_it_refused_rather_than_verified():
    detail = decide_listing(None, "circuit is open").detail
    assert "refused rather than accepted unchecked" in detail
