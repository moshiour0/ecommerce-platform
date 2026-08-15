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

    def callback_wrapper(msg_data):
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

        # THE FIX: Dynamically infer the exact event type based on the unique payload signature
        event_type = msg_data.get("type") or msg_data.get("event_type")
        
        if not event_type:
            if "base_price_cents" in payload:
                event_type = "PriceUpdated"
            elif "total_quantity_available" in payload or "quantity_reserved" in payload:
                event_type = "InventoryReserved"
            elif "name" in payload and "description" in payload:
                event_type = "ProductCreated"
            else:
                event_type = "ProductCreated" # Fallback

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
            logger.error(f"Event router execution failed: {route_err}. Engaging Direct ES Fallback for doc_id={doc_id}.")
            import requests
            from .indexers.es_client import ES_URL, INDEX_NAME
            # Was a second hardcoded http://elasticsearch:9200. Two addresses for
            # one dependency means pointing the worker at a different cluster
            # silently moves only half its writes.
            es_res = requests.put(
                f"{ES_URL}/{INDEX_NAME}/_doc/{doc_id}", json=payload, timeout=30)
            logger.debug(f"Direct Fallback ES Response: {es_res.status_code} - {es_res.text}")

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