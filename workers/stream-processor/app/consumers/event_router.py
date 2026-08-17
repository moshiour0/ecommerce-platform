import logging
from typing import Dict, Any
from ..indexers.es_client import es, INDEX_NAME

from python_common.read_model import CATALOG, INVENTORY, PRICING, write_product

logger = logging.getLogger(__name__)

def process_event(event_type: str, payload: Dict[str, Any]):
    """
    CQRS Denormalizer routing logic.
    Updates the Elasticsearch Read Model based on events from multiple write services.

    Every write goes through python_common.read_model.write_product, which is
    always a partial upsert and refuses fields the emitting service does not
    own. That is not stylistic. ProductCreated was indexed here with es.index(),
    which REPLACES the document, so a redelivered creation event erased the
    price and stock written by the other two handlers -- and Kafka is
    at-least-once, so redelivery is routine. The helper makes the whole-document
    write unavailable rather than merely discouraged.
    """
    logger.info(f"Processing event: {event_type} for aggregate_id: {payload.get('aggregate_id', 'unknown')}")

    try:
        if event_type == "ProductCreated":
            doc_id = payload.get("id")
            write_product(es, doc_id, CATALOG, {
                "seller_id": payload.get("seller_id"),
                "sku": payload.get("sku"),
                "name": payload.get("name"),
                "description": payload.get("description"),
                "is_active": payload.get("is_active", True),
                # catalog's list price, under its own name. pricing-service owns
                # price_cents (§3); writing catalog's number there would give one
                # field two writers and let a stale base price overwrite a
                # published one.
                "base_price_cents": payload.get("price_cents"),
                "updated_at": payload.get("created_at"),
            }, index=INDEX_NAME)
            logger.info(f"Indexed new ProductCreated document for {doc_id}")

        elif event_type == "PriceUpdated":
            # The payload field is base_price_cents, which is pricing's own
            # column name; it lands in price_cents because that is the effective
            # price the read model serves.
            doc_id = payload.get("product_id")
            write_product(es, doc_id, PRICING, {
                "price_cents": payload.get("base_price_cents"),
                "updated_at": payload.get("created_at"),
            }, index=INDEX_NAME)
            logger.info(f"Updated price for {doc_id} to {payload.get('base_price_cents')} cents")

        elif event_type == "InventoryReserved":
            doc_id = payload.get("product_id")
            write_product(es, doc_id, INVENTORY, {
                "quantity_available": payload.get("total_quantity_available"),
                "updated_at": payload.get("updated_at"),
            }, index=INDEX_NAME)
            logger.info(f"Updated inventory for {doc_id}. Available: {payload.get('total_quantity_available')}")

        else:
            logger.debug(f"Ignoring unhandled event type: {event_type}")

    except Exception as e:
        logger.error(f"Failed to index event {event_type} into Elasticsearch: {str(e)}")
        raise e
