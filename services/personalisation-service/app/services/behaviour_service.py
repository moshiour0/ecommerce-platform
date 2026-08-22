"""
Recording what a buyer did, and answering what they seem to like.

Every decision lives in affinity_rules; this reads, writes and commits.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BehaviourEventRow
# Shared, because search-service scores with the same rules that this
# service builds profiles with. Two copies of the affinity arithmetic in
# two services is how the producer and the consumer stop agreeing.
from python_common.affinity_rules import (EVENT_WEIGHTS, BehaviourEvent,
                                          build_profile, profile_as_dict,
                                          retention_cutoff)

logger = logging.getLogger(__name__)

# How much history to read when building a profile.
#
# Bounded because the query is on the path of every personalised search, and a
# buyer with years of history has a long tail that cannot change an ordering --
# the decay has already reduced it to noise. Newest first, so the cap keeps the
# events that matter rather than an arbitrary slice.
PROFILE_EVENT_LIMIT = 500


def _now():
    return datetime.now(timezone.utc)


async def record(db: AsyncSession, *, buyer_id, kind, product_id=None,
                 category_id=None, seller_id=None) -> dict:
    """Record one interaction.

    An unknown kind is refused rather than stored. Storing it would put a row
    in the table that affinity_rules silently skips forever -- data that looks
    like signal, costs storage, and contributes nothing.
    """
    if kind not in EVENT_WEIGHTS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown behaviour kind {kind!r}; expected one of "
                   f"{sorted(EVENT_WEIGHTS)}")

    row = BehaviourEventRow(
        id=uuid.uuid4(),
        buyer_id=uuid.UUID(str(buyer_id)),
        kind=kind,
        product_id=uuid.UUID(str(product_id)) if product_id else None,
        category_id=uuid.UUID(str(category_id)) if category_id else None,
        seller_id=uuid.UUID(str(seller_id)) if seller_id else None,
        occurred_at=_now(),
    )
    db.add(row)
    await db.commit()
    return {"recorded": True, "kind": kind}


async def affinity_profile(db: AsyncSession, buyer_id) -> dict:
    """What this buyer appears to be interested in.

    Returns normalised weights and nothing else. The events behind them stay in
    this service: a ranking needs to know that someone leans towards a
    category, not what they looked at on Tuesday.
    """
    result = await db.execute(
        select(BehaviourEventRow)
        .where(BehaviourEventRow.buyer_id == uuid.UUID(str(buyer_id)))
        .order_by(BehaviourEventRow.occurred_at.desc())
        .limit(PROFILE_EVENT_LIMIT))

    events = [
        BehaviourEvent(
            kind=row.kind,
            category_id=str(row.category_id) if row.category_id else None,
            seller_id=str(row.seller_id) if row.seller_id else None,
            occurred_at=row.occurred_at,
        )
        for row in result.scalars().all()
    ]

    profile = build_profile(events, _now())
    return {"buyer_id": str(buyer_id), **profile_as_dict(profile)}


async def forget(db: AsyncSession, buyer_id) -> dict:
    """Delete everything recorded about one buyer.

    Present because a service that records what people look at needs a way to
    stop having recorded it, and because "we cannot delete it" is a much worse
    answer once asked than a delete statement is to write in advance.

    Immediate rather than a soft flag: a deletion that leaves the rows in place
    is not a deletion, and a flag is one forgotten WHERE clause from being no
    protection at all.
    """
    result = await db.execute(
        delete(BehaviourEventRow)
        .where(BehaviourEventRow.buyer_id == uuid.UUID(str(buyer_id))))
    await db.commit()
    logger.info(f"Forgot {result.rowcount} behaviour event(s) for a buyer")
    return {"forgotten": result.rowcount}


async def sweep_expired(db: AsyncSession, days: int = 365) -> int:
    """Delete behaviour too old to affect a ranking.

    Past four half-lives an event contributes about 3% of its original weight
    and cannot change an ordering, so keeping it buys nothing and holds a
    record of what somebody looked at a year ago.
    """
    cutoff = retention_cutoff(_now(), days)
    result = await db.execute(
        delete(BehaviourEventRow)
        .where(BehaviourEventRow.occurred_at < cutoff))
    await db.commit()
    if result.rowcount:
        logger.info(f"Retention sweep removed {result.rowcount} behaviour "
                    f"event(s) older than {days} days")
    return result.rowcount
