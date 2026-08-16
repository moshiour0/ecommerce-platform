import uuid
import json
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from ..models import OutboxMessage, IdempotencyKey, CartState
from ..schemas import CartAddRequest, CartResponse, CartItem
from .checkout_lock import (
    acquire as acquire_lock, lock_key_for, new_token, release as release_lock,
)
from .cart_cache_rules import DurableCart, merge_item, resolve_cart

logger = logging.getLogger(__name__)
CART_TTL = 3600  # 1 hour


async def _load_durable(db: AsyncSession, user_id: str):
    """The user's live cart_state row, or None.

    Filtered to 'active' to match the invariant the rest of this service
    assumes -- at most one active cart per user. resolve_cart re-checks the
    status anyway, so a row that slips through in another state still resolves
    to empty rather than being trusted.
    """
    result = await db.execute(
        select(CartState)
        .where(CartState.user_id == uuid.UUID(user_id))
        .where(CartState.status == 'active')
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return DurableCart(items=row.items or {}, status=row.status,
                       expires_at=row.expires_at)


async def _resolve(db: AsyncSession, redis: Redis, user_id: str):
    """Read both stores and decide what the cart is. See cart_cache_rules."""
    raw = await redis.get(f"cart:{user_id}")
    # `is not None` rather than a truth test: a stored "{}" is a real, empty
    # cart and must not be mistaken for the missing key that means a miss.
    cached = json.loads(raw) if raw is not None else None
    durable = await _load_durable(db, user_id)
    view = resolve_cart(cached, durable, datetime.now(timezone.utc))
    return view, durable


async def _rewarm(redis: Redis, user_id: str, items: dict, durable) -> None:
    """Put a rehydrated cart back in Redis so the next read is fast again.

    The TTL is the cart's remaining life, not a fresh hour: refreshing it here
    would let a cart outlive the expires_at the sweeper works from, so reading
    a cart would keep it alive indefinitely.

    Failing to re-warm is not worth failing the read over -- the caller already
    has the right answer from Postgres.
    """
    ttl = CART_TTL
    if durable is not None and durable.expires_at is not None:
        remaining = int((durable.expires_at - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            return
        ttl = min(CART_TTL, remaining)
    try:
        await redis.set(f"cart:{user_id}", json.dumps(items), ex=ttl)
    except Exception as e:
        logger.warning("cart cache re-warm failed for %s: %s", user_id, e)

async def check_idempotency(db: AsyncSession, idempotency_key: str):
    """Rule 4: On conflict, we let the caller handle the cached response."""
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        return True  # Already processed — caller should return cached data
    return False  # New key — proceed with business logic

async def add_to_cart(db: AsyncSession, redis: Redis, user_id: str, request: CartAddRequest, idempotency_key: str) -> CartResponse:
    # 1. Postgres Idempotency Check — return current cart on retry
    is_duplicate = await check_idempotency(db, idempotency_key)
    if is_duplicate:
        return await get_cart(db, redis, user_id)

    # 2. Resolve the cart from BOTH stores before merging.
    #
    # Reading only Redis here is what made an eviction destructive: the merge
    # was applied to an empty view and the result then overwrote cart_state,
    # so a lost cache key deleted the durable cart on the very next add.
    cart_key = f"cart:{user_id}"
    view, _durable = await _resolve(db, redis, user_id)

    cart_items = merge_item(view.items, str(request.item.product_id),
                            request.item.quantity)

    # Attempt Redis update
    try:
        await redis.set(cart_key, json.dumps(cart_items), ex=CART_TTL)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to write to Redis")

    # 3. UPSERT Postgres CartState for sweeping
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=CART_TTL)
    
    existing_cart = await db.execute(select(CartState).where(CartState.user_id == uuid.UUID(user_id)).where(CartState.status == 'active'))
    cart_state = existing_cart.scalar_one_or_none()
    
    if cart_state:
        cart_state.items = cart_items
        cart_state.expires_at = expires_at
    else:
        cart_state = CartState(
            user_id=uuid.UUID(user_id),
            items=cart_items,
            status='active',
            expires_at=expires_at
        )
        db.add(cart_state)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    response_items = [CartItem(product_id=uuid.UUID(k), quantity=v) for k, v in cart_items.items()]
    return CartResponse(user_id=uuid.UUID(user_id), items=response_items)

async def checkout_cart(db: AsyncSession, redis: Redis, user_id: str, idempotency_key: str) -> dict:
    cart_key = f"cart:{user_id}"
    
    # C-1 Fix: Acquire Redis lock to prevent double-checkout.
    # The value is a per-request token, not a constant. With a constant, a slow
    # Postgres commit could outlive the 10s TTL, a second request would acquire
    # the lock, and this request's finally-block would delete a lock it no
    # longer owns -- releasing mutual exclusion at exactly the wrong moment.
    lock_key = lock_key_for(user_id)
    lock_token = new_token()
    lock_acquired = await acquire_lock(redis, lock_key, lock_token)
    if not lock_acquired:
        raise HTTPException(status_code=409, detail="Checkout already in progress for this user")

    try:
        # Resolve from both stores rather than Redis alone. An evicted key used
        # to 404 a checkout whose cart was alive and well in Postgres. A cart
        # that is genuinely gone -- expired, or already claimed by another
        # checkout that set status to checkout_in_progress -- still resolves to
        # empty here, so the mutex's losing requests keep getting 404/409.
        view, _durable = await _resolve(db, redis, user_id)
        if view.empty:
            raise HTTPException(status_code=404, detail="Cart not found or empty")

        # Postgres Idempotency Check — return cached on retry
        is_duplicate = await check_idempotency(db, idempotency_key)
        if is_duplicate:
            return {"status": "Checkout already initiated", "user_id": user_id}
        
        # C-4 Fix: Atomically transition cart from 'active' to 'checkout_in_progress'
        # Only sweep-safe if we successfully claim this transition
        result = await db.execute(
            text("""
                UPDATE cart_state
                SET status = 'checkout_in_progress', updated_at = NOW()
                WHERE user_id = :user_id AND status = 'active'
                RETURNING cart_id
            """),
            {"user_id": user_id}
        )
        updated_row = result.fetchone()
        if not updated_row:
            # Cart was already expired or checked out by sweeper
            raise HTTPException(status_code=409, detail="Cart already expired or checkout in progress")
        
        outbox_message = OutboxMessage(
            aggregate_type="Cart",
            aggregate_id=user_id,
            type="CartCheckoutInitiated",
            payload={
                "user_id": user_id,
                "items": view.items,
                "initiated_at": datetime.now(timezone.utc).isoformat()
            }
        )
        db.add(outbox_message)

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=str(e))

        # Delete from Redis AFTER Postgres commit
        await redis.delete(cart_key)

        return {"status": "Checkout initiated", "user_id": user_id}
    finally:
        # Compare-and-delete: release only if we still hold it. An
        # unconditional DELETE here would free a lock a *later* request had
        # acquired after our TTL lapsed. See checkout_lock for the protocol
        # and its tests.
        await release_lock(redis, lock_key, lock_token)

async def get_cart(db: AsyncSession, redis: Redis, user_id: str) -> CartResponse:
    # Reads used to hit Redis and stop there, so an evicted key was reported as
    # an empty cart while cart_state still held the items. The durable row is
    # now the fallback, and a recovered cart is written back so only the first
    # read after an eviction pays for Postgres.
    view, durable = await _resolve(db, redis, user_id)

    if view.rehydrated:
        logger.info("cart cache miss for %s, rehydrated %d item(s) from postgres",
                    user_id, len(view.items))
        await _rewarm(redis, user_id, view.items, durable)

    response_items = [CartItem(product_id=uuid.UUID(k), quantity=v)
                      for k, v in view.items.items()]

    return CartResponse(user_id=uuid.UUID(user_id), items=response_items)
