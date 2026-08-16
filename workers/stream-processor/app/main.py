import logging
import sys
import inspect
import asyncio
from pythonjsonlogger import jsonlogger

# Configure JSON Logging - Elevate ROOT logger to DEBUG
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
logHandler.setFormatter(formatter)
logging.getLogger().handlers = [logHandler]
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("elastic_transport").setLevel(logging.INFO)

# Local imports
from .indexers.es_client import ensure_index_exists
from .consumers.event_router import process_event
from .consumers.event_rules import (
    INDEXED_EVENTS, PRODUCT_CREATED, infer_event_type, is_product_payload,
)

try:
    from python_common.kafka_client import KafkaAvroConsumer
except ImportError as e:
    logger.error(f"CRITICAL: Failed to import python_common: {e}")
    raise e 

def main():
    logger.info("Starting Stream Processor Worker (CQRS Denormalizer)...")
    ensure_index_exists()

    topics = [
        "Product.events",
        "catalog.events",
        "Price.events",
        "Pricing.events",
        "pricing.events",
        "Inventory.events",
        "inventory.events"
    ]

    def callback_wrapper(msg_data, headers=None):
        import json
        
        if isinstance(msg_data, str):
            try:
                msg_data = json.loads(msg_data)
            except json.JSONDecodeError:
                raise ValueError(f"Received string payload but it is not valid JSON: {msg_data}")

        if not isinstance(msg_data, dict):
            raise ValueError(f"Expected a dictionary payload, got {type(msg_data)}")

        raw_payload = msg_data.get("payload", msg_data)

        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = raw_payload
        else:
            payload = raw_payload

        # The payload carries no type, so it is inferred from the shape of its
        # fields. See event_rules: this used to end in a fallback that called
        # anything unrecognised a ProductCreated, which indexed every failed
        # inventory reservation as a null-filled product.
        event_type = infer_event_type(msg_data, payload, headers)

        if event_type is None:
            logger.warning(
                "Unrecognised event shape; skipping rather than guessing. "
                "keys=%s", sorted(payload) if isinstance(payload, dict) else type(payload))
            return

        if event_type not in INDEXED_EVENTS:
            # A known event this worker does not maintain a projection for.
            # Ignored on purpose, which is different from ignored by accident.
            logger.debug("Ignoring %s; no projection for it here", event_type)
            return

        if event_type == PRODUCT_CREATED and not is_product_payload(payload):
            logger.warning(
                "ProductCreated with neither name nor sku; refusing to index. "
                "keys=%s", sorted(payload))
            return

        logger.debug(f"Routing to Elasticsearch -> Event: {event_type}, Data: {payload}")
        
        # E-2/E-3 Fix: Validate event_id is present for idempotent ES upsert
        doc_id = payload.get("id")
        if not doc_id:
            logger.error(f"Event missing 'id' field — cannot ensure idempotent ES write. Event type: {event_type}. Routing to DLQ.")
            raise ValueError(f"Event payload missing required 'id' field for idempotent indexing")
        
        logger.debug(f"Idempotent ES upsert: doc_id={doc_id}, event_type={event_type}")
        
        try:
            if inspect.iscoroutinefunction(process_event):
                asyncio.run(process_event(event_type, payload))
            else:
                res = process_event(event_type, payload)
                if inspect.isawaitable(res):
                    asyncio.run(res)
        except Exception as route_err:
            # No direct-to-Elasticsearch fallback. It used to PUT the raw event
            # payload as the whole document, which is the same erasure the read
            # model helper exists to prevent -- it would replace a real product
            # with whatever an event happened to contain, and it fired exactly
            # when something was already wrong. Letting the exception through
            # sends the message to dlq.<topic>, where a person can look at it.
            logger.error(
                f"Event router failed for doc_id={doc_id} ({event_type}): "
                f"{route_err}. Routing to DLQ.")
            raise

    consumer = KafkaAvroConsumer(
        broker_url="kafka:29092",
        schema_registry_url="http://schema-registry:8081",
        group_id="stream_processor_cg",
        topics=topics
    )

    logger.info(f"Subscribed to topics: {topics}")
    
    try:
        consumer.consume(process_func=callback_wrapper)
    except KeyboardInterrupt:
        logger.info("Stream processor stopped manually.")

if __name__ == "__main__":
    main()