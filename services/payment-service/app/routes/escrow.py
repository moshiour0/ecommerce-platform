import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    EscrowDeliveryRequest, EscrowSettlementRequest, LedgerTransactionResponse,
    PayoutRequest, SellerBalanceResponse,
)
from ..services.escrow_service import (
    book_delivery, book_payout, book_settlement, ledger_health, seller_balance,
)

router = APIRouter(prefix="/escrow", tags=["escrow"])


# The buyer paid the courier. This is the moment the platform starts owing a
# seller money, so it is the moment the liability is booked -- not the payout.
@router.post("/delivery", response_model=LedgerTransactionResponse)
async def delivery_endpoint(request: EscrowDeliveryRequest,
                            db: AsyncSession = Depends(get_db)):
    return await book_delivery(db, request)


@router.post("/settlement", response_model=LedgerTransactionResponse)
async def settlement_endpoint(request: EscrowSettlementRequest,
                              db: AsyncSession = Depends(get_db)):
    return await book_settlement(db, request)


@router.post("/payout", response_model=LedgerTransactionResponse)
async def payout_endpoint(request: PayoutRequest,
                          db: AsyncSession = Depends(get_db)):
    return await book_payout(db, request)


@router.get("/sellers/{seller_id}/balance", response_model=SellerBalanceResponse)
async def balance_endpoint(seller_id: uuid.UUID,
                           db: AsyncSession = Depends(get_db)):
    return await seller_balance(db, seller_id)


# Does the whole ledger balance? A non-zero imbalance means a transaction was
# stored unbalanced and the books are wrong by exactly that amount.
@router.get("/health")
async def ledger_health_endpoint(db: AsyncSession = Depends(get_db)):
    return await ledger_health(db)
