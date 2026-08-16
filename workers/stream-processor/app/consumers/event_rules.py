"""
Event identification for the CQRS denormalizer, as pure functions.

The consumer receives outbox payloads with no type on them. The outbox table
has a `type` column, but it does not survive into the payload this worker sees,
so the event's identity has to be inferred from the shape of its fields.

That inference used to end in `else: event_type = "ProductCreated"` -- anything
unrecognised was assumed to be a new product. InventoryReservationFailed
carries `quantity_available` and `quantity_requested`, while the inventory
signature looks for `total_quantity_available` and `quantity_reserved`, so every
failed reservation fell through to that fallback and was indexed as a product.
The result was a document whose id was the inventory row's id and whose every
catalog field was null:

    {"product_id": "1ae787ca-...", "sku": null, "name": null,
     "description": null, "is_active": true, "updated_at": null}

Three of those existed, one per inventory row that had ever failed a
reservation, and each flash-sale test created more. They were invisible to
search, which requires the catalog marker, so nothing ever complained.

Guessing is now refused. An unrecognised payload returns None and the caller
skips it, because indexing a document under a guessed type writes real data
into the read model on the strength of an assumption. A known event this worker
does not care about is named explicitly so it can be ignored on purpose rather
than by accident.
"""

from typing import Any, Dict, Optional

# Known event types, named so that "not ours" and "not recognised" are
# different answers. Only the first three are indexed; the rest are events this
# worker legitimately ignores.
PRODUCT_CREATED = "ProductCreated"
PRICE_UPDATED = "PriceUpdated"
INVENTORY_RESERVED = "InventoryReserved"
INVENTORY_RESERVATION_FAILED = "InventoryReservationFailed"
INVENTORY_RELEASED = "InventoryReleased"

INDEXED_EVENTS = frozenset({PRODUCT_CREATED, PRICE_UPDATED, INVENTORY_RESERVED})


# The Kafka header the Debezium EventRouter is configured to carry the outbox
# `type` column in. Headers rather than the message envelope because the
# envelope is part of the Avro value schema, and adding a field there is
# rejected by the FULL_TRANSITIVE compatibility the registry enforces.
TYPE_HEADER = "eventType"


def infer_event_type(msg_data: Dict[str, Any],
                     payload: Dict[str, Any],
                     headers: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The event's type, or None when it cannot be established.

    The header wins, then anything explicit on the message, and the shape
    checks are the last resort for events published before the connectors
    carried the type. None means "unknown", never "probably a product".
    """
    from_header = (headers or {}).get(TYPE_HEADER)
    if from_header:
        return from_header

    explicit = msg_data.get("type") or msg_data.get("event_type")
    if explicit:
        return explicit

    if not isinstance(payload, dict):
        return None

    # Checked before the reserved shape: a failed reservation and a successful
    # one both talk about quantities, and the failure is the one that used to
    # be mistaken for a product.
    if "reason" in payload and "quantity_requested" in payload:
        return INVENTORY_RESERVATION_FAILED

    if "base_price_cents" in payload:
        return PRICE_UPDATED

    if "total_quantity_available" in payload or "quantity_reserved" in payload:
        return INVENTORY_RESERVED

    if "name" in payload and "description" in payload:
        return PRODUCT_CREATED

    return None


def is_product_payload(payload: Dict[str, Any]) -> bool:
    """Whether a payload actually describes a product.

    Defence in depth behind infer_event_type. A ProductCreated with neither a
    name nor a sku is not a product however it was labelled, and indexing it
    produces exactly the null-filled documents this module exists to prevent.
    """
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("name")) or bool(payload.get("sku"))
