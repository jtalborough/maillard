"""Database layer for Maillard.

Provides Motor/Beanie initialization and connection management for MongoDB.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from maillard.core.config import get_config

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

# Module-level client
_client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]

_MAX_RETRIES = 5
_BACKOFF_BASE = 1  # seconds


async def init_db() -> None:
    """Initialize the MongoDB connection and Beanie ODM.

    Creates the Motor client and initializes Beanie with all document models.
    Safe to call multiple times (idempotent). Retries with exponential backoff
    if the connection fails (max 5 attempts, ~31s total).
    """
    global _client
    if _client is not None:
        return

    from maillard.db.models import DOCUMENT_MODELS

    config = get_config()
    _logger.info(
        "Connecting to MongoDB host=%s port=%d database=%s",
        config.mongodb_host,
        config.mongodb_port,
        config.mongodb_name,
    )

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            username, password = config.load_mongodb_credentials()
            if username is not None and password is not None:
                _client = AsyncIOMotorClient(
                    host=config.mongodb_host,
                    port=config.mongodb_port,
                    username=username,
                    password=password,
                    authSource=config.mongodb_auth_source,
                )
            else:
                _client = AsyncIOMotorClient(
                    host=config.mongodb_host,
                    port=config.mongodb_port,
                )
            database = _client[config.mongodb_name]

            await init_beanie(
                database=database,
                document_models=DOCUMENT_MODELS,
            )

            _logger.info("Beanie ODM initialized")
            return
        except Exception as exc:
            # Reset so idempotency guard doesn't block retries
            _client = None
            if attempt == _MAX_RETRIES:
                _logger.error(
                    "MongoDB connection failed after %d attempts (error_type=%s) — giving up",
                    _MAX_RETRIES,
                    type(exc).__name__,
                )
                raise RuntimeError("MongoDB initialization failed") from None
            delay = _BACKOFF_BASE * (2 ** (attempt - 1))
            _logger.warning(
                "MongoDB connection attempt %d/%d failed (error_type=%s), retrying in %ds",
                attempt,
                _MAX_RETRIES,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)


async def close_db() -> None:
    """Close the MongoDB connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        _logger.debug("MongoDB connection closed")


def get_client() -> AsyncIOMotorClient | None:  # type: ignore[type-arg]
    """Get the current Motor client, if initialized."""
    return _client


def reset_db() -> None:
    """Reset the client reference.

    Useful for testing with different database configurations.
    Does not close the connection — call close_db() first if needed.
    """
    global _client
    _client = None


# Keep reset_engine as alias for backward compatibility during migration
reset_engine = reset_db

__all__ = [
    "close_db",
    "get_client",
    "init_db",
    "reset_db",
    "reset_engine",
]
