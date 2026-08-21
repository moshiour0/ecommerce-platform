from fastapi import APIRouter, Depends, Query
from elasticsearch import AsyncElasticsearch
from ..es_client import get_es_client
from ..schemas import SearchResponse
from ..services.search_engine import search_products

router = APIRouter(prefix="/search", tags=["search"])

@router.get("", response_model=SearchResponse)
async def perform_search(
    q: str = Query("", description="Search query string"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Items per page"),
    seller_id: str = Query(None, description="Restrict to one seller's products"),
    # The buyer's position, for the proximity term of the ranking formula
    # (ARCHITECTURE §3g). Optional, and absent means proximity drops out of the
    # scoring entirely rather than defaulting everyone to a notional city
    # centre -- a default location would quietly reorder results for every
    # buyer who declined to share one.
    #
    # Bounds are checked here so a malformed pair is a 422 rather than a
    # silently wrong distance: a longitude of 900 would otherwise sail through
    # haversine and rank the whole catalogue by nonsense.
    lat: float = Query(None, ge=-90, le=90,
                       description="Buyer latitude, for nearest-shop scoring"),
    lon: float = Query(None, ge=-180, le=180,
                       description="Buyer longitude, for nearest-shop scoring"),
    es: AsyncElasticsearch = Depends(get_es_client)
):
    # Half a coordinate is not a location. Taking one and defaulting the other
    # would place the buyer on a meridian they never claimed to be on.
    if (lat is None) != (lon is None):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="lat and lon must be given together; one without the other "
                   "is not a location")

    return await search_products(es, q, page, size, seller_id, lat, lon)
