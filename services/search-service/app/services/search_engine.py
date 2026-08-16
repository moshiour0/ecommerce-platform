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
                    {"term": {"is_active": True}},
                    # A document is only a product once catalog has confirmed
                    # it. PriceUpdated and InventoryReserved are indexed with
                    # doc_as_upsert, so an event arriving before (or without) a
                    # ProductCreated conjures a document out of nothing -- the
                    # index currently holds one whose entire content is a stock
                    # level. Those were already excluded, but only by accident:
                    # they happen to have no is_active field, and the day
                    # anything gives the upsert path a default they would all
                    # become visible as nameless, priceless results.
                    #
                    # sku is the catalog marker: nothing else writes it.
                    {"exists": {"field": "sku"}}
                    # Inventory is deliberately not filtered here. Showing an
                    # out-of-stock product is a product decision, not a
                    # correctness one, and hiding them made freshly seeded
                    # products invisible during testing.
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