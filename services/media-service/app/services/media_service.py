"""
Media orchestration: database, outbox, and the decisions in media_rules.

Everything about *what* may happen lives in media_rules and is unit tested.
What lives here is the transaction: the status change and its outbox row are
written together, so an event can never describe a state change that did not
commit (Rule 3).
"""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models import IdempotencyKey, MediaAsset, OutboxMessage
from ..schemas import MediaRegisterRequest, MediaResponse, ScanResultRequest
from ..storage import presigned_upload_url, public_url
from .media_rules import (
    Decision, MediaStatus, Outcome, is_confidential, is_publicly_servable,
    is_servable, may_issue_upload_url, plan_delete, plan_registration_for,
    plan_scan_result, plan_scan_start, storage_key,
)

logger = logging.getLogger(__name__)

AGGREGATE = "Media"


def to_response(asset: MediaAsset) -> MediaResponse:
    return MediaResponse(
        id=asset.id,
        owner_id=asset.owner_id,
        filename=asset.filename,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        status=asset.status,
        purpose=asset.purpose,
        servable=is_servable(asset.status),
        publicly_servable=is_publicly_servable(asset.status, asset.purpose),
        scan_detail=asset.scan_detail,
    )


async def _claim_idempotency(db: AsyncSession, key: str) -> bool:
    """True when this key has been seen before."""
    stmt = insert(IdempotencyKey).values(key=key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    return result.rowcount == 0


async def _load(db: AsyncSession, media_id: uuid.UUID) -> MediaAsset:
    result = await db.execute(select(MediaAsset).where(MediaAsset.id == media_id))
    asset = result.scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return asset


def _emit(db: AsyncSession, asset: MediaAsset, event: str) -> None:
    db.add(OutboxMessage(
        aggregate_type=AGGREGATE,
        aggregate_id=str(asset.id),
        type=event,
        payload={
            "media_id": str(asset.id),
            "owner_id": str(asset.owner_id),
            "status": asset.status,
            "purpose": asset.purpose,
            "content_type": asset.content_type,
            "filename": asset.filename,
        },
    ))


def _reject(decision: Decision) -> None:
    """Turn a refused decision into the right HTTP status.

    INVALID is the caller's request being wrong (422). NOT_ALLOWED is a
    legitimate request against a state that forbids it (409) -- the distinction
    matters to the dispatcher, which retries one and not the other.
    """
    if decision.outcome is Outcome.INVALID:
        raise HTTPException(status_code=422, detail=decision.detail)
    raise HTTPException(status_code=409, detail=decision.detail)


async def _apply(db: AsyncSession, asset: MediaAsset, decision: Decision,
                 scan_detail=None) -> MediaResponse:
    """Commit a status change and its event in one transaction."""
    if not decision.ok:
        _reject(decision)

    asset.status = decision.new_status.value
    if scan_detail is not None:
        asset.scan_detail = scan_detail

    if decision.event:
        _emit(db, asset, decision.event)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return to_response(asset)


async def register_media(db: AsyncSession, request: MediaRegisterRequest,
                         idempotency_key: str) -> MediaResponse:
    if await _claim_idempotency(db, idempotency_key):
        # Rule 4: a repeated key returns the original result, never a 409.
        existing = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key))
        row = existing.scalar_one_or_none()
        if row is not None and row.result_id is not None:
            return to_response(await _load(db, row.result_id))
        raise HTTPException(status_code=409,
                            detail="Duplicate request still in flight")

    # The allowed content types depend on what the asset is for: a KYC
    # document may be a PDF and a listing photo may not.
    decision = plan_registration_for(request.purpose, request.filename,
                                     request.content_type, request.size_bytes)
    if not decision.ok:
        await db.rollback()
        _reject(decision)

    asset = MediaAsset(
        owner_id=request.owner_id,
        purpose=request.purpose,
        filename=request.filename,
        content_type=request.content_type,
        size_bytes=request.size_bytes,
        storage_key=request.storage_key,
        checksum=request.checksum,
        status=decision.new_status.value,
    )
    db.add(asset)
    await db.flush()  # assign the id before it goes into the event and the key

    # Bytes go to a key derived from the purpose, so the bucket can carry a
    # different policy per prefix rather than per object. A caller may supply
    # its own key, but the default is the one the storage layout expects.
    if not asset.storage_key:
        asset.storage_key = storage_key(request.purpose, str(asset.owner_id),
                                        str(asset.id))

    _emit(db, asset, decision.event)
    await db.execute(
        IdempotencyKey.__table__.update()
        .where(IdempotencyKey.key == idempotency_key)
        .values(result_id=asset.id)
    )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("media %s registered and quarantined", asset.id)
    return to_response(asset)


async def start_scan(db: AsyncSession, media_id: uuid.UUID) -> MediaResponse:
    asset = await _load(db, media_id)
    return await _apply(db, asset, plan_scan_start(MediaStatus(asset.status)))


async def record_scan_result(db: AsyncSession, media_id: uuid.UUID,
                             request: ScanResultRequest) -> MediaResponse:
    asset = await _load(db, media_id)
    decision = plan_scan_result(MediaStatus(asset.status), request.infected)
    return await _apply(db, asset, decision, scan_detail=request.detail)


async def delete_media(db: AsyncSession, media_id: uuid.UUID) -> MediaResponse:
    asset = await _load(db, media_id)
    return await _apply(db, asset, plan_delete(MediaStatus(asset.status)))


async def get_media(db: AsyncSession, media_id: uuid.UUID) -> MediaResponse:
    return to_response(await _load(db, media_id))


async def get_location(db: AsyncSession, media_id: uuid.UUID):
    """Where the bytes are -- only for media that has been cleared.

    This is the endpoint the default-deny exists for. Metadata is readable at
    any status; the location is not, because handing out the key to unscanned
    media is the same as serving it.
    """
    asset = await _load(db, media_id)

    # Confidential first, and with a 404 rather than a 409. A KYC document or a
    # body photo passing a virus scan does not make it public, and answering
    # "this exists but you may not have it" tells an enumerating caller which
    # ids are real.
    if is_confidential(asset.purpose):
        logger.warning("refused public location for %s asset %s",
                       asset.purpose, media_id)
        raise HTTPException(status_code=404, detail="Media not found")

    if not is_servable(asset.status):
        raise HTTPException(
            status_code=409,
            detail=f"media is not servable (status {asset.status})")
    if not asset.storage_key:
        raise HTTPException(status_code=404, detail="no storage key recorded")
    return {"id": asset.id, "storage_key": asset.storage_key,
            "url": public_url(asset.storage_key)}


async def get_upload_url(db: AsyncSession, media_id: uuid.UUID) -> dict:
    """A pre-signed URL the owner PUTs the file to.

    Bytes go straight from the client to the object store; this service only
    ever sees metadata. Issued once, while the asset is quarantined -- see
    may_issue_upload_url for why re-issuing after a scan would defeat
    quarantine entirely.
    """
    asset = await _load(db, media_id)

    decision = may_issue_upload_url(asset.status, asset.purpose)
    if not decision.ok:
        _reject(decision)

    if not asset.storage_key:
        # Registered before storage keys existed, or with one supplied and then
        # cleared. Derive it now rather than refusing.
        asset.storage_key = storage_key(asset.purpose, str(asset.owner_id),
                                        str(asset.id))
        await db.commit()

    logger.info("issued upload url for %s asset %s", asset.purpose, media_id)
    return presigned_upload_url(asset.storage_key, asset.content_type)
