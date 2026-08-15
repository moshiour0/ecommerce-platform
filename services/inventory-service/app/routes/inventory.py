from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import ReserveRequest, InventoryResponse
from ..services.inventory_service import reserve_inventory

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
