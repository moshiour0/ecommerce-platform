"""
Booking the escrow ledger.

The decisions — what balances, how commission rounds, which accounts move —
live in ledger_rules and are unit tested. What lives here is the write, and it
has two jobs beyond persistence.

**Refuse an unbalanced transaction.** `assert_balanced` runs before anything is
stored. Money appearing from nowhere in a ledger is worse than a rejected
request: the rejection is loud, and the imbalance is silent until somebody
reconciles a bank statement months later.

**Book each thing once.** Delivery events arrive through the outbox and the
dispatcher, which is at-least-once, and couriers resend callbacks on top of
that. A second delivery booking would credit the seller twice for one parcel,
so the uniqueness on (seller_order_id, reason, account) is load-bearing rather
than tidy, and a repeat is a no-op rather than an error.
"""

import logging
import os
import uuid

import httpx
from fastapi import HTTPException
from python_common.resilience import (
    AsyncCircuitBreaker, BulkheadFullError, CircuitOpenError,
)
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models import LedgerEntry, OutboxMessage
from .ledger_rules import (
    Account, EntryReason, LedgerError, assert_balanced, plan_delivery,
    plan_payout, plan_settlement,
)

logger = logging.getLogger(__name__)

SELLER_SERVICE_URL = os.getenv("SELLER_SERVICE_URL", "http://seller-service:8020")
SELLER_TIMEOUT = float(os.getenv("SELLER_SERVICE_TIMEOUT", "5.0"))

_breaker = AsyncCircuitBreaker("seller-service", bulkhead=10)


async def _commission_for(seller_id) -> dict:
    """The rate this seller accepted, from seller-service.

    Read at delivery rather than snapshotted at checkout, and that is safe for
    a specific reason: the rate comes from the seller's *accepted contract
    version*, which only changes when the seller deliberately accepts new
    terms. Raising the platform's rate does not move it, so there is no window
    in which an order silently reprices.

    Fails closed. A commission that cannot be established is not defaulted to
    the current rate -- booking money against a contract nobody can produce is
    exactly the number that survives until a seller disputes it.
    """
    async def _get():
        async with httpx.AsyncClient(timeout=SELLER_TIMEOUT) as client:
            return await client.get(
                f"{SELLER_SERVICE_URL}/sellers/{seller_id}/commission")

    try:
        response = await _breaker.call(_get)
    except (CircuitOpenError, BulkheadFullError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"cannot establish the commission rate ({e}); refusing to "
                   f"book escrow at a guessed rate") from e
    except (httpx.TimeoutException, httpx.RequestError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"seller-service unreachable ({type(e).__name__}); "
                   f"refusing to book escrow at a guessed rate") from e

    # Two different failures hide behind "not 200", and collapsing them into
    # one status is expensive now that the dispatcher acts on the difference.
    #
    #   404 -- seller-service answered, and this seller has no commission rate:
    #          they do not exist, or never accepted a contract. Repetition will
    #          not conjure one. Terminal, so the command parks and is seen.
    #   5xx -- seller-service is broken, not authoritative. Retryable, because
    #          the rate almost certainly exists and we simply cannot read it.
    #
    # Returning 409 for both, as this did, would let a thirty-second outage in
    # seller-service permanently park real liabilities: the dispatcher would
    # read "the downstream refused" where the truth was "the downstream fell
    # over". Anything unexpected is treated as retryable for the same reason --
    # money is not written off on a status nobody anticipated.
    if response.status_code == 404:
        raise HTTPException(
            status_code=409,
            detail=f"no commission rate for seller {seller_id}: "
                   f"seller-service returned 404. Either the seller does not "
                   f"exist or they have accepted no contract; escrow cannot "
                   f"be booked at a guessed rate.")
    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail=f"seller-service returned {response.status_code} for "
                   f"seller {seller_id}; refusing to book escrow at a guessed "
                   f"rate, and this is retryable rather than final")
    return response.json()


async def _already_booked(db: AsyncSession, seller_order_id,
                          reason: EntryReason) -> bool:
    result = await db.execute(
        select(func.count(LedgerEntry.id))
        .where(LedgerEntry.seller_order_id == seller_order_id)
        .where(LedgerEntry.reason == reason.value))
    return (result.scalar() or 0) > 0


async def _write(db: AsyncSession, entries, currency: str,
                 commission_bps=None, contract_version=None) -> uuid.UUID:
    """Persist one balanced transaction, or refuse it."""
    try:
        assert_balanced(entries)
    except LedgerError as e:
        raise HTTPException(status_code=422, detail=str(e))

    transaction_id = uuid.uuid4()
    for entry in entries:
        db.add(LedgerEntry(
            transaction_id=transaction_id,
            account=entry.account.value,
            amount_cents=entry.amount_cents,
            currency=currency.upper(),
            reason=entry.reason.value,
            seller_id=uuid.UUID(entry.seller_id) if entry.seller_id else None,
            seller_order_id=(uuid.UUID(entry.seller_order_id)
                             if entry.seller_order_id else None),
            commission_bps=commission_bps,
            contract_version=contract_version,
            detail=entry.detail[:512],
        ))
    return transaction_id


async def book_delivery(db: AsyncSession, request) -> dict:
    """The buyer paid the courier: the platform now owes the seller."""
    if await _already_booked(db, request.seller_order_id, EntryReason.DELIVERY):
        # At-least-once delivery plus courier resends. A second booking would
        # credit the seller twice for one parcel.
        logger.info("delivery already booked for %s", request.seller_order_id)
        return {"transaction_id": uuid.UUID(int=0), "reason": "DELIVERY",
                "entries": 0, "seller_owed_delta": 0,
                "detail": "already booked; ignoring"}

    commission = await _commission_for(request.seller_id)
    rate_bps = commission["commission_bps"]

    try:
        entries = plan_delivery(str(request.seller_id),
                                str(request.seller_order_id),
                                request.collected_cents, rate_bps)
    except LedgerError as e:
        raise HTTPException(status_code=422, detail=str(e))

    transaction_id = await _write(
        db, entries, request.currency, rate_bps,
        commission.get("accepted_contract_version"))

    owed = -sum(e.amount_cents for e in entries
                if e.account is Account.SELLER_PAYABLE)

    db.add(OutboxMessage(
        aggregate_type="Ledger",
        aggregate_id=str(transaction_id),
        type="EscrowBooked",
        payload={
            "transaction_id": str(transaction_id),
            "seller_id": str(request.seller_id),
            "seller_order_id": str(request.seller_order_id),
            "collected_cents": request.collected_cents,
            "commission_bps": rate_bps,
            "seller_owed_cents": owed,
        },
    ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        # The uniqueness on (seller_order_id, reason, account) fired: another
        # copy of the same event won the race. That is the guard working.
        logger.info("delivery booking raced for %s: %s",
                    request.seller_order_id, e)
        return {"transaction_id": uuid.UUID(int=0), "reason": "DELIVERY",
                "entries": 0, "seller_owed_delta": 0,
                "detail": "already booked; ignoring"}

    logger.info("escrow booked for seller order %s: seller owed %s at %sbps",
                request.seller_order_id, owed, rate_bps)
    return {"transaction_id": transaction_id, "reason": "DELIVERY",
            "entries": len(entries), "seller_owed_delta": owed,
            "detail": f"commission {rate_bps}bps"}


async def book_settlement(db: AsyncSession, request) -> dict:
    """The courier handed the cash over.

    Does not touch what the seller is owed: that was decided at delivery, and a
    slow courier is the platform's problem rather than a reason to owe the
    seller less.
    """
    if await _already_booked(db, request.seller_order_id,
                             EntryReason.SETTLEMENT):
        return {"transaction_id": uuid.UUID(int=0), "reason": "SETTLEMENT",
                "entries": 0, "seller_owed_delta": 0,
                "detail": "already booked; ignoring"}

    try:
        entries = plan_settlement(str(request.seller_id),
                                  str(request.seller_order_id),
                                  request.remitted_cents)
    except LedgerError as e:
        raise HTTPException(status_code=422, detail=str(e))

    transaction_id = await _write(db, entries, request.currency)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.info("settlement booking raced for %s: %s",
                    request.seller_order_id, e)
        return {"transaction_id": uuid.UUID(int=0), "reason": "SETTLEMENT",
                "entries": 0, "seller_owed_delta": 0,
                "detail": "already booked; ignoring"}

    return {"transaction_id": transaction_id, "reason": "SETTLEMENT",
            "entries": len(entries), "seller_owed_delta": 0,
            "detail": f"remitted {request.remitted_cents}"}


async def book_payout(db: AsyncSession, request) -> dict:
    """The platform paid the seller.

    Not idempotent by seller order, because a payout is not attached to one --
    it settles a balance across many. Callers must supply their own
    idempotency; the ledger will happily record two payouts, and the resulting
    negative balance is visible rather than hidden, which is what a ledger is
    for.
    """
    try:
        entries = plan_payout(str(request.seller_id), request.amount_cents)
    except LedgerError as e:
        raise HTTPException(status_code=422, detail=str(e))

    transaction_id = await _write(db, entries, request.currency)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return {"transaction_id": transaction_id, "reason": "PAYOUT",
            "entries": len(entries),
            "seller_owed_delta": -request.amount_cents,
            "detail": f"paid out {request.amount_cents}"}


async def seller_balance(db: AsyncSession, seller_id) -> dict:
    """What the platform owes this seller, as a positive number."""
    result = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0),
               func.count(LedgerEntry.id))
        .where(LedgerEntry.seller_id == seller_id)
        .where(LedgerEntry.account == Account.SELLER_PAYABLE.value))
    total, count = result.one()
    return {"seller_id": seller_id, "owed_cents": -int(total),
            "currency": "BDT", "entries": int(count)}


async def ledger_health(db: AsyncSession) -> dict:
    """Does the whole ledger balance?

    The one check worth running over everything ever written. A non-zero total
    means some transaction was stored unbalanced and the platform's books are
    wrong by exactly that amount -- so it is reported as a number rather than a
    boolean, because the number is the size of the problem.
    """
    total = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)))
    imbalance = int(total.scalar() or 0)

    unbalanced = await db.execute(
        select(LedgerEntry.transaction_id)
        .group_by(LedgerEntry.transaction_id)
        .having(func.sum(LedgerEntry.amount_cents) != 0))
    offenders = [str(row[0]) for row in unbalanced.all()]

    by_account = {}
    rows = await db.execute(
        select(LedgerEntry.account,
               func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
        .group_by(LedgerEntry.account))
    for account, amount in rows.all():
        by_account[account] = int(amount)

    return {
        "balanced": imbalance == 0 and not offenders,
        "imbalance_cents": imbalance,
        "unbalanced_transactions": offenders[:20],
        "accounts": by_account,
    }
