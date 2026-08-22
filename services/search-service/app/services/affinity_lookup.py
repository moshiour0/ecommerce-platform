"""
Fetching one buyer's affinity profile for one search.

Once per search, not once per result. The profile is a fact about the buyer,
so a hundred results need it once -- which is the difference between this and
the per-seller quality lookup it sits beside, and the reason that one has now
been replaced by an indexed field while this one has not needed to be.

Rule 11: behind a breaker, and it **fails soft**. A profile that cannot be
fetched becomes None, which `personalisation_boost` turns into exactly 1.0 --
the same score the whole platform produced before this service existed. An
outage costs personalisation and nothing else, and a search must never fail
because a ranking refinement was unavailable.
"""

import logging
import os
from typing import Any, Dict, Optional

import httpx
from python_common.resilience import (AsyncCircuitBreaker, BulkheadFullError,
                                      CircuitOpenError)

logger = logging.getLogger(__name__)

PERSONALISATION_URL = os.getenv("PERSONALISATION_SERVICE_URL",
                                "http://personalisation-service:8023")
# Tight on purpose. This is a refinement on the path of every search, and a
# slow answer is worth less than a fast neutral one.
AFFINITY_TIMEOUT = float(os.getenv("AFFINITY_TIMEOUT_SECONDS", "1.5"))

_breaker = AsyncCircuitBreaker("personalisation-service", bulkhead=10)


async def affinity_profile(buyer_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """This buyer's profile, or None.

    None for an anonymous search, by construction rather than by accident:
    there is no device or session profile to fall back to, so a buyer who is
    not identified is not personalised. That is the correct behaviour and also
    the private one.

    Never raises.
    """
    if not buyer_id:
        return None

    async def _get():
        async with httpx.AsyncClient(timeout=AFFINITY_TIMEOUT) as client:
            return await client.get(
                f"{PERSONALISATION_URL}/personalisation/affinity/{buyer_id}")

    try:
        response = await _breaker.call(_get)
    except (CircuitOpenError, BulkheadFullError) as exc:
        logger.warning(f"Affinity unavailable ({exc}); ranking without it")
        return None
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(f"personalisation-service unreachable "
                       f"({type(exc).__name__}); ranking without it")
        return None

    if response.status_code != 200:
        logger.warning(f"Affinity lookup returned {response.status_code}; "
                       f"ranking without it")
        return None

    try:
        return response.json()
    except ValueError:
        logger.warning("Affinity lookup returned unparseable JSON; "
                       "ranking without it")
        return None
