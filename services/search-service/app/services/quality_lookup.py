"""
Seller quality signals for the ranking pipeline.

Fetched per distinct seller on a results page, cached in process for a short
window, and behind a Rule 11 breaker. A page of twenty results usually spans
far fewer sellers, and the cache means the steady-state cost of ranking a
search is close to zero.

**This is not where it belongs in production.** D4 describes the quality boost
as a function_score over *indexed* fields, and that is right: the signals
should be denormalised onto the product documents by the CQRS pipeline so
Elasticsearch scores them in one query. Doing it here is correct and slower,
and it is what makes the ranking real today rather than in whichever sprint the
projection lands.

The failure mode that matters is the one this deliberately avoids: if
order-saga is slow or down, search must not be. Every failure resolves to
`None`, and `None` means *average* — the same cold-start rule ranking applies
to a seller with no record. A search that returned nothing because a metrics
service was unhealthy would be a far worse outcome than a search ordered
slightly less well.
"""

import logging
import os
import time
from typing import Dict, Iterable, Optional

import httpx
from python_common.resilience import (
    AsyncCircuitBreaker, BulkheadFullError, CircuitOpenError,
)

logger = logging.getLogger(__name__)

ORDER_SAGA_URL = os.getenv("ORDER_SAGA_URL", "http://order-saga:8012")

# Short, because a search must not wait on a ranking refinement. If the answer
# is not back in this long, ranking proceeds without it.
TIMEOUT_SECONDS = float(os.getenv("RANKING_QUALITY_TIMEOUT", "1.5"))

# Seller metrics move over days. A minute of staleness costs nothing and saves
# every repeat query on a popular seller.
CACHE_TTL_SECONDS = float(os.getenv("RANKING_QUALITY_TTL", "60"))

# Bounded so a pathological result set cannot make one search issue hundreds of
# calls. Beyond this the remaining sellers rank as average, which is the same
# thing that happens to a seller with no record.
MAX_LOOKUPS_PER_QUERY = int(os.getenv("RANKING_QUALITY_MAX_LOOKUPS", "25"))

_breaker = AsyncCircuitBreaker("order-saga-metrics", bulkhead=10)
_cache: Dict[str, tuple] = {}


def _cached(seller_id: str):
    entry = _cache.get(seller_id)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _cache.pop(seller_id, None)
        return None
    return value


async def _fetch(seller_id: str) -> Optional[dict]:
    async def _get():
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            return await client.get(
                f"{ORDER_SAGA_URL}/orders/seller-metrics",
                params={"seller_id": seller_id})

    try:
        response = await _breaker.call(_get)
    except (CircuitOpenError, BulkheadFullError):
        # Expected under load, and not worth a stack trace on every search.
        return None
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("seller metrics unavailable for %s: %s", seller_id, e)
        return None

    if response.status_code != 200:
        return None
    return response.json().get("quality")


async def quality_for(seller_ids: Iterable[str]) -> Dict[str, dict]:
    """Quality inputs per seller, best effort.

    A seller missing from the result ranks as average. That is deliberate and
    is the same rule as cold start: unknown is never *bad*, or an outage would
    quietly rerank the entire catalogue by which sellers happened to be cached.
    """
    unique = []
    for seller_id in seller_ids or []:
        if seller_id and seller_id not in unique:
            unique.append(str(seller_id))

    resolved: Dict[str, dict] = {}
    fetched = 0

    for seller_id in unique:
        cached = _cached(seller_id)
        if cached is not None:
            resolved[seller_id] = cached
            continue

        if fetched >= MAX_LOOKUPS_PER_QUERY:
            continue

        fetched += 1
        quality = await _fetch(seller_id)
        if quality is not None:
            _cache[seller_id] = (time.monotonic() + CACHE_TTL_SECONDS, quality)
            resolved[seller_id] = quality

    return resolved


def cache_state() -> dict:
    """For /health, so an operator can see whether ranking is actually using
    quality signals or silently falling back to average for everything."""
    return {
        "cached_sellers": len(_cache),
        "breaker": _breaker.state,
        "breaker_failures": _breaker.total_failures,
    }
