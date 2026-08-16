"""
Cart cache coherence, as pure functions.

Redis holds the cart's contents; Postgres holds a cart_state row used by the
expiry sweeper. Redis is written first on every add, so it is the fast path --
but it is memory, and memory is evictable. The question this module answers is
what the cart *is* when those two disagree.

Before this existed, get_cart read Redis and nothing else, so a missing key was
indistinguishable from an empty cart. Deleting only the Redis key of a live
cart -- an eviction, a maxmemory trim, a Redis restart -- produced this against
the running stack:

    GET /cart/{user}          -> 200 {"items": []}
    postgres cart_state       -> {"<product A>": 2}, status=active

The user is shown an empty cart while the durable row still says they have two
of product A. That alone is only stale. The next write is what makes it
permanent: add_to_cart rebuilt the cart from the empty Redis view and then
overwrote cart_state with the result, so product A was destroyed in Postgres
too:

    after adding product B    -> postgres cart_state = {"<product B>": 1}
    A present in postgres     -> False

A cache that quietly deletes the durable copy is worse than no cache. So a miss
now falls back to the durable row instead of being read as emptiness, and the
merge that follows a write is applied to the resolved cart rather than to
whatever Redis happened to still hold.

The counterpart to these rules is tests/e2e/test_07_cart_cache_coherence.py,
which deletes a live cart's key out from under the running service and asserts
what the user sees next.

Rehydration is deliberately conservative. It resurrects a cart only when the
durable row says one is genuinely live: any other status, or an expiry that has
already passed, resolves to empty. Bringing back a cart that is mid-checkout
would let items already handed to the saga be checked out a second time, which
trades a display bug for a double order.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

# product_id (as a string, the JSON key form) -> quantity
CartItems = Dict[str, int]


class CartSource(str, Enum):
    CACHE = "cache"            # Redis answered
    REHYDRATED = "rehydrated"  # Redis missed and the durable row was live
    EMPTY = "empty"            # no live cart in either place


# cart_state.status values. Only one of them describes a cart a user may still
# add to; the rest are terminal as far as the cache is concerned.
STATUS_ACTIVE = "active"
STATUS_CHECKOUT_IN_PROGRESS = "checkout_in_progress"
STATUS_EXPIRED = "expired"


@dataclass(frozen=True)
class DurableCart:
    """The part of a cart_state row this decision depends on."""

    items: CartItems
    status: str
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class CartView:
    """The cart as the service should treat it, and where it came from."""

    items: CartItems
    source: CartSource

    @property
    def rehydrated(self) -> bool:
        """True when this view was recovered from Postgres after a cache miss.

        Callers use this to decide whether to write the recovered cart back
        into Redis: re-warming on a hit would be a pointless round trip, and
        never re-warming means every read after an eviction pays for Postgres.
        """
        return self.source is CartSource.REHYDRATED

    @property
    def empty(self) -> bool:
        return not self.items


def resolve_cart(cached: Optional[CartItems],
                 durable: Optional[DurableCart],
                 now: datetime) -> CartView:
    """Decide what a user's cart contains, given both stores.

    `cached` is None for a cache miss -- the key was absent. That is the case
    this function exists for, and it is not the same as a cached empty cart:
    the first means "Redis does not know", the second means "Redis says the
    cart is empty". Collapsing the two is the original defect.

    `durable` is None when there is no cart_state row at all. `now` is passed
    in rather than read from the clock so expiry is testable without waiting
    an hour, matching the fake-clock convention in checkout_lock.
    """
    if cached is not None:
        # Redis is written before Postgres on every add, so when it answers it
        # is the more recent of the two. An empty dict here is a real answer,
        # not a miss, and is honoured as one.
        return CartView(dict(cached), CartSource.CACHE)

    if durable is None:
        return CartView({}, CartSource.EMPTY)

    if durable.status != STATUS_ACTIVE:
        # checkout_in_progress above all: those items are already inside a
        # saga. Resurrecting them so the user can check out again turns a lost
        # cache into a duplicate order.
        return CartView({}, CartSource.EMPTY)

    if durable.expires_at is not None and durable.expires_at <= now:
        # Expired by the clock even though the sweeper has not marked the row
        # yet. The sweeper runs on an interval, so this window is ordinary --
        # and a cart the platform already considers dead must not come back
        # merely because someone read it during the gap.
        return CartView({}, CartSource.EMPTY)

    return CartView(dict(durable.items), CartSource.REHYDRATED)


def merge_item(items: CartItems, product_id: str, quantity: int) -> CartItems:
    """Add a quantity of one product to a cart, returning a new mapping.

    Returns a copy rather than mutating: the caller holds the resolved view,
    and mutating it in place makes "what did the cart look like before this
    write" unanswerable, which is precisely what the durable copy needs to
    know. Quantity is already constrained to be positive by CartItem's
    pydantic field, so it is not re-validated here.
    """
    merged = dict(items)
    merged[product_id] = merged.get(product_id, 0) + quantity
    return merged
