import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# seller_db. Rule 1: this service owns it and nothing else reads it directly.
# catalog_db carries seller_id as an opaque reference with no foreign key for
# exactly this reason -- see migrations/012.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://admin:supersecret@postgres:5432/seller_db",
)

engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    # Rule 11: every SELECT FOR UPDATE needs a lock_timeout so contention
    # surfaces as a fast error instead of a convoy that starves the pool.
    connect_args={"server_settings": {"lock_timeout": "3000"}},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session
