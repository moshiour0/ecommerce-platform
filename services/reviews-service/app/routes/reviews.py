import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.review_service import (edit_review, list_for_product,
                                       moderate_review, moderation_queue,
                                       my_reviews, product_summary,
                                       report_review, seller_summary,
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


class ReportRequest(BaseModel):
    reason: str
    note: Optional[str] = Field(None, max_length=1000)


class ModerateRequest(BaseModel):
    decision: str          # "remove" or "clear"
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/{review_id}/report", status_code=200)
async def report(review_id: uuid.UUID, request: ReportRequest,
                 reporter_id: str = Depends(require_buyer),
                 db: AsyncSession = Depends(get_db)):
    """Say that a review should not be there.

    Requires identity, and one report per person per review. Both matter: the
    hiding threshold counts *distinct reporters*, so an anonymous or repeatable
    report would let one determined person take down anything.
    """
    return await report_review(db, review_id, reporter_id=reporter_id,
                               reason=request.reason, note=request.note)


@router.get("/mine", status_code=200)
async def mine(buyer_id: str = Depends(require_buyer),
               db: AsyncSession = Depends(get_db)):
    """A buyer's own reviews, including any that have been hidden.

    Safe at this path because nothing else answers GET on a single segment --
    /{review_id} is PATCH only. If a GET /{review_id} is ever added it must be
    declared *after* this one, or "mine" is parsed as a uuid and 422s.
    """
    return await my_reviews(db, buyer_id)


# ---------------------------------------------------------------------------
# moderation
# ---------------------------------------------------------------------------
#
# These are operator endpoints. They are unauthenticated *here* because every
# service on this platform trusts the gateway to have established identity, and
# the gateway does not yet have a role model -- so there is nothing this
# service could check that would mean anything.
#
# That is a real gap and it is named rather than papered over with a header
# nobody verifies: until the gateway can say "this caller is staff", these must
# not be routed publicly. They are reachable only inside the mesh.

@router.get("/moderation/queue", status_code=200)
async def queue(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """Reviews hidden by reports and waiting on a person. Oldest first."""
    return await moderation_queue(db, limit)


@router.post("/moderation/{review_id}", status_code=200)
async def moderate(review_id: uuid.UUID, request: ModerateRequest,
                   db: AsyncSession = Depends(get_db)):
    """Rule on a review: it stays, or it goes.

    No third option. "Hidden indefinitely without deciding" is what the pending
    state already is, and giving it a permanent form lets a queue become a
    graveyard.
    """
    return await moderate_review(db, review_id, decision=request.decision,
                                 note=request.note)
