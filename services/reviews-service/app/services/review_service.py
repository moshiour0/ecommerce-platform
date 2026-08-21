"""
Writing and reading reviews. Persistence, ordering and the outbox.

Every decision lives in review_rules; this module fetches, applies and commits.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import IdempotencyKey, OutboxMessage, Review
from .order_client import fetch_purchase
from .review_rules import (Aggregate, aggregate_ratings, distribution,
                           may_edit, may_review, quality_signal,
                           validate_rating)

logger = logging.getLogger(__name__)

EVENT_REVIEW_PUBLISHED = "ReviewPublished"
EVENT_REVIEW_UPDATED = "ReviewUpdated"


def _now():
    return datetime.now(timezone.utc)


def _view(review: Review) -> dict:
    return {
        "id": str(review.id),
        "seller_order_id": str(review.seller_order_id),
        "product_id": str(review.product_id),
        "seller_id": str(review.seller_id),
        "buyer_id": str(review.buyer_id),
        "product_rating": review.product_rating,
        "seller_rating": review.seller_rating,
        "title": review.title,
        "body": review.body,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


def _event_payload(review: Review) -> dict:
    """What the projection needs to update a read model.

    Carries seller_id and product_id so a consumer can recompute both
    aggregates without calling back for context.
    """
    return {
        "review_id": str(review.id),
        "product_id": str(review.product_id),
        "seller_id": str(review.seller_id),
        "seller_order_id": str(review.seller_order_id),
        "product_rating": review.product_rating,
        "seller_rating": review.seller_rating,
    }


async def submit_review(db: AsyncSession, *, buyer_id, seller_order_id,
                        product_id, product_rating, seller_rating=None,
                        title=None, body=None, idempotency_key=None) -> dict:
    """Verify the purchase, then record the verdict.

    The order is deliberate: nothing is written, and no rating is even
    validated, until the purchase is established. A caller who never bought the
    thing should not be able to tell a malformed rating from a refused one.
    """
    if idempotency_key:
        existing = await db.get(IdempotencyKey, idempotency_key)
        if existing is not None:
            if existing.result_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="a submission with this key is still in flight")
            stored = await db.get(Review, existing.result_id)
            if stored is not None:
                return _view(stored)

    purchase = await fetch_purchase(seller_order_id)

    already = await db.execute(
        select(func.count(Review.id))
        .where(Review.seller_order_id == uuid.UUID(str(seller_order_id)))
        .where(Review.product_id == uuid.UUID(str(product_id))))

    eligibility = may_review(
        buyer_id=buyer_id,
        order_buyer_id=purchase["buyer_id"],
        seller_order_status=purchase["status"],
        product_ids_in_order=purchase["product_ids"],
        product_id=product_id,
        delivered_at=_parse(purchase.get("delivered_at")),
        now=_now(),
        already_reviewed=(already.scalar() or 0) > 0,
    )
    if not eligibility.allowed:
        raise HTTPException(status_code=eligibility.http_status,
                            detail=eligibility.detail)

    try:
        product_rating = validate_rating(product_rating, "product_rating")
        if seller_rating is not None:
            seller_rating = validate_rating(seller_rating, "seller_rating")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    review = Review(
        id=uuid.uuid4(),
        seller_order_id=uuid.UUID(str(seller_order_id)),
        order_id=uuid.UUID(str(purchase["order_id"])),
        product_id=uuid.UUID(str(product_id)),
        seller_id=uuid.UUID(str(purchase["seller_id"])),
        buyer_id=uuid.UUID(str(buyer_id)),
        product_rating=product_rating,
        seller_rating=seller_rating,
        title=title,
        body=body,
    )
    db.add(review)
    db.add(OutboxMessage(
        aggregate_type="Review", aggregate_id=str(review.id),
        type=EVENT_REVIEW_PUBLISHED, payload=_event_payload(review)))

    if idempotency_key:
        db.add(IdempotencyKey(key=idempotency_key, result_id=review.id))

    try:
        await db.commit()
    except IntegrityError:
        # The unique constraint caught a race the count above could not: two
        # submissions for one purchase, both passing the check before either
        # committed. The database is the only place that can decide this.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="this purchase has already been reviewed") from None

    await db.refresh(review)
    logger.info(f"Review {review.id} published for product {product_id} "
                f"by {buyer_id}")
    return _view(review)


async def edit_review(db: AsyncSession, review_id, *, buyer_id,
                      product_rating=None, seller_rating=None,
                      title=None, body=None) -> dict:
    """Revise a review, within the edit window and only by its author."""
    review = await db.get(Review, uuid.UUID(str(review_id)))
    if review is None:
        raise HTTPException(status_code=404, detail="no such review")

    # 404 rather than 403: a caller must not be able to confirm that a review
    # id exists by watching the status change.
    if str(review.buyer_id) != str(buyer_id):
        raise HTTPException(status_code=404, detail="no such review")

    if not may_edit(authored_at=review.created_at, now=_now()):
        raise HTTPException(
            status_code=422,
            detail="the edit window for this review has closed")

    try:
        if product_rating is not None:
            review.product_rating = validate_rating(product_rating,
                                                    "product_rating")
        if seller_rating is not None:
            review.seller_rating = validate_rating(seller_rating,
                                                   "seller_rating")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if title is not None:
        review.title = title
    if body is not None:
        review.body = body
    review.updated_at = _now()

    db.add(OutboxMessage(
        aggregate_type="Review", aggregate_id=str(review.id),
        type=EVENT_REVIEW_UPDATED, payload=_event_payload(review)))

    await db.commit()
    await db.refresh(review)
    return _view(review)


async def product_summary(db: AsyncSession, product_id) -> dict:
    """What buyers see on a product page."""
    result = await db.execute(
        select(Review.product_rating)
        .where(Review.product_id == uuid.UUID(str(product_id))))
    ratings = [row[0] for row in result.all()]
    summary = aggregate_ratings(ratings)
    return {
        "product_id": str(product_id),
        "average_rating": summary.average,
        "review_count": summary.count,
        "distribution": distribution(ratings),
    }


async def seller_summary(db: AsyncSession, seller_id) -> dict:
    """What ranking consumes, in the shape quality_inputs expects.

    Reported raw. ranking_rules.rating_component does the shrinking, and doing
    it here as well would pull a small sample toward neutral twice -- see
    aggregate_ratings.
    """
    result = await db.execute(
        select(Review.seller_rating)
        .where(Review.seller_id == uuid.UUID(str(seller_id))))
    ratings = [row[0] for row in result.all()]
    summary = aggregate_ratings(ratings)
    return {"seller_id": str(seller_id), **quality_signal(summary),
            "distribution": distribution(ratings)}


async def list_for_product(db: AsyncSession, product_id, limit: int = 20):
    result = await db.execute(
        select(Review)
        .where(Review.product_id == uuid.UUID(str(product_id)))
        .order_by(Review.created_at.desc())
        .limit(min(limit, 100)))
    return [_view(r) for r in result.scalars().all()]


def _parse(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp compared against an aware `now` raises rather than
    # answering, which would deny a legitimate review for a formatting reason.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
