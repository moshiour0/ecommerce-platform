"""
Unit tests for cart cache coherence.

These pin the decision that turns "Redis does not have this key" into either a
recovered cart or a genuinely empty one. The end-to-end counterpart --
tests/e2e/test_07_cart_cache_coherence.py -- proves the same thing against a
real Redis and a real Postgres by deleting a live cart's key out from under the
running service.

The distinction under test throughout is between a cache MISS (cached is None)
and a cached EMPTY cart (cached is {}). Collapsing those two is what let an
eviction read as "the user has nothing" and then destroy the durable copy on
the next write.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import cart_cache_rules

resolve_cart = cart_cache_rules.resolve_cart
merge_item = cart_cache_rules.merge_item
DurableCart = cart_cache_rules.DurableCart
CartSource = cart_cache_rules.CartSource
STATUS_ACTIVE = cart_cache_rules.STATUS_ACTIVE
STATUS_CHECKOUT_IN_PROGRESS = cart_cache_rules.STATUS_CHECKOUT_IN_PROGRESS
STATUS_EXPIRED = cart_cache_rules.STATUS_EXPIRED

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
EARLIER = NOW - timedelta(seconds=1)

PRODUCT_A = "6d9d9aa6-0fec-4011-a19e-afa5ad0e406c"
PRODUCT_B = "093ab7ca-457d-40f2-97f6-20cc44fdf476"


def active(items, expires_at=LATER):
    return DurableCart(items=items, status=STATUS_ACTIVE, expires_at=expires_at)


# ---------------------------------------------------------------------------
# cache hits
# ---------------------------------------------------------------------------

def test_cache_hit_is_used_without_consulting_postgres():
    view = resolve_cart({PRODUCT_A: 2}, active({PRODUCT_A: 99}), NOW)
    assert view.items == {PRODUCT_A: 2}
    assert view.source is CartSource.CACHE


def test_cached_empty_cart_is_a_hit_not_a_miss():
    # {} means "Redis says the cart is empty"; None means "Redis does not
    # know". Only the second may fall back, or every genuinely emptied cart
    # would be refilled from a stale durable row.
    view = resolve_cart({}, active({PRODUCT_A: 2}), NOW)
    assert view.items == {}
    assert view.source is CartSource.CACHE


def test_cache_hit_is_copied_not_aliased():
    cached = {PRODUCT_A: 1}
    view = resolve_cart(cached, None, NOW)
    view.items[PRODUCT_A] = 999
    assert cached == {PRODUCT_A: 1}


# ---------------------------------------------------------------------------
# the miss that started this
# ---------------------------------------------------------------------------

def test_miss_with_live_durable_cart_rehydrates():
    view = resolve_cart(None, active({PRODUCT_A: 2}), NOW)
    assert view.items == {PRODUCT_A: 2}
    assert view.source is CartSource.REHYDRATED
    assert view.rehydrated is True


def test_miss_with_no_durable_row_is_empty():
    view = resolve_cart(None, None, NOW)
    assert view.items == {}
    assert view.source is CartSource.EMPTY
    assert view.rehydrated is False


def test_rehydrated_items_are_copied_not_aliased():
    durable = active({PRODUCT_A: 2})
    view = resolve_cart(None, durable, NOW)
    view.items[PRODUCT_A] = 999
    assert durable.items == {PRODUCT_A: 2}


# ---------------------------------------------------------------------------
# what must NOT come back
# ---------------------------------------------------------------------------

def test_checkout_in_progress_is_never_rehydrated():
    # Those items are already inside a saga. Handing them back so the user can
    # check out again turns a lost cache entry into a duplicate order -- a far
    # worse bug than the empty cart this fallback exists to prevent.
    view = resolve_cart(
        None,
        DurableCart({PRODUCT_A: 2}, STATUS_CHECKOUT_IN_PROGRESS, LATER),
        NOW,
    )
    assert view.items == {}
    assert view.source is CartSource.EMPTY


def test_expired_status_is_never_rehydrated():
    view = resolve_cart(
        None, DurableCart({PRODUCT_A: 2}, STATUS_EXPIRED, LATER), NOW)
    assert view.items == {}


def test_active_row_past_its_expiry_is_not_rehydrated():
    # The sweeper marks rows on an interval, so an active row whose expires_at
    # has passed is ordinary rather than exceptional. It is still a dead cart,
    # and reading it during the gap must not resurrect it.
    view = resolve_cart(None, active({PRODUCT_A: 2}, expires_at=EARLIER), NOW)
    assert view.items == {}
    assert view.source is CartSource.EMPTY


def test_expiry_exactly_now_is_treated_as_expired():
    # Fail closed on the boundary: a cart expiring at this instant is gone.
    view = resolve_cart(None, active({PRODUCT_A: 2}, expires_at=NOW), NOW)
    assert view.items == {}


def test_active_row_without_an_expiry_still_rehydrates():
    view = resolve_cart(None, active({PRODUCT_A: 2}, expires_at=None), NOW)
    assert view.items == {PRODUCT_A: 2}
    assert view.source is CartSource.REHYDRATED


@pytest.mark.parametrize("status", ["active", "ACTIVE", "Active"])
def test_status_matching_is_exact(status):
    # cart_state.status is written by this service in lower case; anything else
    # is not a status this code recognises, and an unrecognised status must
    # fail closed rather than be guessed at.
    view = resolve_cart(None, DurableCart({PRODUCT_A: 2}, status, LATER), NOW)
    if status == "active":
        assert view.items == {PRODUCT_A: 2}
    else:
        assert view.items == {}


# ---------------------------------------------------------------------------
# merging a write onto the resolved cart
# ---------------------------------------------------------------------------

def test_merge_adds_a_new_product():
    assert merge_item({PRODUCT_A: 2}, PRODUCT_B, 1) == {PRODUCT_A: 2, PRODUCT_B: 1}


def test_merge_accumulates_an_existing_product():
    assert merge_item({PRODUCT_A: 2}, PRODUCT_A, 3) == {PRODUCT_A: 5}


def test_merge_does_not_mutate_its_input():
    before = {PRODUCT_A: 2}
    merge_item(before, PRODUCT_B, 1)
    assert before == {PRODUCT_A: 2}


def test_merge_onto_an_empty_cart():
    assert merge_item({}, PRODUCT_A, 1) == {PRODUCT_A: 1}


# ---------------------------------------------------------------------------
# the regression, end to end through the pure layer
# ---------------------------------------------------------------------------

def test_write_after_cache_loss_preserves_the_durable_cart():
    """The exact scenario observed against the running stack.

    Cart holds A x2. The Redis key is evicted. The user adds B. Before the
    fallback existed the service merged onto an empty view and overwrote
    cart_state with {B: 1}, destroying A in the one store that was supposed to
    survive the cache.
    """
    durable = active({PRODUCT_A: 2})

    view = resolve_cart(None, durable, NOW)          # eviction
    merged = merge_item(view.items, PRODUCT_B, 1)    # the user adds B

    assert merged == {PRODUCT_A: 2, PRODUCT_B: 1}
    assert PRODUCT_A in merged, "product A was destroyed by a cache miss"


def test_write_after_cache_loss_on_a_dead_cart_does_not_resurrect_it():
    # The mirror image: losing the cache must not revive a checked-out cart
    # just because the user adds something afterwards.
    durable = DurableCart({PRODUCT_A: 2}, STATUS_CHECKOUT_IN_PROGRESS, LATER)

    view = resolve_cart(None, durable, NOW)
    merged = merge_item(view.items, PRODUCT_B, 1)

    assert merged == {PRODUCT_B: 1}
