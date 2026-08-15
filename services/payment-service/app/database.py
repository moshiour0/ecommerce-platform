from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

import os
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://admin:supersecret@postgres:5432/payment_ledger_db")

engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    connect_args={"server_settings": {"lock_timeout": "3000"}}
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with async_session() as session:
        yield session
