"""
Audit orchestration: appending to the chain, and checking it.

The interesting part is that appending is not concurrency-safe by nature. Two
requests arriving together both read the same tail record, both compute
sequence = n + 1 from it, and both link to the same prev_hash. One of two things
then happens: the chain forks, or the second write loses and a record vanishes.
Either way the log is no longer evidence.

Two independent defences, because this is the kind of thing that must not
depend on one of them being remembered:

  1. an advisory lock held for the transaction, so appends serialize;
  2. UNIQUE on sequence and on record_hash, so if the lock is ever removed or
     bypassed the database still refuses the second writer.

The lock is transaction-scoped (pg_advisory_xact_lock), so it is released by
commit or rollback and cannot be leaked by a request that dies mid-flight. That
is the same reasoning as the compare-and-delete in the cart checkout lock: a
lock that outlives its owner is worse than no lock.
"""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models import AuditRecord, OutboxMessage
from ..schemas import AuditAppendRequest
from .audit_rules import GENESIS_HASH, link, verify_chain

logger = logging.getLogger(__name__)

# A fixed key for the single append lock. Any constant works as long as nothing
# else in the platform picks the same one; this is "AUDIT" in hex.
CHAIN_LOCK_KEY = 0x41554449_54

EVENT_RECORDED = "AuditRecordAppended"


def to_dict(record: AuditRecord) -> dict:
    """The record as audit_rules sees it."""
    return {
        "sequence": record.sequence,
        "actor": record.actor,
        "action": record.action,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "recorded_at": record.recorded_at.isoformat() if record.recorded_at else None,
        "payload": record.payload,
        "prev_hash": record.prev_hash,
        "record_hash": record.record_hash,
    }


async def append_record(db: AsyncSession, request: AuditAppendRequest) -> AuditRecord:
    # Serialize appends. Transaction-scoped, so it releases on commit or
    # rollback without a finally block to forget.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": CHAIN_LOCK_KEY})

    tail = (await db.execute(
        select(AuditRecord).order_by(AuditRecord.sequence.desc()).limit(1)
    )).scalar_one_or_none()

    next_sequence = 1 if tail is None else tail.sequence + 1
    prev_hash = GENESIS_HASH if tail is None else tail.record_hash

    recorded_at = request.recorded_at or datetime.now(timezone.utc)

    fields = {
        "sequence": next_sequence,
        "actor": request.actor,
        "action": request.action,
        "resource_type": request.resource_type,
        "resource_id": request.resource_id,
        "recorded_at": recorded_at.isoformat(),
        "payload": request.payload,
    }
    hashes = link(prev_hash, fields)

    record = AuditRecord(
        sequence=next_sequence,
        actor=request.actor,
        action=request.action,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        payload=request.payload,
        recorded_at=recorded_at,
        prev_hash=hashes["prev_hash"],
        record_hash=hashes["record_hash"],
    )
    db.add(record)

    db.add(OutboxMessage(
        aggregate_type="Audit",
        aggregate_id=str(next_sequence),
        type=EVENT_RECORDED,
        payload={
            "sequence": next_sequence,
            "actor": request.actor,
            "action": request.action,
            "resource_type": request.resource_type,
            "resource_id": request.resource_id,
            "record_hash": hashes["record_hash"],
        },
    ))

    try:
        await db.commit()
    except IntegrityError:
        # The UNIQUE backstop fired, which means two writers got past the lock.
        # A conflict is the correct outcome and the caller should retry; what
        # must never happen is both rows landing.
        await db.rollback()
        logger.warning("audit append conflicted at sequence %s", next_sequence)
        raise HTTPException(
            status_code=409,
            detail="concurrent append conflicted; retry")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return record


async def list_records(db: AsyncSession, limit: int = 100, offset: int = 0):
    result = await db.execute(
        select(AuditRecord).order_by(AuditRecord.sequence).offset(offset).limit(limit))
    return list(result.scalars().all())


async def get_by_resource(db: AsyncSession, resource_id: str, limit: int = 100):
    result = await db.execute(
        select(AuditRecord)
        .where(AuditRecord.resource_id == resource_id)
        .order_by(AuditRecord.sequence)
        .limit(limit))
    return list(result.scalars().all())


async def verify(db: AsyncSession):
    """Re-hash the whole log in sequence order and report every break.

    Reads in sequence order rather than insertion order on purpose: a chain
    verified in the order rows happen to come back would fail whenever the
    planner chose a different scan.
    """
    result = await db.execute(select(AuditRecord).order_by(AuditRecord.sequence))
    records = [to_dict(r) for r in result.scalars().all()]
    breaks = verify_chain(records)
    return records, breaks
