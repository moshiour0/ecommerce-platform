from elasticsearch import AsyncElasticsearch
from ..schemas import SearchResponse, SearchResultItem
from .quality_lookup import quality_for
from .ranking_rules import rank
from .search_rules import effective_price

INDEX_NAME = "products"

async def search_products(es: AsyncElasticsearch, query: str, page: int,
                          size: int, seller_id: str = None) -> SearchResponse:
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

    # One seller's shop front. A term filter rather than a query so it does
    # not affect scoring: restricting to a seller should not reorder their
    # products relative to each other.
    if seller_id:
        body["query"]["bool"]["filter"].append({"term": {"seller_id": seller_id}})

    # If the user provides an empty query, fallback to match_all
    if not query:
        body["query"]["bool"]["must"] = [{"match_all": {}}]

    res = await es.search(index=INDEX_NAME, body=body)

    total_hits = res["hits"]["total"]["value"]
    hits = res["hits"]["hits"]

    # Seller quality for the sellers on this page, and only them. Best effort:
    # a seller we could not resolve ranks as average, which is the same rule
    # cold start applies. A search must not fail because a metrics service is
    # slow.
    quality = await quality_for(h["_source"].get("seller_id") for h in hits)

    candidates = []
    for hit in hits:
        source = hit["_source"]
        # pricing's price if it has published one, catalog's list price
        # otherwise, and None rather than 0 when neither has. See
        # search_rules for why a silent zero is the wrong answer.
        price, price_source = effective_price(source)
        seller_id = source.get("seller_id")
        candidates.append({
            "id": source.get("product_id"),
            # Elasticsearch's text score is the relevance term. Everything
            # else in the formula is a proportion of it (ranking_rules), so
            # nothing can rescue a product the query did not match.
            "relevance": hit.get("_score") or 0.0,
            "quality": quality.get(str(seller_id)) if seller_id else None,
            # Proximity is implemented and tested in ranking_rules and is not
            # wired here: seller coordinates are not in the read model yet, so
            # there is nothing to measure a distance against. Passing None
            # makes the decay exactly 1.0 -- the same as an unlocated seller --
            # rather than silently ordering by something else.
            "distance_km": None,
            "affinity": None,
            "source": source,
            "price": price,
            "price_source": price_source,
        })

    items = []
    for candidate in rank(candidates):
        source = candidate["source"]
        items.append(SearchResultItem(
            product_id=source.get("product_id"),
            name=source.get("name"),
            description=source.get("description"),
            price_cents=candidate["price"],
            price_source=candidate["price_source"],
            seller_id=source.get("seller_id"),
            quantity_available=source.get("quantity_available", 0)
        ))

    return SearchResponse(total_hits=total_hits, items=items)