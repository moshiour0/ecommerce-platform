import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    ContractAcceptanceRequest, DocumentSubmissionRequest, ReasonRequest,
    ReviewResultRequest, SellerPermissionResponse, SellerRegisterRequest,
    SellerResponse,
)
from ..services.seller_service import (
    accept_contract, ban_seller, get_permission, get_seller, list_sellers,
    record_review_result, register_seller, reinstate_seller, start_review,
    submit_documents, suspend_seller,
)

router = APIRouter(prefix="/sellers", tags=["sellers"])


@router.post("", response_model=SellerResponse, status_code=201)
async def register_endpoint(
    request: SellerRegisterRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=400,
                            detail="Idempotency-Key header is required")
    return await register_seller(db, request, idempotency_key)


# The review queue is this with ?status=documents_submitted.
@router.get("", response_model=List[SellerResponse])
async def list_endpoint(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    return await list_sellers(db, status, limit)


@router.get("/{seller_id}", response_model=SellerResponse)
async def get_endpoint(seller_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await get_seller(db, seller_id)


# The narrow answer for other services. catalog-service asking whether a
# seller may list should not receive their rejection reason or their address.
@router.get("/{seller_id}/permission", response_model=SellerPermissionResponse)
async def permission_endpoint(seller_id: uuid.UUID,
                              db: AsyncSession = Depends(get_db)):
    return await get_permission(db, seller_id)


@router.post("/{seller_id}/documents", response_model=SellerResponse)
async def documents_endpoint(seller_id: uuid.UUID,
                             request: DocumentSubmissionRequest,
                             db: AsyncSession = Depends(get_db)):
    return await submit_documents(db, seller_id, request)


@router.post("/{seller_id}/review", response_model=SellerResponse)
async def start_review_endpoint(seller_id: uuid.UUID,
                                db: AsyncSession = Depends(get_db)):
    return await start_review(db, seller_id)


@router.post("/{seller_id}/review/result", response_model=SellerResponse)
async def review_result_endpoint(seller_id: uuid.UUID,
                                 request: ReviewResultRequest,
                                 db: AsyncSession = Depends(get_db)):
    return await record_review_result(db, seller_id, request)


# Accepting the contract is what makes a seller live, so it is a seller
# action rather than an administrative one.
@router.post("/{seller_id}/contract", response_model=SellerResponse)
async def contract_endpoint(seller_id: uuid.UUID,
                            request: ContractAcceptanceRequest,
                            db: AsyncSession = Depends(get_db)):
    return await accept_contract(db, seller_id, request)


@router.post("/{seller_id}/suspend", response_model=SellerResponse)
async def suspend_endpoint(seller_id: uuid.UUID, request: ReasonRequest,
                           db: AsyncSession = Depends(get_db)):
    return await suspend_seller(db, seller_id, request)


@router.post("/{seller_id}/reinstate", response_model=SellerResponse)
async def reinstate_endpoint(seller_id: uuid.UUID,
                             db: AsyncSession = Depends(get_db)):
    return await reinstate_seller(db, seller_id)


@router.post("/{seller_id}/ban", response_model=SellerResponse)
async def ban_endpoint(seller_id: uuid.UUID, request: ReasonRequest,
                       db: AsyncSession = Depends(get_db)):
    return await ban_seller(db, seller_id, request)
