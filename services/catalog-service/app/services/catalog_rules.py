"""
Catalog decisions, as pure functions.

Mostly this module exists because of one bug that nothing caught for the life of
the project: the ProductCreated event did not carry `sku` or `is_active`, so
every product in the Elasticsearch read model had a null SKU and was active
regardless of what was asked for. All 49 indexed documents were missing a SKU.

Nothing failed. The API accepted `sku`, ignored it (pydantic drops unknown
fields), and returned 201. It accepted `is_active: false`, never stored it, and
echoed back `true` because the response schema supplies that as a default. The
indexer read `payload.get("sku")` and got None, and `payload.get("is_active",
True)` and got the fallback. Every layer behaved reasonably on its own and the
field simply evaporated between them.

So the event payload is built here, from a declared field list, and there is a
test asserting that every field the read model consumes is present in it. A
field that goes missing again fails a unit test instead of silently producing
nulls in search results.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

# Every key the ProductCreated payload must carry. stream-processor's
# event_router reads id, sku, name, description, is_active and created_at; the
# rest are here because a consumer added later should not have to ask catalog
# for what it already knew at publish time.
PRODUCT_EVENT_FIELDS = (
    "id", "seller_id", "category_id", "sku", "name", "description",
    "price_cents", "is_active", "created_at",
)

# The platform seller: a sentinel, not a merchant. Products created before
# sellers existed belong to it, and it is the default until seller-service can
# issue real ids. Deliberately greppable -- every reference to it is a place
# that still assumes a single tenant.
PLATFORM_SELLER_ID = "00000000-0000-0000-0000-000000000001"

# SKUs are compared exactly and used as an Elasticsearch keyword, so casing and
# stray whitespace are differences that nobody intends. Normalised once, on the
# way in.
SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,63}$")


class InvalidSku(ValueError):
    pass


def normalize_sku(raw: Optional[str]) -> str:
    """Trim and upper-case a SKU, or raise InvalidSku.

    Uppercasing is not cosmetic. `sku` is a keyword field in the index, so
    "abc-1" and "ABC-1" are two different products to search while being the
    same product to a human, and a uniqueness constraint on the raw string
    would let both exist.
    """
    if raw is None:
        raise InvalidSku("sku is required")

    candidate = str(raw).strip().upper()
    if not candidate:
        raise InvalidSku("sku is required")
    if not SKU_PATTERN.match(candidate):
        raise InvalidSku(
            "sku must be 2-64 characters of A-Z, 0-9, dot, underscore or "
            f"hyphen, starting alphanumeric (got {raw!r})")
    return candidate


def build_product_event(product: Any, created_at_iso: str) -> Dict[str, Any]:
    """The ProductCreated payload.

    Built from the persisted row rather than from the request, so the event
    describes what was actually stored. Building it from the request is how
    `is_active` came to be reported as true in the read model while nothing had
    stored it at all.
    """
    payload = {
        "id": str(product.id),
        # Without this the read model cannot tell whose product it is, so no
        # seller filter, no seller page, and no per-seller ranking signal.
        "seller_id": str(product.seller_id),
        "category_id": str(product.category_id),
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "price_cents": product.price_cents,
        "is_active": product.is_active,
        "created_at": created_at_iso,
    }

    # Cheap self-check: a field dropped from the dict above is a field that
    # silently becomes null in search. Raising here turns that into a failed
    # write rather than a quiet gap in the read model.
    missing = [f for f in PRODUCT_EVENT_FIELDS if f not in payload]
    if missing:
        raise RuntimeError(
            f"ProductCreated payload is missing {missing}; the read model "
            f"would index nulls for them")

    return payload


# ---------------------------------------------------------------------------
# May this seller list?
# ---------------------------------------------------------------------------
# seller-service owns the answer (ARCHITECTURE_STATE_FINAL.md §3e). Catalog
# asks and obeys; it does not re-derive the rule, because two services
# deriving "may this seller sell" from a status string is two services that
# will eventually disagree.
#
# The decision that needed writing down is what to do when the answer does not
# arrive. Catalog **fails closed**: an unreachable seller-service refuses new
# listings rather than accepting them unchecked.
#
# That is the opposite of the usual availability instinct, and it is right
# here for a reason that is specific rather than principled. Creating a
# product is a cold path -- a seller uploading a listing, not a buyer
# browsing or checking out, neither of which touches seller-service at all.
# Refusing listings for the minutes seller-service is down costs a retry.
# Accepting them costs exactly the control this check exists to be: an
# unverified or suspended seller putting goods on the platform during an
# outage, which is when nobody is watching.

class ListingOutcome(str, Enum):
    ALLOWED = "allowed"
    REFUSED = "refused"            # a real seller who may not sell
    UNKNOWN_SELLER = "unknown"     # no such seller
    UNAVAILABLE = "unavailable"    # seller-service could not be reached


@dataclass(frozen=True)
class ListingDecision:
    outcome: ListingOutcome
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is ListingOutcome.ALLOWED

    @property
    def http_status(self) -> int:
        """403 refused, 404 unknown, 503 unavailable.

        Distinguished because they are different instructions to the caller.
        403 is "this will not work until your account changes" and must not be
        retried. 503 is "try again shortly" and must be. Collapsing them into
        one status makes a seller dashboard either retry forever against a
        suspension or give up on a blip.
        """
        return {
            ListingOutcome.REFUSED: 403,
            ListingOutcome.UNKNOWN_SELLER: 404,
            ListingOutcome.UNAVAILABLE: 503,
        }[self.outcome]


def decide_listing(permission: Optional[Dict[str, Any]],
                   unreachable_reason: Optional[str] = None) -> ListingDecision:
    """Turn seller-service's answer -- or its absence -- into a decision.

    `permission` is the body of GET /sellers/{id}/permission, or None when the
    seller does not exist. `unreachable_reason` is set when the call could not
    be made at all, and wins over everything: a missing answer is not a
    negative answer, and must not be reported as one.
    """
    if unreachable_reason:
        return ListingDecision(
            ListingOutcome.UNAVAILABLE,
            f"cannot verify the seller right now ({unreachable_reason}); "
            f"listings are refused rather than accepted unchecked")

    if permission is None:
        return ListingDecision(
            ListingOutcome.UNKNOWN_SELLER,
            "no such seller")

    # Explicitly `is True`. A missing key, a null, or the string "false" must
    # not read as permission -- this is the one boolean in the platform where
    # a truthiness bug puts unverified goods on sale.
    if permission.get("may_list_products") is True:
        return ListingDecision(ListingOutcome.ALLOWED, "")

    status = permission.get("status", "unknown")
    return ListingDecision(
        ListingOutcome.REFUSED,
        f"seller is {status} and may not list products")
