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
            # keyword: categories are matched and aggregated on, never
            # full-text searched.
            "category_id": {"type": "keyword"},
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

            # --- seller signals, denormalised (ARCHITECTURE 3g) ------------
            # Here rather than fetched per seller at query time. The ranking
            # used to make one HTTP call per distinct seller on a results page,
            # capped at 25, cached for a minute and behind a breaker -- correct,
            # and a round trip per search. Indexed, Elasticsearch scores on
            # them in the same query that matched the text.
            "seller_may_sell": {"type": "boolean"},
            "seller_rating": {"type": "float"},
            "seller_review_count": {"type": "integer"},
            "seller_on_time_dispatch_rate": {"type": "float"},
            "seller_cancellation_rate": {"type": "float"},
            "seller_return_rate": {"type": "float"},
            "seller_confidence": {"type": "float"},
            # The shop's coordinates. geo_point so distance is computable in
            # the query rather than by pulling every seller's address.
            # Absent means unlocated, not far: distance_decay returns exactly
            # 1.0 for a missing distance, so an incomplete profile costs a
            # seller nothing.
            "seller_location": {"type": "geo_point"},
            "seller_signals_updated_at": {"type": "date"},
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
                _apply_new_mapping_fields()
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


def _apply_new_mapping_fields() -> None:
    """Add mapping fields the existing index does not have yet.

    `ensure_index_exists` only ever created. An index that already existed kept
    whatever mapping it was born with, so adding a field to INDEX_MAPPING did
    nothing on any environment that had run before -- which is every
    environment except a fresh volume.

    That is worse than it sounds, because Elasticsearch does not error on the
    missing field: it maps it dynamically on first write. A float arrives as a
    float and mostly works, so the gap stays invisible. `seller_location` is
    where it stops being invisible -- dynamically it becomes an object with two
    numbers, not a `geo_point`, and every geo query against it fails at the
    point somebody finally writes one.

    Only additive changes are attempted. Elasticsearch cannot change the type
    of an existing field, so a conflicting mapping is reported and left alone
    rather than papered over: that needs a reindex and a person deciding when.
    """
    try:
        current = es.indices.get_mapping(index=INDEX_NAME)
        existing = current[INDEX_NAME]["mappings"].get("properties", {})
    except Exception as exc:
        logger.warning("Could not read the mapping for %s: %s", INDEX_NAME, exc)
        return

    wanted = INDEX_MAPPING["mappings"]["properties"]
    missing = {name: spec for name, spec in wanted.items()
               if name not in existing}

    conflicting = [
        name for name, spec in wanted.items()
        if name in existing and existing[name].get("type") != spec.get("type")
    ]
    if conflicting:
        logger.error(
            "Index %s has fields whose type disagrees with the mapping: %s. "
            "Elasticsearch cannot change these in place; they need a reindex.",
            INDEX_NAME, conflicting)

    if not missing:
        return

    try:
        es.indices.put_mapping(index=INDEX_NAME,
                               body={"properties": missing})
        logger.info("Added %s field(s) to the %s mapping: %s",
                    len(missing), INDEX_NAME, sorted(missing))
    except Exception as exc:
        logger.error("Could not extend the mapping for %s: %s",
                     INDEX_NAME, exc)
