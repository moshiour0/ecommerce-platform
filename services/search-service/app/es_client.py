from elasticsearch import AsyncElasticsearch

ES_URL = "http://elasticsearch:9200"

async_es = AsyncElasticsearch([ES_URL])

async def get_es_client():
    yield async_es
