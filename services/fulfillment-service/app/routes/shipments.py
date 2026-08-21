import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    CallbackResponse, CourierCallbackRequest, SettlementRequest,
    ShipmentCreateRequest, ShipmentResponse,
)
from ..services import courier_registry
from ..services.shipment_service import (
    create_shipment, handle_callback, ingest_settlement,
)

router = APIRouter(tags=["shipments"])


@router.get("/couriers")
def couriers_endpoint():
    """Which couriers are configured.

    Exposed because "unknown courier" is the first thing anybody hits, and the
    answer is a file on disk rather than anything in this service's code.
    """
    return {"couriers": courier_registry.known()}


@router.post("/shipments", response_model=ShipmentResponse, status_code=201)
async def create_shipment_endpoint(request: ShipmentCreateRequest,
                                   db: AsyncSession = Depends(get_db)):
    return await create_shipment(db, request)


# Where a courier pushes status. The provider is in the path so one URL per
# courier can be handed out, and the body carries that courier's own word --
# translating it is this service's job and nobody else's.
@router.post("/couriers/{provider}/callback", response_model=CallbackResponse)
async def callback_endpoint(provider: str, request: CourierCallbackRequest,
                            db: AsyncSession = Depends(get_db)):
    return await handle_callback(db, provider, request)


# The remittance file. Nothing here moves money: it classifies every row and
# stores the result, including the rows that did not reconcile.
@router.post("/settlements", status_code=200)
async def settlement_endpoint(request: SettlementRequest,
                              db: AsyncSession = Depends(get_db)):
    return await ingest_settlement(db, request)
