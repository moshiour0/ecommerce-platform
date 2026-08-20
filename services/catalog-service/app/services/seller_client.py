"""
Asking seller-service whether a seller may list.

Catalog does not hold seller state and does not re-derive the rule. It asks
the service that owns the answer and obeys it, because two services deriving
"may this seller sell" from a status string is two services that will
eventually disagree — and the one that disagrees quietly is the one that keeps
selling.

Synchronous rather than a local projection fed by `Seller.events`, which was
the other option. The projection wins on availability and loses on
correctness: it is eventually consistent, so a seller suspended for selling
counterfeits keeps listing for as long as the lag lasts. Creating a product is
a cold path — no buyer request touches seller-service — so the availability
the projection buys is worth little here, and the correctness it costs is
exactly the control being built.

Behind a circuit breaker per Rule 11, and it fails **closed**: see
catalog_rules.decide_listing for why refusing beats accepting-unchecked when
the answer does not arrive.
"""

import logging
import os

import httpx
from python_common.resilience import (
    AsyncCircuitBreaker, BulkheadFullError, CircuitOpenError,
)

logger = logging.getLogger(__name__)

SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://seller-service:8020")

# Short. This call sits in front of a seller pressing "publish", and a slow
# answer is worse than a refusal they can retry -- the breaker needs failures
# to be fast to be able to open at all.
TIMEOUT_SECONDS = float(os.getenv("SELLER_SERVICE_TIMEOUT", "3.0"))

_breaker = AsyncCircuitBreaker("seller-service", bulkhead=10)


async def fetch_permission(seller_id):
    """(permission_body, unreachable_reason).

    Exactly one of the two is set:

      ({...}, None)  seller-service answered about a seller that exists
      (None,  None)  seller-service answered: no such seller
      (None,  "...") seller-service did not answer

    The third case is kept distinct from the second on purpose. "No such
    seller" is an answer and "I could not ask" is not, and a client that
    conflates them turns every outage into a wave of 404s that look like bad
    seller ids.
    """
    async def _get():
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            return await client.get(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/permission")

    try:
        response = await _breaker.call(_get)
    except CircuitOpenError as e:
        logger.warning("seller-service circuit open; refusing listing: %s", e)
        return None, "seller-service circuit is open"
    except BulkheadFullError as e:
        logger.warning("seller-service bulkhead full; refusing listing: %s", e)
        return None, "too many seller checks in flight"
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("seller-service unreachable: %s", e)
        return None, f"seller-service unreachable ({type(e).__name__})"

    if response.status_code == 404:
        return None, None

    if response.status_code >= 500:
        # A 5xx is the downstream failing, which the breaker has already
        # counted. Treated as "could not ask" rather than as a refusal.
        logger.warning("seller-service returned %s", response.status_code)
        return None, f"seller-service returned {response.status_code}"

    if response.status_code != 200:
        # 4xx other than 404 means this request is wrong -- a malformed id,
        # say -- and retrying it will not help. Reported as an unanswerable
        # question rather than as a refusal, because the seller is not the
        # problem.
        return None, f"seller-service rejected the query ({response.status_code})"

    return response.json(), None


def breaker_state() -> dict:
    """For /health, so an operator can see the circuit without reading logs."""
    return {
        "state": _breaker.state,
        "total_calls": _breaker.total_calls,
        "total_failures": _breaker.total_failures,
        "total_short_circuited": _breaker.total_short_circuited,
    }
