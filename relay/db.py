"""Async database engine, session factory, and startup helpers."""
from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings
from .models import Base, User

logger = logging.getLogger("bentunnel.db")

# Module-level singletons, populated by :func:`init_engine`.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazily create and cache the global async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazily create and cache the global session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an :class:`AsyncSession`."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables if missing.

    Idempotent — uses ``CREATE TABLE IF NOT EXISTS`` semantics via
    ``Base.metadata.create_all``. **Never** drops anything.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema verified")


async def bootstrap_admin() -> None:
    """Create the admin user from env-provided bootstrap credentials."""
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            User.__table__.select().where(User.email == settings.ADMIN_EMAIL)
        )
        existing = result.first()
        if existing is not None:
            logger.info("Admin user already exists — skipping bootstrap")
            return

        admin = User(
            email=settings.ADMIN_EMAIL,
            first_name=settings.ADMIN_BOOTSTRAP_FIRST_NAME,
            is_admin=1,
            is_suspended=0,
        )
        admin.set_password(settings.ADMIN_BOOTSTRAP_PASSWORD)
        session.add(admin)
        await session.commit()
        logger.info("Bootstrapped admin user %s", settings.ADMIN_EMAIL)


async def dispose_engine() -> None:
    """Dispose the engine — used on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
