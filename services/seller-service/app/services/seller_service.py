"""
Seller orchestration: database, outbox, and the decisions in seller_rules.

Everything about *what* may happen lives in seller_rules and is unit tested.
What lives here is the transaction: the status change and its outbox row are
written together, so an event can never describe a state change that did not
commit (Rule 3).
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models import IdempotencyKey, OutboxMessage, Seller, SellerDocument
from ..schemas import (
    ContractAcceptanceRequest, DocumentSubmissionRequest, ReasonRequest,
    ReviewResultRequest, SellerRegisterRequest, SellerResponse,
)
from .seller_rules import (
    CURRENT_CONTRACT_VERSION, commission_bps, Decision, Outcome, SellerStatus,
    may_list_products, may_receive_orders, missing_documents,
    needs_contract_acceptance, plan_ban, plan_contract_acceptance,
    plan_document_submission, plan_registration, plan_reinstatement,
    plan_review_result, plan_review_start, plan_suspension,
)

logger = logging.getLogger(__name__)

AGGREGATE = "Seller"


def to_response(seller: Seller, submitted_types=None) -> SellerResponse:
    return SellerResponse(
        id=seller.id,
        legal_name=seller.legal_name,
        display_name=seller.display_name,
        contact_email=seller.contact_email,
        contact_phone=seller.contact_phone,
        status=seller.status,
        status_reason=seller.status_reason,
        accepted_contract_version=seller.accepted_contract_version,
        current_contract_version=CURRENT_CONTRACT_VERSION,
        may_list_products=may_list_products(seller.status,
                                            seller.accepted_contract_version),
        may_receive_orders=may_receive_orders(seller.status,
                                              seller.accepted_contract_version),
        needs_contract_acceptance=needs_contract_acceptance(
            seller.status, seller.accepted_contract_version),
        missing_documents=sorted(
            d.value for d in missing_documents(submitted_types or [])),
        city=seller.city,
        district=seller.district,
        country=seller.country,
        created_at=seller.created_at,
    )


async def _claim_idempotency(db: AsyncSession, key: str) -> bool:
    """True when this key has been seen before."""
    stmt = insert(IdempotencyKey).values(key=key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    return result.rowcount == 0


async def _load(db: AsyncSession, seller_id: uuid.UUID) -> Seller:
    result = await db.execute(select(Seller).where(Seller.id == seller_id))
    seller = result.scalar_one_or_none()
    if seller is None:
        raise HTTPException(status_code=404, detail="Seller not found")
    return seller


async def _document_types(db: AsyncSession, seller_id: uuid.UUID) -> list:
    result = await db.execute(
        select(SellerDocument.document_type)
        .where(SellerDocument.seller_id == seller_id))
    return [row[0] for row in result.all()]


def _emit(db: AsyncSession, seller: Seller, event: str) -> None:
    """One outbox row per transition.

    The payload is deliberately small and free of anything from inside a KYC
    document. These events go to Kafka, where they are retained and read by
    consumers this service does not know about; a national ID number in an
    event payload is that number in every consumer's storage too.
    """
    db.add(OutboxMessage(
        aggregate_type=AGGREGATE,
        aggregate_id=str(seller.id),
        type=event,
        payload={
            "seller_id": str(seller.id),
            "display_name": seller.display_name,
            "status": seller.status,
            "status_reason": seller.status_reason,
            "accepted_contract_version": seller.accepted_contract_version,
            "city": seller.city,
            "district": seller.district,
            "country": seller.country,
            # Denormalised on purpose: a consumer deciding whether to accept a
            # listing should not have to re-derive the rule, and re-deriving is
            # how two services end up disagreeing about who may sell.
            "may_list_products": may_list_products(
                seller.status, seller.accepted_contract_version),
        },
    ))


def _reject(decision: Decision) -> None:
    """Turn a refused decision into the right HTTP status.

    INVALID is the caller's request being wrong (422). NOT_ALLOWED is a
    legitimate request against a state that forbids it (409) -- the
    distinction matters to any caller deciding whether a retry could ever
    succeed.
    """
    if decision.outcome is Outcome.INVALID:
        raise HTTPException(status_code=422, detail=decision.detail)
    raise HTTPException(status_code=409, detail=decision.detail)


async def _apply(db: AsyncSession, seller: Seller, decision: Decision,
                 reason: str = None) -> SellerResponse:
    """Commit a status change and its event in one transaction."""
    if not decision.ok:
        _reject(decision)

    seller.status = decision.new_status.value
    if reason is not None:
        seller.status_reason = reason

    if decision.event:
        _emit(db, seller, decision.event)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("seller %s -> %s", seller.id, seller.status)
    return to_response(seller, await _document_types(db, seller.id))


async def register_seller(db: AsyncSession, request: SellerRegisterRequest,
                          idempotency_key: str) -> SellerResponse:
    if await _claim_idempotency(db, idempotency_key):
        # Rule 4: a repeated key returns the original result, never a 409.
        existing = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key))
        row = existing.scalar_one_or_none()
        if row is not None and row.result_id is not None:
            return to_response(await _load(db, row.result_id),
                               await _document_types(db, row.result_id))
        raise HTTPException(status_code=409,
                            detail="Duplicate request still in flight")

    decision = plan_registration(request.legal_name, request.display_name,
                                 request.contact_email)
    if not decision.ok:
        await db.rollback()
        _reject(decision)

    seller = Seller(
        legal_name=request.legal_name.strip(),
        display_name=request.display_name.strip(),
        contact_email=request.contact_email.strip(),
        contact_phone=request.contact_phone,
        status=decision.new_status.value,
        address_line=request.address_line,
        city=request.city,
        district=request.district,
        country=request.country.upper(),
        latitude=request.latitude,
        longitude=request.longitude,
    )
    db.add(seller)
    await db.flush()  # assign the id before it goes into the event and the key

    _emit(db, seller, decision.event)
    await db.execute(
        IdempotencyKey.__table__.update()
        .where(IdempotencyKey.key == idempotency_key)
        .values(result_id=seller.id)
    )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("seller %s registered (%s)", seller.id, seller.display_name)
    return to_response(seller, [])


async def submit_documents(db: AsyncSession, seller_id: uuid.UUID,
                           request: DocumentSubmissionRequest) -> SellerResponse:
    """Attach KYC documents and, if the set is complete, join the queue.

    Documents are replaced by type rather than appended, so "which trade
    licence did the reviewer look at" always has exactly one answer.
    """
    seller = await _load(db, seller_id)

    incoming = {d.document_type: d.media_id for d in request.documents}

    # Decide before writing. Attaching documents to a banned seller and only
    # then refusing the transition would leave their records in the database.
    existing = set(await _document_types(db, seller_id))
    resulting = sorted(existing | set(incoming))
    decision = plan_document_submission(seller.status, resulting)
    if not decision.ok:
        await db.rollback()
        _reject(decision)

    for document_type, media_id in incoming.items():
        await db.execute(
            delete(SellerDocument)
            .where(SellerDocument.seller_id == seller_id)
            .where(SellerDocument.document_type == document_type))
        db.add(SellerDocument(seller_id=seller_id,
                              document_type=document_type,
                              media_id=media_id))

    # A resubmission clears the previous rejection: leaving it would show the
    # seller last week's "trade licence is expired" next to the licence they
    # just uploaded.
    return await _apply(db, seller, decision, reason="")


async def start_review(db: AsyncSession, seller_id: uuid.UUID) -> SellerResponse:
    seller = await _load(db, seller_id)
    return await _apply(db, seller, plan_review_start(seller.status))


async def record_review_result(db: AsyncSession, seller_id: uuid.UUID,
                               request: ReviewResultRequest) -> SellerResponse:
    seller = await _load(db, seller_id)
    decision = plan_review_result(seller.status, request.approved,
                                  request.reason or "")
    # The reason is the reviewer's text on a rejection and nothing on an
    # approval -- an approved seller showing a stale rejection message is a
    # support ticket.
    return await _apply(db, seller, decision,
                        reason=decision.detail if not request.approved else None)


async def accept_contract(db: AsyncSession, seller_id: uuid.UUID,
                          request: ContractAcceptanceRequest) -> SellerResponse:
    seller = await _load(db, seller_id)
    decision = plan_contract_acceptance(seller.status, request.version)
    if not decision.ok:
        _reject(decision)

    seller.accepted_contract_version = request.version
    seller.contract_accepted_at = datetime.now(timezone.utc)
    return await _apply(db, seller, decision, reason=None)


async def suspend_seller(db: AsyncSession, seller_id: uuid.UUID,
                         request: ReasonRequest) -> SellerResponse:
    seller = await _load(db, seller_id)
    decision = plan_suspension(seller.status, request.reason)
    return await _apply(db, seller, decision, reason=decision.detail)


async def reinstate_seller(db: AsyncSession, seller_id: uuid.UUID) -> SellerResponse:
    seller = await _load(db, seller_id)
    return await _apply(db, seller, plan_reinstatement(seller.status), reason="")


async def ban_seller(db: AsyncSession, seller_id: uuid.UUID,
                     request: ReasonRequest) -> SellerResponse:
    seller = await _load(db, seller_id)
    decision = plan_ban(seller.status, request.reason)
    return await _apply(db, seller, decision, reason=decision.detail)


async def get_seller(db: AsyncSession, seller_id: uuid.UUID) -> SellerResponse:
    seller = await _load(db, seller_id)
    return to_response(seller, await _document_types(db, seller_id))


async def get_permission(db: AsyncSession, seller_id: uuid.UUID) -> dict:
    """The narrow answer other services need.

    Deliberately separate from the full record. catalog-service asking "may
    this seller list?" should not receive their rejection reason or their
    address, and a small response is a small blast radius.

    A seller that does not exist answers 404 rather than a polite `false`:
    "this id is not a seller" and "this seller may not sell" are different
    facts, and a caller that cannot tell them apart will treat a typo'd id as
    a suspended shop.
    """
    seller = await _load(db, seller_id)
    return {
        "seller_id": seller.id,
        "status": seller.status,
        "may_list_products": may_list_products(
            seller.status, seller.accepted_contract_version),
        "may_receive_orders": may_receive_orders(
            seller.status, seller.accepted_contract_version),
    }


async def list_sellers(db: AsyncSession, status: str = None, limit: int = 50):
    """Sellers, optionally by status.

    The review queue is this with status=documents_submitted, which is why the
    status column is indexed. Capped rather than paginated for now: an
    unbounded list of every seller on the platform is the query that gets
    written once and then runs forever.
    """
    query = select(Seller).order_by(Seller.created_at.desc()).limit(min(limit, 200))
    if status:
        if status not in {s.value for s in SellerStatus}:
            raise HTTPException(status_code=422,
                                detail=f"unknown status {status!r}")
        query = query.where(Seller.status == status)

    result = await db.execute(query)
    sellers = result.scalars().all()
    if not sellers:
        return []

    # One query for every seller's document types, not one per seller. The
    # review queue is the hottest caller of this endpoint and it is exactly
    # the place an N+1 hides: fifty sellers reads fine and behaves like fifty
    # round trips.
    ids = [s.id for s in sellers]
    rows = await db.execute(
        select(SellerDocument.seller_id, SellerDocument.document_type)
        .where(SellerDocument.seller_id.in_(ids)))
    by_seller = {}
    for seller_id, document_type in rows.all():
        by_seller.setdefault(seller_id, []).append(document_type)

    return [to_response(s, by_seller.get(s.id, [])) for s in sellers]


async def get_commission(db: AsyncSession, seller_id: uuid.UUID) -> dict:
    """The commission rate this seller actually accepted.

    Read by payment-service when it books escrow at delivery. The rate comes
    from the seller's *accepted* contract version rather than the current one,
    so raising the platform's rate does not silently reprice orders placed by
    sellers who never agreed to it -- they have to accept the new version
    first, and may_list_products already stops them selling until they do.

    A seller who has accepted nothing has no rate, and that is reported rather
    than defaulted: booking a commission against a contract nobody signed is
    exactly the kind of number that survives until a seller disputes it.
    """
    seller = await _load(db, seller_id)
    version = seller.accepted_contract_version
    rate = commission_bps(version)

    if rate is None:
        raise HTTPException(
            status_code=409,
            detail=f"seller {seller_id} has no commission rate: accepted "
                   f"contract version is {version!r}. Nothing may be booked "
                   f"against a contract that cannot be produced.")

    return {
        "seller_id": seller.id,
        "accepted_contract_version": version,
        "current_contract_version": CURRENT_CONTRACT_VERSION,
        "commission_bps": rate,
    }
