from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    AuditAppendRequest, AuditRecordResponse, VerifyResponse,
)
from ..services.audit_service import (
    append_record, get_by_resource, list_records, verify,
)

router = APIRouter(prefix="/audit", tags=["audit"])


# Append only. There is deliberately no PUT, PATCH or DELETE on this router:
# the hash chain makes tampering detectable, and not offering an edit route
# keeps this service from being the thing that does the tampering.
@router.post("/records", response_model=AuditRecordResponse, status_code=201)
async def append_endpoint(
    request: AuditAppendRequest,
    db: AsyncSession = Depends(get_db),
):
    return await append_record(db, request)


@router.get("/records", response_model=List[AuditRecordResponse])
async def list_endpoint(
    resource_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if resource_id:
        return await get_by_resource(db, resource_id, limit)
    return await list_records(db, limit, offset)


@router.get("/verify", response_model=VerifyResponse)
async def verify_endpoint(db: AsyncSession = Depends(get_db)):
    """Re-hash the whole log and report every break.

    Deliberately a normal 200 with `intact: false` rather than an error status
    when the chain is broken: this is a report, and a monitoring system that
    cannot tell "the log is damaged" from "the audit service is down" will end
    up treating both as noise.
    """
    records, breaks = await verify(db)
    return VerifyResponse(
        records_checked=len(records),
        intact=not breaks,
        breaks=[{"index": b.index, "sequence": b.sequence, "reason": b.reason}
                for b in breaks],
    )
