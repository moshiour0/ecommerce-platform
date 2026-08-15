import logging
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

ES_URL = "http://elasticsearch:9200"
INDEX_NAME = "products"

es = Elasticsearch([ES_URL])

def ensure_index_exists():
    if not es.indices.exists(index=INDEX_NAME):
        mapping = {
            "mappings": {
                "properties": {
                    "product_id": {"type": "keyword"},
                    "sku": {"type": "keyword"},
                    "name": {"type": "text"},
                    "description": {"type": "text"},
                    "price_cents": {"type": "integer"},
                    "quantity_available": {"type": "integer"},
                    "is_active": {"type": "boolean"},
                    "updated_at": {"type": "date"}
                }
            }
        }
        es.indices.create(index=INDEX_NAME, body=mapping)
        logger.info(f"Created Elasticsearch index: {INDEX_NAME}")
    else:
        logger.info(f"Elasticsearch index {INDEX_NAME} already exists")
