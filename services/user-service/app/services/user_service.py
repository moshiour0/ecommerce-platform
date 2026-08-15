import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from passlib.context import CryptContext
from ..models import User, OutboxMessage, IdempotencyKey
from ..schemas import UserCreate

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

async def register_user(db: AsyncSession, user_in: UserCreate, idempotency_key: str) -> User:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")
    
    # Check if user already exists
    existing_user = await db.execute(select(User).where(User.email == user_in.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Create User
    user_id = uuid.uuid4()
    hashed_password = get_password_hash(user_in.password)
    user = User(
        id=user_id,
        email=user_in.email,
        password_hash=hashed_password,
        is_active=True
    )
    db.add(user)

    # Create OutboxMessage within the same atomic transaction
    payload = {
        "id": str(user.id),
        "email": user.email,
        "is_active": user.is_active,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="User",
        aggregate_id=str(user.id),
        type="UserCreated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(user)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return user
