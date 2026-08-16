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

logger = logging.getLogger(__name__)
CART_TTL = 3600  # 1 hour

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
        return await get_cart(redis, user_id)
    
    # 2. Redis Cart Operations
    cart_key = f"cart:{user_id}"
    
    # Read existing cart
    existing_cart_data = await redis.get(cart_key)
    cart_items = {}
    if existing_cart_data:
        cart_items = json.loads(existing_cart_data)

    # Update item quantity
    prod_id = str(request.item.product_id)
    if prod_id in cart_items:
        cart_items[prod_id] += request.item.quantity
    else:
        cart_items[prod_id] = request.item.quantity

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
        # Check if cart exists in Redis
        existing_cart_data = await redis.get(cart_key)
        if not existing_cart_data:
            raise HTTPException(status_code=404, detail="Cart not found or empty")
        
        # Postgres Idempotency Check — return cached on retry
        is_duplicate = await check_idempotency(db, idempotency_key)
        if is_duplicate:
            return {"status": "Checkout already initiated", "user_id": user_id}
        
        # C-4 Fix: Atomically transition cart from 'active' to 'checkout_in_progress'
        # Only sweep-safe if we successfully claim this transition
        result = await db.execute(
            text("""
                UPDATE cart_state SET status = 'checkout_in_progress'
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
                "items": json.loads(existing_cart_data),
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

async def get_cart(redis: Redis, user_id: str) -> CartResponse:
    cart_key = f"cart:{user_id}"
    existing_cart_data = await redis.get(cart_key)
    
    if not existing_cart_data:
        return CartResponse(user_id=uuid.UUID(user_id), items=[])

    cart_items = json.loads(existing_cart_data)
    response_items = [CartItem(product_id=uuid.UUID(k), quantity=v) for k, v in cart_items.items()]
    
    return CartResponse(user_id=uuid.UUID(user_id), items=response_items)
