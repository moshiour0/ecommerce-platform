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

# Sized so every service's pool can be fully open at once and still fit
# inside Postgres' max_connections.
#
# This was 20 + 10 in every one of sixteen services -- up to 480 possible
# connections against a max_connections of 100. It survived because
# nothing had ever driven enough concurrent work to open them, and then a
# test that ran seventeen order lifecycles did: Postgres hit 103 of 100
# and ten unrelated e2e tests failed with read timeouts that looked like
# a dozen separate bugs.
#
# 5 + 5 across sixteen services is 160, against a max_connections now
# raised to 200. The real answer at this size is a connection pooler --
# PgBouncer in transaction mode, one upstream connection serving many
# clients -- and that is a component this platform does not have yet.
engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=5, max_overflow=5,
    # Rule 11: every SELECT FOR UPDATE needs a lock_timeout so contention
    # surfaces as a fast error instead of a convoy that starves the pool.
    connect_args={"server_settings": {"lock_timeout": "3000"}},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session
