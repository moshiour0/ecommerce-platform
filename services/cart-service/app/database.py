import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# PostgreSQL setup
import os

from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
POSTGRES_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://admin:supersecret@postgres:5432/cart_db")
engine = create_async_engine(
    POSTGRES_URL, echo=False, pool_size=20, max_overflow=10,
    connect_args={"server_settings": {"lock_timeout": "3000"}}
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with async_session() as session:
        yield session

# Redis setup
#
# From the environment, like every other dependency in this platform. It was a
# literal, which meant this service could not be pointed at another Redis
# without a rebuild -- the same drift es_client.py already records fixing.
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Retry a dropped connection rather than turning it into a 500.
#
# Redis restarts close every pooled connection, and the first command on each
# stale one raises ConnectionError. Without a retry that surfaces as "Failed to
# write to Redis" -- a failed add-to-cart or a failed checkout -- for as long as
# the pool takes to cycle. Observed directly: recreating the Redis container
# produced six such errors, one of which failed a checkout in the mutex test.
#
# health_check_interval is the other half: it pings a connection that has been
# idle longer than the interval before handing it out, so a stale one is found
# by the pool rather than by a customer.
redis_client = aioredis.from_url(
    REDIS_URL,
    decode_responses=True,
    retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), retries=3),
    retry_on_error=[ConnectionError, TimeoutError],
    health_check_interval=30,
)

async def get_redis():
    yield redis_client
