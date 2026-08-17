import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    MediaLocationResponse, MediaRegisterRequest, MediaResponse,
    ScanResultRequest, UploadUrlResponse,
)
from ..services.media_service import (
    delete_media, get_location, get_media, get_upload_url,
    record_scan_result, register_media, start_scan,
)

router = APIRouter(prefix="/media", tags=["media"])


@router.post("", response_model=MediaResponse, status_code=201)
async def register_endpoint(
    request: MediaRegisterRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    return await register_media(db, request, idempotency_key)


@router.get("/{media_id}", response_model=MediaResponse)
async def get_endpoint(media_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await get_media(db, media_id)


# Metadata is readable at any status; the location is not. Serving the storage
# key for unscanned media is indistinguishable from serving the media.
@router.get("/{media_id}/location", response_model=MediaLocationResponse)
async def location_endpoint(media_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await get_location(db, media_id)


@router.post("/{media_id}/scan", response_model=MediaResponse)
async def start_scan_endpoint(media_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await start_scan(db, media_id)


@router.post("/{media_id}/scan/result", response_model=MediaResponse)
async def scan_result_endpoint(
    media_id: uuid.UUID,
    request: ScanResultRequest,
    db: AsyncSession = Depends(get_db),
):
    return await record_scan_result(db, media_id, request)


@router.delete("/{media_id}", response_model=MediaResponse)
async def delete_endpoint(media_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await delete_media(db, media_id)


# The client PUTs the file to this URL directly. Bytes never pass through this
# service, so its memory profile does not depend on what people upload.
@router.post("/{media_id}/upload-url", response_model=UploadUrlResponse)
async def upload_url_endpoint(media_id: uuid.UUID,
                              db: AsyncSession = Depends(get_db)):
    return await get_upload_url(db, media_id)
