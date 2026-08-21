"""
Asking reviews-service what buyers think of a seller.

This is the wire that makes §3g's quality_boost complete. The formula has
always read a rating and a review count; until reviews existed they were
reported as None, and ranking correctly treated the seller as *average*.

Rule 11: behind a breaker, and it **fails soft** -- the opposite of
seller-service's listing check, deliberately.

The difference is what each failure costs. A listing check that fails open lets
a suspended seller sell, so it fails closed. A ratings lookup that fails closed
would make every seller unrateable during a blip and silently reshuffle search
results; failing soft returns None, which ranking already treats as average.
Unknown-is-average is the cold-start rule the whole formula is built on, so an
outage degrades to exactly the behaviour that was correct yesterday.
"""

import logging
import os

import httpx
from python_common.resilience import (AsyncCircuitBreaker, BulkheadFullError,
                                      CircuitOpenError)

logger = logging.getLogger(__name__)

REVIEWS_SERVICE_URL = os.getenv("REVIEWS_SERVICE_URL",
                                "http://reviews-service:8022")
REVIEWS_TIMEOUT = float(os.getenv("REVIEWS_TIMEOUT_SECONDS", "3"))

_breaker = AsyncCircuitBreaker("reviews-service", bulkhead=10)


async def fetch_seller_rating(seller_id) -> dict:
    """This seller's rating and review count, or None for both.

    Never raises. Every failure -- unreachable, open circuit, a bad status,
    malformed JSON -- resolves to unknown, because a seller's ranking must not
    depend on whether a secondary service answered.
    """
    async def _get():
        async with httpx.AsyncClient(timeout=REVIEWS_TIMEOUT) as client:
            return await client.get(
                f"{REVIEWS_SERVICE_URL}/reviews/sellers/{seller_id}/summary")

    unknown = {"rating": None, "review_count": None}

    try:
        response = await _breaker.call(_get)
    except (CircuitOpenError, BulkheadFullError) as exc:
        logger.warning(f"Ratings unavailable for seller {seller_id} ({exc}); "
                       f"treating as unrated")
        return unknown
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(f"reviews-service unreachable for seller {seller_id} "
                       f"({type(exc).__name__}); treating as unrated")
        return unknown

    if response.status_code != 200:
        logger.warning(f"Ratings lookup for seller {seller_id} returned "
                       f"{response.status_code}; treating as unrated")
        return unknown

    try:
        body = response.json()
    except ValueError:
        logger.warning(f"Ratings lookup for seller {seller_id} returned "
                       f"unparseable JSON; treating as unrated")
        return unknown

    # A count of zero with no average is a seller nobody has rated, which is
    # unknown rather than bad -- passed through as-is so ranking applies the
    # cold-start rule instead of scoring them at the bottom.
    return {"rating": body.get("rating"),
            "review_count": body.get("review_count")}
