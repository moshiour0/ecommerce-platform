import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.review_service import (edit_review, list_for_product,
                                       product_summary, seller_summary,
                                       submit_review)

router = APIRouter(prefix="/reviews", tags=["reviews"])


class SubmitRequest(BaseModel):
    seller_order_id: uuid.UUID
    product_id: uuid.UUID
    # Not constrained to 1..5 here on purpose. review_rules.validate_rating
    # owns the scale, and pydantic enforcing it too would mean two places to
    # change and one of them silently winning.
    product_rating: int
    seller_rating: Optional[int] = None
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = None


class EditRequest(BaseModel):
    product_rating: Optional[int] = None
    seller_rating: Optional[int] = None
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = None


def require_buyer(x_user_id: Optional[str] = Header(None)) -> str:
    """Who this is, from the gateway's verified token and nothing else.

    The gateway strips any inbound x-user-id and sets it from the JWT, the same
    way it does for x-seller-id (see api-gateway). A buyer id taken from the
    body would let anyone review as anyone.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=401,
            detail="no buyer identity on this request; the gateway supplies "
                   "x-user-id from the verified token")
    return x_user_id


@router.post("", status_code=201)
async def submit(request: SubmitRequest,
                 buyer_id: str = Depends(require_buyer),
                 idempotency_key: Optional[str] = Header(
                     None, alias="Idempotency-Key"),
                 db: AsyncSession = Depends(get_db)):
    return await submit_review(
        db, buyer_id=buyer_id, seller_order_id=request.seller_order_id,
        product_id=request.product_id,
        product_rating=request.product_rating,
        seller_rating=request.seller_rating,
        title=request.title, body=request.body,
        idempotency_key=idempotency_key)


@router.patch("/{review_id}", status_code=200)
async def edit(review_id: uuid.UUID, request: EditRequest,
               buyer_id: str = Depends(require_buyer),
               db: AsyncSession = Depends(get_db)):
    return await edit_review(
        db, review_id, buyer_id=buyer_id,
        product_rating=request.product_rating,
        seller_rating=request.seller_rating,
        title=request.title, body=request.body)


# Summaries are public: a product page shows them to anyone, and requiring
# identity to read a rating would make the catalogue useless to a visitor.
@router.get("/products/{product_id}/summary", status_code=200)
async def product_summary_endpoint(product_id: uuid.UUID,
                                   db: AsyncSession = Depends(get_db)):
    return await product_summary(db, product_id)


@router.get("/products/{product_id}", status_code=200)
async def product_reviews_endpoint(product_id: uuid.UUID, limit: int = 20,
                                   db: AsyncSession = Depends(get_db)):
    return await list_for_product(db, product_id, limit)


# Ranking's half of the contract. Returns rating and review_count in exactly
# the shape seller_metrics.quality_inputs takes.
@router.get("/sellers/{seller_id}/summary", status_code=200)
async def seller_summary_endpoint(seller_id: uuid.UUID,
                                  db: AsyncSession = Depends(get_db)):
    return await seller_summary(db, seller_id)
