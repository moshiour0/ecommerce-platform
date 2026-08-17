import logging
import os
import time

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

# Was hardcoded to http://elasticsearch:9200. search-service already takes this
# from ELASTICSEARCH_URL, so the two disagreed about how they are configured
# and this one could not be pointed anywhere else without a rebuild.
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
INDEX_NAME = os.getenv("ES_PRODUCT_INDEX", "products")

# The default client has no retries and a short timeout. Elasticsearch answers
# its readiness probe at cluster status yellow but is still warming up and
# garbage collecting for a while afterwards, so the first request raced it and
# the worker died with ConnectionTimeout -- crash-looping against a dependency
# that was merely slow, not broken.
es = Elasticsearch(
    [ES_URL],
    request_timeout=30,
    retry_on_timeout=True,
    max_retries=5,
)

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "product_id": {"type": "keyword"},
            # keyword, not text: sellers are filtered and aggregated on,
            # never full-text searched.
            "seller_id": {"type": "keyword"},
            "sku": {"type": "keyword"},
            "name": {"type": "text"},
            "description": {"type": "text"},
            # Written only by pricing-service: the effective price.
            "price_cents": {"type": "integer"},
            # Written only by catalog-service: the list price, and the
            # fallback search uses until pricing has published.
            "base_price_cents": {"type": "integer"},
            "quantity_available": {"type": "integer"},
            "is_active": {"type": "boolean"},
            "updated_at": {"type": "date"},
        }
    }
}


def ensure_index_exists(max_attempts: int = 30, delay_seconds: float = 5.0) -> None:
    """Create the product index, waiting for Elasticsearch to accept traffic.

    A worker must tolerate its dependency starting slowly. Failing hard here
    means Kubernetes restarts the pod, which re-runs exactly this code against
    an Elasticsearch that is still warming up -- a loop that resolves itself
    only by luck. Retrying in-process converges instead.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            if es.indices.exists(index=INDEX_NAME):
                logger.info("Elasticsearch index %s already exists", INDEX_NAME)
                return
            es.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
            logger.info("Created Elasticsearch index: %s", INDEX_NAME)
            return
        except Exception as exc:  # transport errors, 503 while recovering, etc.
            last_error = exc
            logger.warning(
                "Elasticsearch not ready (attempt %s/%s): %s", attempt, max_attempts, exc
            )
            time.sleep(delay_seconds)

    raise RuntimeError(
        f"Elasticsearch at {ES_URL} did not become usable after "
        f"{max_attempts} attempts: {last_error}"
    )
