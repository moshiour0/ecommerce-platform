from elasticsearch import AsyncElasticsearch
from ..schemas import SearchResponse, SearchResultItem
from .affinity_lookup import affinity_profile
from python_common.affinity_rules import (affinity_for,
                                          profile_from_dict)
from .quality_lookup import quality_for
from .ranking_rules import haversine_km
from python_common.read_model import (has_seller_signals,
                                      quality_from_document)
from .ranking_rules import rank
from .search_rules import effective_price

INDEX_NAME = "products"

async def search_products(es: AsyncElasticsearch, query: str, page: int,
                          size: int, seller_id: str = None,
                          buyer_lat: float = None,
                          buyer_lon: float = None,
                          buyer_id: str = None) -> SearchResponse:
    """Search, scored by ARCHITECTURE 3g's one pipeline.

    `buyer_lat`/`buyer_lon` are optional and default to unknown. A buyer who
    has not shared a location gets distance_km=None for every candidate, which
    makes the decay exactly 1.0 across the board -- proximity drops out of the
    formula rather than defaulting everyone to some notional city centre.
    """
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
                    {"exists": {"field": "sku"}},
                    # A suspended seller's catalogue disappears from search.
                    #
                    # Listing enforcement was creation-only: catalog-service
                    # asked whether a seller could list at the moment a product
                    # was created, and nothing ever asked again. Suspending a
                    # seller left every product they had ever listed both
                    # visible and buyable.
                    #
                    # Written as "not explicitly false" rather than "is true",
                    # and the difference is the whole safety of it. A document
                    # the projection has not reached yet has no
                    # `seller_may_sell` at all, and requiring true would empty
                    # the catalogue the moment this deployed. Suspension hides
                    # a seller only once the platform positively knows they are
                    # suspended; unknown stays visible.
                    {"bool": {"must_not": [
                        {"term": {"seller_may_sell": False}}]}}
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

    # Seller quality now arrives on the documents themselves, written by the
    # projection in stream-processor (ARCHITECTURE 3g). This used to be an
    # HTTP call per distinct seller on the page -- cached for a minute, capped
    # at 25, behind a breaker, every failure resolving to average. Correct, and
    # a round trip inside a search.
    #
    # quality_lookup is kept as the fallback for documents indexed before the
    # projection existed, and only those: a hit that already carries signals
    # costs nothing, and one that does not is resolved the old way rather than
    # ranking as average forever.
    # One fetch for the whole page, because a profile is a fact about the buyer
    # rather than about any result. None for an anonymous search, which scores
    # exactly as it did before personalisation existed.
    profile = profile_from_dict(await affinity_profile(buyer_id))

    unprojected = [h["_source"].get("seller_id") for h in hits
                   if not has_seller_signals(h["_source"])
                   and h["_source"].get("seller_id")]
    fallback_quality = await quality_for(unprojected) if unprojected else {}

    candidates = []
    for hit in hits:
        source = hit["_source"]
        # pricing's price if it has published one, catalog's list price
        # otherwise, and None rather than 0 when neither has. See
        # search_rules for why a silent zero is the wrong answer.
        price, price_source = effective_price(source)
        seller_id = source.get("seller_id")

        if has_seller_signals(source):
            quality = quality_from_document(source)
        else:
            quality = (fallback_quality.get(str(seller_id))
                       if seller_id else None)

        # Proximity, finally measured. distance_km is None whenever either end
        # is unknown -- a buyer who did not share a location, or a shop that
        # has not set one -- and distance_decay returns exactly 1.0 for None.
        # So an incomplete profile is neutral rather than buried, which is the
        # same cold-start rule the rest of the formula follows.
        location = source.get("seller_location") or {}
        distance_km = haversine_km(buyer_lat, buyer_lon,
                                   location.get("lat"), location.get("lon"))

        candidates.append({
            "id": source.get("product_id"),
            # Elasticsearch's text score is the relevance term. Everything
            # else in the formula is a proportion of it (ranking_rules), so
            # nothing can rescue a product the query did not match.
            "relevance": hit.get("_score") or 0.0,
            "quality": quality,
            "distance_km": distance_km,
            # The last term of the formula, finally carrying something.
            # None whenever there is no opinion -- anonymous buyer, too little
            # history, or a product whose category and seller are both unknown
            # -- and None is exactly a 1.0 multiplier.
            "affinity": affinity_for(profile,
                                     source.get("category_id"),
                                     seller_id),
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