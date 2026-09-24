import os
import ssl
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Render often gives postgres://; Supabase/Render give postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Strip query params that asyncpg does not understand
# (Supabase PgBouncer, libpq sslmode, etc.)
if "?" in DATABASE_URL:
    base, query = DATABASE_URL.split("?", 1)
    _IGNORED = {"pgbouncer", "connection_limit", "connect_timeout", "sslmode", "ssl"}
    clean_params = "&".join(
        p for p in query.split("&")
        if p.split("=")[0] not in _IGNORED
    )
    DATABASE_URL = f"{base}?{clean_params}" if clean_params else base

# statement_cache_size=0 is required when connecting through Supabase's
# PgBouncer pooler (port 6543, transaction mode) — it doesn't support
# prepared statements.
# Remote hosts (Supabase, Render) need TLS; localhost does not.
# Use CERT_NONE so Windows / corporate proxies / the Supabase pooler
# don't fail on a self-signed cert in the chain. Traffic is still encrypted.
_connect_args: dict = {"statement_cache_size": 0}
_is_local = "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL
if DATABASE_URL and not _is_local:
    _ssl = ssl.create_default_context()
    _ssl.check_hostname = False
    _ssl.verify_mode = ssl.CERT_NONE
    _connect_args["ssl"] = _ssl

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app):
    """Create all tables on startup."""
    from models import Base  # avoid circular import at module level
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
