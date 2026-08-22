"""
Which events change a seller's standing, and what to write when they do.

Pure: no Elasticsearch, no HTTP, no clock. The I/O lives in the router; this
answers "does this event mean a seller's signals moved" and "given these
signals, what belongs on their product documents".

Why this exists
---------------
§3g scores products on the seller behind them -- rating, on-time dispatch, and
the two rates that mean a buyer did not get what they ordered. Until now
search-service fetched those *per seller, per results page*, over HTTP: cached
for a minute, capped at 25 lookups, behind a breaker, every failure resolving
to average. That was correct and it was a round trip inside a search.

D4 said these belong denormalised onto the product documents so Elasticsearch
scores them in the query that already matched the text. This is that job.

The shape of the problem
------------------------
Signals are a fact about a *seller*; the read model is keyed by *product*. One
new review changes a number that is copied onto every product that seller
lists. So a refresh is an update-by-query over `seller_id`, not a document
write -- which is why it is worth being careful about how often it runs, and
why REFRESH_TRIGGERS below is a small explicit set rather than "any event
mentioning a seller".

Refreshed as a set, never in part. A partial write would leave today's rating
beside last week's return rate, and nothing about the document would look
wrong.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


# Events that mean a seller's signals have moved.
#
# Deliberately narrow. Every entry here costs an update-by-query across that
# seller's whole catalogue, so an event earns its place by changing one of the
# numbers ranking actually reads.
#
# Notably absent: SellerOrderConfirmed and SellerOrderDispatched. Dispatch
# timing feeds the on-time rate, but the rate cannot change until an order
# *concludes* -- and both of those are followed by a delivery, a return or a
# cancellation that is already on this list. Including them would roughly
# double the refresh volume to reach the same numbers a few minutes earlier.
REFRESH_TRIGGERS = frozenset({
    # Buyers' opinions.
    "ReviewPublished",
    "ReviewUpdated",

    # Concluded orders: the three outcomes compute_metrics counts.
    "SellerOrderDelivered",
    "SellerOrderReturned",
    "SellerOrderCancelled",

    # The shop itself. These are the event names seller-service actually
    # emits -- checked against its outbox rather than assumed, because a
    # trigger set full of plausible-looking names that nothing publishes is a
    # projection that silently never runs.
    "SellerApproved",
    "SellerReinstated",
    "SellerLocationUpdated",

    # And the events that take a seller *out* of good standing. Without these
    # the projection only ever learned good news: a suspension left every one
    # of that seller's product documents saying they could still sell, and
    # search went on showing them.
    "SellerSuspended",
    "SellerBanned",
    "SellerRejected",
})


# Events that change whether a seller's products may be *seen* at all, as
# opposed to how well they score.
#
# These must never be coalesced. The refresh window exists so that a seller
# delivering twenty orders in a minute does not trigger twenty update-by-queries
# to reach nearly the same rating -- which is fine, because a rating that is
# thirty seconds stale changes nothing anyone can observe.
#
# A visibility flag is not like that. Measured: a suspend and a reinstate
# arrived 1.3 seconds apart, the suspend refreshed, and the reinstate was
# dropped as "refreshed recently". The seller's entire catalogue stayed hidden
# afterwards -- and would have stayed hidden indefinitely, because nothing else
# was going to happen to a seller nobody could buy from. A suspension that
# cannot be undone is not a suspension, it is a deletion with extra steps.
VISIBILITY_EVENTS = frozenset({
    "SellerSuspended", "SellerBanned", "SellerRejected",
    "SellerApproved", "SellerReinstated",
})


def changes_visibility(event_type: str) -> bool:
    """Whether this event must refresh immediately, coalescing be damned."""
    return event_type in VISIBILITY_EVENTS


def is_refresh_trigger(event_type: str) -> bool:
    return event_type in REFRESH_TRIGGERS


def seller_id_from(event_type: str, payload: Dict[str, Any]) -> Optional[str]:
    """Whose signals moved, or None if the event cannot say.

    Seller events key on the seller itself; order and review events carry it as
    a field. Returning None rather than guessing means an event that has
    changed shape is skipped and logged instead of refreshing the wrong
    seller's catalogue.
    """
    for key in ("seller_id", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


@dataclass(frozen=True)
class SellerSignals:
    """Everything ranking reads about a seller, ready to be indexed."""

    # Whether this seller may still receive orders. Distinct from every other
    # field here: the rest tune a score, this one decides whether the product
    # should be findable at all.
    may_sell: Optional[bool]
    rating: Optional[float]
    review_count: Optional[int]
    on_time_dispatch_rate: Optional[float]
    cancellation_rate: Optional[float]
    return_rate: Optional[float]
    confidence: Optional[float]
    latitude: Optional[float]
    longitude: Optional[float]


def parse_coordinates(latitude: Any, longitude: Any):
    """A lat/lon pair, or (None, None) if it is not usable.

    seller_db stores these as text because the ranking needed a number and not
    a spatial index. That means anything can be in there, so this is where the
    parsing happens -- and a shop with a malformed location is *unlocated*
    rather than at the equator. (0, 0) is in the Gulf of Guinea, and silently
    putting every broken profile there would make them all each other's
    nearest neighbours.
    """
    if latitude is None or longitude is None:
        return None, None
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None, None

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None, None
    return lat, lon


def build_signal_fields(signals: SellerSignals) -> Dict[str, Any]:
    """The document fields to write, matching read_model's SELLER_SIGNALS set.

    Every field is written every time, including the ones that are None. That
    is deliberate: a seller whose last review was deleted must have their
    rating *cleared*, and a refresh that only wrote non-null values would leave
    the old number in place forever, with nothing to indicate it was stale.
    """
    latitude, longitude = signals.latitude, signals.longitude
    location = None
    if latitude is not None and longitude is not None:
        # Elasticsearch's object form, which is the unambiguous one. The array
        # form is [lon, lat] and the string form is "lat,lon", and mixing them
        # up puts shops in the wrong hemisphere without erroring.
        location = {"lat": latitude, "lon": longitude}

    return {
        # None means unknown, and search treats unknown as *visible*. That is
        # the deliberate direction: a projection that has not run yet, or a
        # seller-service that could not be reached during a refresh, must not
        # silently empty the catalogue. Suspension hides a seller only once the
        # platform positively knows they are suspended.
        "seller_may_sell": signals.may_sell,
        "seller_rating": signals.rating,
        "seller_review_count": signals.review_count,
        "seller_on_time_dispatch_rate": signals.on_time_dispatch_rate,
        "seller_cancellation_rate": signals.cancellation_rate,
        "seller_return_rate": signals.return_rate,
        "seller_confidence": signals.confidence,
        "seller_location": location,
    }


def signals_from_responses(metrics: Optional[Dict[str, Any]],
                           reviews: Optional[Dict[str, Any]],
                           profile: Optional[Dict[str, Any]]) -> SellerSignals:
    """Assemble what three services said into one set of signals.

    Any of the three may be None -- a service was unreachable, or has nothing
    for this seller. Missing stays missing: it becomes None in the document,
    and ranking treats an absent signal as *average* rather than as bad. That
    is the §3g cold-start rule, and it is the reason a failed refresh degrades
    to neutral instead of burying a seller.
    """
    metrics = metrics or {}
    reviews = reviews or {}
    profile = profile or {}

    latitude, longitude = parse_coordinates(profile.get("latitude"),
                                            profile.get("longitude"))

    # `may_list_products` is what seller-service reports on the profile; the
    # checkout path asks `may_receive_orders`. They agree today and are
    # separate on purpose, so this reads the one that governs visibility.
    may_sell = profile.get("may_list_products")
    if may_sell is not None:
        may_sell = bool(may_sell)

    return SellerSignals(
        may_sell=may_sell,
        rating=reviews.get("rating"),
        review_count=reviews.get("review_count"),
        on_time_dispatch_rate=metrics.get("on_time_dispatch_rate"),
        cancellation_rate=metrics.get("cancellation_rate"),
        return_rate=metrics.get("return_rate"),
        confidence=metrics.get("confidence"),
        latitude=latitude,
        longitude=longitude,
    )

# quality_from_document lives in python_common.read_model, beside the field
# names it reads: the projection writes `seller_rating` and ranking wants
# `rating`, so the translation must not exist twice.
