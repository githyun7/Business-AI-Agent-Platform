# PostgreSQL via asyncpg
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://agent:agentpass@localhost:5432/agentdb"
)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

async def init_db():
    """Create tables on startup."""
    async with engine.begin() as conn:
        from models import Session, Message  # noqa: F401 — needed for metadata
        await conn.run_sync(Base.metadata.create_all)