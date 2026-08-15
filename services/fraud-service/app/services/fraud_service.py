import uuid
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from fastapi import HTTPException
from ..models import FraudEvaluation, OutboxMessage, IdempotencyKey
from ..schemas import FraudRequest

logger = logging.getLogger(__name__)

# Hardcoded blocklist for APK signatures
BLOCKED_APK_SIGNATURES = ["0xBADF00D", "0xDEADBEEF"]

async def evaluate_checkout_risk(db: AsyncSession, request: FraudRequest, idempotency_key: str) -> FraudEvaluation:
    # Check idempotency — return cached result on retry (Rule 4)
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        existing_key = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
        )
        idem_record = existing_key.scalar_one_or_none()
        if idem_record and idem_record.result_id:
            cached_result = await db.execute(
                select(FraudEvaluation).where(FraudEvaluation.id == idem_record.result_id)
            )
            cached = cached_result.scalar_one_or_none()
            if cached:
                return cached
        raise HTTPException(status_code=409, detail="Idempotency key already processed but result not found")

    is_blocked = False
    reason = None
    risk_score = 0

    # Safely extract telemetry dict (default to empty dict if None)
    telemetry = request.telemetry or {}

    # 1. Inspect telemetry for compromised devices or bot activity safely
    if telemetry.get("is_rooted_jailbroken"):
        is_blocked = True
        reason = "Device rooted/jailbroken"
        risk_score = 100
    elif telemetry.get("interaction_time_ms", 999) < 50:
        is_blocked = True
        reason = "Zero-click suspicion (impossibly fast interaction)"
        risk_score = 95
    elif telemetry.get("apk_signature") in BLOCKED_APK_SIGNATURES:
        is_blocked = True
        reason = "Suspicious APK signature"
        risk_score = 90

    # Fallbacks for missing edge data
    ip_addr = request.ip_address or "127.0.0.1"
    device_fp = request.device_fingerprint or "unknown"

    # F-4 Fix: Velocity check on user_id + IP + device_fingerprint combination
    if not is_blocked:
        one_minute_ago = datetime.now(timezone.utc) - timedelta(seconds=60)
        recent_requests_query = select(func.count()).select_from(FraudEvaluation).where(
            FraudEvaluation.user_id == request.user_id,
            FraudEvaluation.ip_address == ip_addr,
            FraudEvaluation.device_fingerprint == device_fp,
            FraudEvaluation.created_at >= one_minute_ago
        )
        recent_requests_count = (await db.execute(recent_requests_query)).scalar() or 0

        if recent_requests_count > 5:
            is_blocked = True
            reason = "Velocity limit exceeded (Inventory Hoarding)"
            risk_score = 85
        else:
            # Baseline risk score for legitimate-looking traffic
            risk_score = min(10 + (recent_requests_count * 5), 50)
            reason = "Clean"

    # Save Evaluation
    eval_id = uuid.uuid4()
    evaluation = FraudEvaluation(
        id=eval_id,
        user_id=request.user_id,
        ip_address=ip_addr,
        device_fingerprint=device_fp,
        risk_score=risk_score,
        is_blocked=is_blocked,
        reason=reason,
        created_at=datetime.now(timezone.utc)
    )
    db.add(evaluation)

    # Create OutboxMessage
    payload = {
        "id": str(evaluation.id),
        "user_id": str(evaluation.user_id),
        "risk_score": evaluation.risk_score,
        "is_blocked": evaluation.is_blocked,
        "reason": evaluation.reason,
        "created_at": evaluation.created_at.isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="FraudEvaluation",
        aggregate_id=str(evaluation.user_id),
        type="FraudEvaluated",
        payload=payload
    )
    db.add(outbox_message)

    # Store result_id in idempotency key
    idem_result = await db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
    )
    idem_record = idem_result.scalar_one()
    idem_record.result_id = eval_id

    try:
        await db.commit()
        await db.refresh(evaluation)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return evaluation
