import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# behaviour_db. Rule 1: this service owns it and nothing else reads it directly.
#
# It holds the most personal data on the platform -- what individual people
# looked at -- which is why it is a database of its own rather than a table in
# catalog_db, and why affinity is exposed as normalised weights rather than as
# the events behind them.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://admin:supersecret@postgres:5432/behaviour_db",
)

engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=5, max_overflow=5,
    connect_args={"server_settings": {"lock_timeout": "3000"}},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session
