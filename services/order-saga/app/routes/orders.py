from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import (
    CreateOrderRequest, SagaEventRequest, SagaResponse,
    SellerOrderActionRequest,
)
from ..services.saga_orchestrator import (
    advance_saga, get_seller_orders, start_saga, transition_seller_order,
)
import uuid

router = APIRouter(prefix="/orders", tags=["orders"])

@router.post("", response_model=SagaResponse, status_code=201)
async def create_order_endpoint(
    request: CreateOrderRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await start_saga(db, request, idempotency_key)

@router.post("/{order_id}/events", response_model=SagaResponse, status_code=200)
async def process_saga_event_endpoint(
    order_id: str,
    request: SagaEventRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    try:
        uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid order_id format")
        
    import sys
    print(f"RECEIVED EVENT: {request.event_type} FOR {order_id}", file=sys.stderr, flush=True)
    return await advance_saga(db, order_id, request, idempotency_key)


# The per-seller breakdown, and the buyer-facing status derived from it.
@router.get("/{order_id}/seller-orders", status_code=200)
async def seller_orders_endpoint(order_id: uuid.UUID,
                                 db: AsyncSession = Depends(get_db)):
    return await get_seller_orders(db, order_id)


# The COD lifecycle, one action per step (ARCHITECTURE_STATE_FINAL.md §3d).
#
# One endpoint per action rather than a PATCH that takes a target status. A
# caller that can name the destination can name any destination, and the guard
# then lives in whatever validates the field; a named action cannot ask for a
# transition the table does not contain.
@router.post("/{order_id}/seller-orders/{seller_order_id}/{action}",
             status_code=200)
async def seller_order_action_endpoint(
    order_id: uuid.UUID,
    seller_order_id: uuid.UUID,
    action: str,
    request: SellerOrderActionRequest = SellerOrderActionRequest(),
    db: AsyncSession = Depends(get_db),
):
    return await transition_seller_order(db, order_id, seller_order_id,
                                         action, request)
