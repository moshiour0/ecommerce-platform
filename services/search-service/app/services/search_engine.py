from elasticsearch import AsyncElasticsearch
from ..schemas import SearchResponse, SearchResultItem

INDEX_NAME = "products"

async def search_products(es: AsyncElasticsearch, query: str, page: int, size: int) -> SearchResponse:
    from_offset = (page - 1) * size

    body = {
        "from": from_offset,
        "size": size,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["name^2", "description"],
                            "fuzziness": "AUTO"
                        }
                    }
                ],
                "filter": [
                    {"term": {"is_active": True}}
                    # Temporarily removed strict inventory check so newly seeded products appear
                    # {"range": {"quantity_available": {"gt": 0}}}
                ]
            }
        }
    }

    # If the user provides an empty query, fallback to match_all
    if not query:
        body["query"]["bool"]["must"] = [{"match_all": {}}]

    res = await es.search(index=INDEX_NAME, body=body)

    total_hits = res["hits"]["total"]["value"]
    hits = res["hits"]["hits"]

    items = []
    for hit in hits:
        source = hit["_source"]
        items.append(SearchResultItem(
            product_id=source.get("product_id"),
            name=source.get("name"),
            description=source.get("description"),
            price_cents=source.get("price_cents", 0),
            quantity_available=source.get("quantity_available", 0)
        ))

    return SearchResponse(total_hits=total_hits, items=items)