from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import (
    InventoryResponse, ReleaseRequest, ReleaseResponse, ReserveRequest,
)
from ..services.inventory_service import release_inventory, reserve_inventory

router = APIRouter(prefix="/inventory", tags=["inventory"])

@router.post("/reserve", response_model=InventoryResponse, status_code=200)
async def reserve_inventory_endpoint(
    request: ReserveRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await reserve_inventory(db, request, idempotency_key)


# The inverse of /reserve (§5). Answers 200 even when the order holds nothing:
# the reaper issues this blind, because at INVENTORY_RESERVED it cannot know
# whether the reservation landed before the acknowledgement dropped, and a
# compensation that errors on "nothing to release" strands the saga forever.
@router.post("/release", response_model=ReleaseResponse, status_code=200)
async def release_endpoint(
    request: ReleaseRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    return await release_inventory(db, request, idempotency_key)
