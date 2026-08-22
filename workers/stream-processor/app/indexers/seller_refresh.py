"""
Copying a seller's standing onto every product they sell.

The I/O half of seller_projection. It asks three services what is currently
true about a seller, then writes that onto their product documents in one
update-by-query.

Read-through, not event-sourced
------------------------------
An event says a seller's signals *moved*, not what they moved to -- a review
carries one rating, and what ranking needs is the seller's average. Rebuilding
that average from the event stream would mean this worker keeping its own
running totals, which is a second copy of the truth that drifts from the first
and has no way to notice. So the event is a trigger and the numbers are fetched.

The cost is honest and worth stating: one refresh is two HTTP calls plus an
update-by-query across that seller's whole catalogue. That is why
REFRESH_TRIGGERS is a small explicit set, and why a busy seller is coalesced
rather than refreshed once per order.
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from elasticsearch import Elasticsearch

from python_common.read_model import SELLER_SIGNALS, validate_product_write

from ..seller_projection import build_signal_fields, signals_from_responses
from .es_client import INDEX_NAME, es

logger = logging.getLogger(__name__)

ORDER_SAGA_URL = os.getenv("ORDER_SAGA_URL", "http://order-saga:8012")
REVIEWS_SERVICE_URL = os.getenv("REVIEWS_SERVICE_URL",
                                "http://reviews-service:8022")
SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL",
                               "http://seller-service:8020")
FETCH_TIMEOUT = float(os.getenv("SIGNAL_FETCH_TIMEOUT_SECONDS", "5"))

# How long to wait before refreshing the same seller again.
#
# A seller with a hundred products who delivers twenty orders in a minute would
# otherwise trigger twenty update-by-queries over the same hundred documents to
# arrive at very nearly the same numbers. Ranking signals move slowly and
# nothing downstream can tell the difference between now and thirty seconds
# ago, so coalescing is free accuracy-wise and the difference between a
# projection and a load generator.
REFRESH_COALESCE_SECONDS = float(
    os.getenv("SELLER_REFRESH_COALESCE_SECONDS", "30"))

# seller_id -> monotonic time of the last refresh. In-process, so a restart
# forgets it and the next event refreshes immediately -- which is the safe
# direction to be wrong in.
_last_refresh: Dict[str, float] = {}


def _get_json(url: str) -> Optional[Dict[str, Any]]:
    """Fetch, or None. Never raises.

    A signal that cannot be fetched must not fail the whole refresh: the other
    two still have current values, and the missing one becomes None, which
    ranking treats as average. Failing the refresh instead would leave *every*
    signal stale, which is worse than one being neutral.
    """
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning(f"Signal fetch failed for {url}: {type(exc).__name__}")
        return None

    if response.status_code != 200:
        logger.warning(f"Signal fetch for {url} returned "
                       f"{response.status_code}")
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning(f"Signal fetch for {url} returned unparseable JSON")
        return None


def should_refresh(seller_id: str, now: Optional[float] = None,
                   window: float = REFRESH_COALESCE_SECONDS) -> bool:
    """Whether enough time has passed since this seller was last refreshed."""
    now = time.monotonic() if now is None else now
    last = _last_refresh.get(seller_id)
    return last is None or (now - last) >= window


def _mark_refreshed(seller_id: str) -> None:
    _last_refresh[seller_id] = time.monotonic()


def fetch_signals(seller_id: str):
    """What the three services currently say about this seller."""
    metrics = _get_json(
        f"{ORDER_SAGA_URL}/orders/seller-metrics?seller_id={seller_id}")
    reviews = _get_json(
        f"{REVIEWS_SERVICE_URL}/reviews/sellers/{seller_id}/summary")
    profile = _get_json(f"{SELLER_SERVICE_URL}/sellers/{seller_id}")
    return signals_from_responses(metrics, reviews, profile)


def refresh_seller(seller_id: str, force: bool = False) -> int:
    """Write this seller's current signals onto all of their products.

    Returns how many documents were updated. Zero is normal and not an error:
    a seller with no indexed products yet, or one whose refresh was coalesced.
    """
    if not force and not should_refresh(seller_id):
        logger.debug(f"Seller {seller_id} refreshed recently; coalescing")
        return 0

    signals = fetch_signals(seller_id)
    fields = build_signal_fields(signals)

    # The same ownership check every other read-model write goes through. This
    # writer may only touch the seller-signal fields, so a typo creates a
    # loud failure here rather than a dynamically-mapped field in
    # Elasticsearch that looks fine until someone searches on it.
    validate_product_write(SELLER_SIGNALS, fields.keys())

    fields["seller_signals_updated_at"] = _iso_now()

    # update_by_query rather than a document write, because the unit of change
    # is a seller and the unit of storage is a product. painless assigns each
    # field including the nulls: clearing matters as much as setting, or a
    # seller whose last review was removed keeps that rating forever.
    script_lines = []
    params = {}
    for index, (key, value) in enumerate(fields.items()):
        param = f"p{index}"
        script_lines.append(f"ctx._source.{key} = params.{param};")
        params[param] = value

    try:
        result = es.update_by_query(
            index=INDEX_NAME,
            body={
                "query": {"term": {"seller_id": seller_id}},
                "script": {"source": " ".join(script_lines),
                           "params": params, "lang": "painless"},
            },
            # proceed rather than abort: one contended document must not fail
            # the refresh for the seller's whole catalogue. But "proceed" means
            # the contended documents are *skipped*, and that is only harmless
            # for a score -- see the retry below.
            conflicts="proceed",
            refresh=True,
            request_timeout=60,
        )
    except Exception as exc:
        logger.error(f"Seller signal refresh failed for {seller_id}: {exc}")
        raise

    updated = result.get("updated", 0)
    conflicts = result.get("version_conflicts", 0)

    # A skipped document is a lie that nothing reports.
    #
    # Measured: a suspend refreshed "across 0 product(s)" because the checkout
    # moments earlier had triggered an InventoryReserved write to the same
    # document. Elasticsearch skipped it on a version conflict, the flag was
    # never written, and the seller stayed visible while suspended. The log
    # said the refresh had succeeded, because it counted `updated` and never
    # looked at `version_conflicts`.
    #
    # A stale rating is worth little. A stale visibility flag means a suspended
    # seller is still being sold from, so a forced refresh retries rather than
    # accepting the skip.
    # A skipped document is a lie that nothing reports.
    #
    # Measured twice, in both directions. A suspend refreshed "across 0
    # product(s)" because a checkout moments earlier had written to the same
    # document; Elasticsearch skipped it on a version conflict, the flag was
    # never written, and the seller stayed visible while suspended. The log
    # said the refresh had succeeded, because it counted `updated` and never
    # looked at `version_conflicts`.
    #
    # A stale rating is worth little. A stale visibility flag means a suspended
    # seller is still being sold from, or a reinstated one stays invisible with
    # no further event coming to fix it.
    #
    # So a forced refresh does not hope -- it *verifies*. An immediate retry is
    # not enough either: the first attempt at this retried 44ms later, while
    # the contending writer was still in flight, and matched nothing at all.
    if force:
        _ensure_visibility_applied(seller_id, fields, script_lines, params,
                                   conflicts)
    elif conflicts:
        logger.info(f"Seller signal refresh for {seller_id} skipped "
                    f"{conflicts} contended document(s); the next event will "
                    f"carry the same values")

    _mark_refreshed(seller_id)
    logger.info(f"Refreshed seller signals for {seller_id} across {updated} "
                f"product(s), {conflicts} conflict(s): "
                f"rating={signals.rating} reviews={signals.review_count} "
                f"located={signals.latitude is not None}")
    return updated


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# How hard to try before admitting a visibility change did not land.
VISIBILITY_ATTEMPTS = 5
VISIBILITY_BACKOFF_SECONDS = 0.4


def _ensure_visibility_applied(seller_id, fields, script_lines, params,
                               first_conflicts):
    """Keep writing until every one of this seller's documents agrees.

    Checks the result rather than the operation. `update_by_query` reports how
    many documents it changed and how many it skipped, and neither number
    answers the only question that matters here: does any document still
    disagree with the seller's current standing?

    Bounded, and loud when it gives up. A seller whose visibility does not
    match their status is a merchant either being sold from while suspended or
    invisible while active, and both need a person rather than a retry.
    """
    import time as _time

    desired = fields.get("seller_may_sell")
    if desired is None:
        # Unknown standing writes no visibility opinion, so there is nothing to
        # verify -- and search treats a missing flag as visible by design.
        return

    for attempt in range(VISIBILITY_ATTEMPTS):
        try:
            stale = es.count(index=INDEX_NAME, body={"query": {"bool": {
                "must": [{"term": {"seller_id": seller_id}}],
                "must_not": [{"term": {"seller_may_sell": desired}}]}}})
        except Exception as exc:
            logger.warning(f"Could not verify seller {seller_id} visibility: "
                           f"{exc}")
            return

        remaining = stale.get("count", 0)
        if remaining == 0:
            if attempt or first_conflicts:
                logger.info(f"Seller {seller_id} visibility settled to "
                            f"may_sell={desired} after {attempt + 1} "
                            f"attempt(s)")
            return

        _time.sleep(VISIBILITY_BACKOFF_SECONDS * (attempt + 1))
        try:
            es.update_by_query(
                index=INDEX_NAME,
                body={"query": {"term": {"seller_id": seller_id}},
                      "script": {"source": " ".join(script_lines),
                                 "params": params, "lang": "painless"}},
                conflicts="proceed", refresh=True, request_timeout=60)
        except Exception as exc:
            logger.warning(f"Retry {attempt + 1} for seller {seller_id} "
                           f"failed: {exc}")

    logger.error(
        f"Seller {seller_id} still has document(s) disagreeing with "
        f"may_sell={desired} after {VISIBILITY_ATTEMPTS} attempts. They are "
        f"either being sold from while suspended, or invisible while active. "
        f"This needs a person.")
