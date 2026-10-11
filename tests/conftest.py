"""Pytest configuration and fixtures for Maillard tests."""

import asyncio
import os
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

# Set test environment variables BEFORE importing maillard modules
os.environ["MAILLARD_MONGODB_HOST"] = "localhost"
os.environ["MAILLARD_MONGODB_PORT"] = "27017"
os.environ["MAILLARD_MONGODB_NAME"] = "maillard_test"


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def mongo_db() -> AsyncGenerator[Any, None]:
    """Initialize Beanie with mongomock-motor for testing.

    Creates an in-memory MongoDB mock and initializes Beanie with all
    document models. Each test gets a fresh database.

    Also sets the module-level _client in maillard.db so that init_db()
    is a no-op when called by lifespan() or other code paths.
    """
    import maillard.db as db_module
    from maillard.db.models import DOCUMENT_MODELS

    client = AsyncMongoMockClient()
    database = client["maillard_test"]

    await init_beanie(
        database=database,
        document_models=DOCUMENT_MODELS,
    )

    # Set the module-level client so init_db() sees it as already initialized
    db_module._client = client  # type: ignore[assignment]

    yield database

    # Clean up all collections after each test
    for collection_name in await database.list_collection_names():
        await database[collection_name].drop()

    db_module._client = None
    client.close()


@pytest.fixture(autouse=True)
def reset_global_state() -> Generator[None, None, None]:
    """Reset all global state before and after each test.

    This fixture is automatically applied to all tests to ensure
    a clean state between tests. This is necessary because:
    1. maillard.core.config uses lru_cache
    2. maillard.db uses module-level client reference
    3. maillard.mcp.state uses a singleton pattern

    Each test starts with completely fresh state.
    """
    # Import here to avoid import issues
    from maillard.core.config import reset_config
    from maillard.db import reset_db
    from maillard.mcp.state import reset_app_state

    # Reset before test
    reset_config()
    reset_db()
    reset_app_state()

    yield

    # Reset after test (cleanup)
    reset_config()
    reset_db()
    reset_app_state()
