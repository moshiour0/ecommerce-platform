"""
Asking order-saga whether a purchase happened.

Rule 1: reviews-service cannot read order_db, so verification is a synchronous
HTTP call. That makes order-saga a hard dependency of writing a review, which
is the correct trade: a review whose purchase could not be verified is exactly
the review this feature exists to prevent.

Rule 11: behind a breaker, and it **fails closed**. If order-saga cannot be
reached the review is refused with a 503 rather than accepted unverified.
Accepting on failure would mean an outage is the way to publish whatever you
like, and outages are cheap to cause.
"""

import logging
import os

import httpx
from fastapi import HTTPException
from python_common.resilience import (AsyncCircuitBreaker, BulkheadFullError,
                                      CircuitOpenError)

logger = logging.getLogger(__name__)

ORDER_SAGA_URL = os.getenv("ORDER_SAGA_URL", "http://order-saga:8012")
ORDER_TIMEOUT = float(os.getenv("ORDER_TIMEOUT_SECONDS", "5"))

_breaker = AsyncCircuitBreaker("order-saga", bulkhead=10)


async def fetch_purchase(seller_order_id) -> dict:
    """The facts the eligibility rules need, or an honest failure.

    404 is passed through as 404: the purchase genuinely does not exist, and
    that is an answer rather than a fault. Everything else that is not a 200
    becomes a 503, because "we could not establish that you bought this" is a
    temporary condition and must not read as "you did not buy this".
    """
    async def _get():
        async with httpx.AsyncClient(timeout=ORDER_TIMEOUT) as client:
            return await client.get(
                f"{ORDER_SAGA_URL}/orders/seller-orders/"
                f"{seller_order_id}/purchase")

    try:
        response = await _breaker.call(_get)
    except (CircuitOpenError, BulkheadFullError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"cannot verify the purchase right now ({exc}); a review "
                   f"is not accepted unverified") from exc
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"order service unreachable ({type(exc).__name__}); a "
                   f"review is not accepted unverified") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="no such purchase")
    if response.status_code != 200:
        logger.error(f"Purchase lookup for {seller_order_id} returned "
                     f"{response.status_code}: {response.text[:200]}")
        raise HTTPException(
            status_code=503,
            detail=f"could not verify the purchase (order service returned "
                   f"{response.status_code}); refusing rather than accepting "
                   f"an unverified review")
    return response.json()
