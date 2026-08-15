import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# PostgreSQL setup
import os
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
REDIS_URL = "redis://redis:6379/0"
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

async def get_redis():
    yield redis_client
