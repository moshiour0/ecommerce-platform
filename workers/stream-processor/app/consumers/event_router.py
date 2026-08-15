import logging
from typing import Dict, Any
from ..indexers.es_client import es, INDEX_NAME

logger = logging.getLogger(__name__)

def process_event(event_type: str, payload: Dict[str, Any]):
    """
    CQRS Denormalizer routing logic.
    Updates the Elasticsearch Read Model based on events from multiple write services.
    """
    logger.info(f"Processing event: {event_type} for aggregate_id: {payload.get('aggregate_id', 'unknown')}")

    try:
        if event_type == "ProductCreated":
            doc_id = payload.get("id")
            doc = {
                "product_id": doc_id,
                "sku": payload.get("sku"),
                "name": payload.get("name"),
                "description": payload.get("description"),
                "is_active": payload.get("is_active", True),
                "updated_at": payload.get("created_at")
            }
            es.index(index=INDEX_NAME, id=doc_id, document=doc)
            logger.info(f"Indexed new ProductCreated document for {doc_id}")

        elif event_type == "PriceUpdated":
            # FIXED: Match the exact payload fields from the Pricing Service
            doc_id = payload.get("product_id")
            doc = {
                "doc": {
                    "price_cents": payload.get("base_price_cents"), # Fixed
                    "updated_at": payload.get("created_at")         # Fixed
                },
                "doc_as_upsert": True
            }
            es.update(index=INDEX_NAME, id=doc_id, body=doc)
            logger.info(f"Updated price for {doc_id} to {payload.get('base_price_cents')} cents")

        elif event_type == "InventoryReserved":
            doc_id = payload.get("product_id")
            doc = {
                "doc": {
                    "quantity_available": payload.get("total_quantity_available"),
                    "updated_at": payload.get("updated_at")
                },
                "doc_as_upsert": True
            }
            es.update(index=INDEX_NAME, id=doc_id, body=doc)
            logger.info(f"Updated inventory for {doc_id}. Available: {payload.get('total_quantity_available')}")

        else:
            logger.debug(f"Ignoring unhandled event type: {event_type}")

    except Exception as e:
        logger.error(f"Failed to index event {event_type} into Elasticsearch: {str(e)}")
        raise e