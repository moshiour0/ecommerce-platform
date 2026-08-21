import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# review_db. Rule 1: this service owns it and nothing else reads it directly.
# A review references an order and a product by id with no foreign key, which
# is why verifying a purchase is an HTTP call to order-saga rather than a join.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://admin:supersecret@postgres:5432/review_db",
)

# 5 + 5, matching every other service: sixteen services fully open is 160
# against a max_connections of 200. See any sibling database.py for the
# incident that set this.
engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=5, max_overflow=5,
    connect_args={"server_settings": {"lock_timeout": "3000"}},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session
