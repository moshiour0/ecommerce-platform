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
    es: AsyncElasticsearch = Depends(get_es_client)
):
    return await search_products(es, q, page, size, seller_id)
