from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import NotificationRequest, NotificationResponse
from ..services.notification_service import schedule_notification

router = APIRouter(prefix="/notifications", tags=["notifications"])

@router.post("", response_model=NotificationResponse, status_code=201)
async def create_notification_endpoint(
    request: NotificationRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await schedule_notification(db, request, idempotency_key)
