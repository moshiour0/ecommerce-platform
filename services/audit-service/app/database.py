import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://admin:supersecret@postgres:5432/audit_db",
)

engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    # Rule 11: appends serialize on an advisory lock, so a backlog must fail
    # fast rather than pile up holding connections.
    connect_args={"server_settings": {"lock_timeout": "3000"}},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session
