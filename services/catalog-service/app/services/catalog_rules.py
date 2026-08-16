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
from typing import Any, Dict, Optional

# Every key the ProductCreated payload must carry. stream-processor's
# event_router reads id, sku, name, description, is_active and created_at; the
# rest are here because a consumer added later should not have to ask catalog
# for what it already knew at publish time.
PRODUCT_EVENT_FIELDS = (
    "id", "category_id", "sku", "name", "description", "price_cents",
    "is_active", "created_at",
)

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
