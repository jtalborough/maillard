"""Shared application state for the MCP server.

This module manages the shared state across all MCP tools including:
- Database session management
- Roaster driver instance
- Current roast state
- Event bus for component communication

The ApplicationState class provides lifespan management for async resources.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from maillard.core.config import MaillardConfig, get_config
from maillard.core.cooling import CoolingFanController
from maillard.core.events import EventBus
from maillard.core.heating import HeatingManager
from maillard.core.safety import SafetyMonitor
from maillard.db import close_db, init_db
from maillard.driver import KaleidoDriver, RoasterDriver, SimulatorDriver
from maillard.mcp.monitoring import RoastMonitor
from maillard.roasting.execution import RoastExecutionEngine
from maillard.roasting.session import RoastSession
from maillard.roasting.state import RoastPhase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_logger = logging.getLogger(__name__)


DriverType = Literal["kaleido", "simulator"]


@dataclass
class RoastContext:
    """Context for the current roast session.

    Tracks the active roast ID and provides roast-specific state.
    This is set when a roast begins and cleared when it completes or aborts.
    """

    roast_id: str | None = None
    profile_id: str | None = None
    coffee_id: str | None = None
    is_active: bool = False
    charge_time: float | None = None  # Unix timestamp when CHARGE was triggered


@dataclass
class ApplicationState:
    """Shared application state for the MCP server.

    This class manages all shared resources and state needed by MCP tools.
    Use the `lifespan` context manager to properly initialize and cleanup
    resources.

    Attributes:
        config: Application configuration
        driver: Active roaster driver (Kaleido or Simulator)
        event_bus: Event bus for component communication
        roast_context: Current roast session context
    """

    config: MaillardConfig = field(default_factory=get_config)
    driver: RoasterDriver | None = None
    event_bus: EventBus = field(default_factory=EventBus)
    roast_context: RoastContext = field(default_factory=RoastContext)
    roast_session: RoastSession | None = None
    _heating_manager: HeatingManager | None = field(default=None, repr=False)
    _monitor: RoastMonitor | None = field(default=None, repr=False)
    _safety_monitor: SafetyMonitor | None = field(default=None, repr=False)
    _execution_engine: RoastExecutionEngine | None = field(default=None, repr=False)
    _cooling: CoolingFanController | None = field(default=None, repr=False)
    _initialized: bool = field(default=False, repr=False)
    _driver_type: DriverType = field(default="kaleido", repr=False)

    def cooling_fan(self) -> CoolingFanController:
        if self._cooling is None:
            self._cooling = CoolingFanController(
                duration_seconds=self.config.cooling_fan_duration_seconds
            )
        else:
            self._cooling.configure(self.config.cooling_fan_duration_seconds)
        return self._cooling

    @property
    def is_roast_active(self) -> bool:
        """Check if a roast session is currently active."""
        return self.roast_session is not None and self.roast_session.is_active

    @property
    def current_phase(self) -> RoastPhase | None:
        """Current roast sub-phase, or None if no session."""
        if self.roast_session is None:
            return None
        return self.roast_session.current_phase

    @property
    def roast_elapsed_time(self) -> float | None:
        """Elapsed time in seconds since charge, or None if no session."""
        if self.roast_session is None:
            return None
        return self.roast_session.elapsed_time

    @property
    def is_initialized(self) -> bool:
        """Check if application state has been initialized."""
        return self._initialized

    async def initialize(self) -> None:
        """Initialize async resources.

        This method initializes the database and prepares the application
        for operation. Call this before using any MCP tools.
        """
        if self._initialized:
            return

        _logger.info("Initializing application state")

        # Initialize database
        await init_db()
        _logger.debug("Database initialized")

        self._initialized = True
        _logger.info("Application state initialized")

    async def shutdown(self) -> None:
        """Shutdown and cleanup async resources.

        Disconnects from the roaster (if connected) and closes
        the database connection.

        Shutdown order (safety-critical):
        1. HeatingManager - Stop HS maintenance first (safety)
        2. ExecutionEngine - Stop profile following
        3. RoastMonitor - Stop polling
        4. SafetyMonitor - Stop safety checks
        5. Driver - Disconnect
        """
        _logger.info("Shutting down application state")

        # Stop heating manager FIRST (safety: stop heat before anything else)
        if self._heating_manager is not None:
            try:
                await self._heating_manager.stop()
            except Exception:
                _logger.warning("Error stopping heating manager", exc_info=True)
            self._heating_manager = None

        # Stop monitors if running (order: execution engine, roast monitor, safety)
        if self._execution_engine is not None:
            try:
                await self._execution_engine.stop()
            except Exception:
                _logger.warning("Error stopping execution engine", exc_info=True)
            self._execution_engine = None

        if self._monitor is not None:
            try:
                await self._monitor.stop()
            except Exception:
                _logger.warning("Error stopping roast monitor", exc_info=True)
            self._monitor = None

        if self._safety_monitor is not None:
            try:
                await self._safety_monitor.stop()
            except Exception:
                _logger.warning("Error stopping safety monitor", exc_info=True)
            self._safety_monitor = None

        # Disconnect driver if connected
        if self.driver is not None and self.driver.is_connected:
            try:
                await self.driver.disconnect()
                _logger.debug("Driver disconnected")
            except Exception:
                _logger.warning("Error disconnecting driver", exc_info=True)

        # Close database
        await close_db()
        _logger.debug("Database closed")

        self._initialized = False
        _logger.info("Application state shutdown complete")

    async def halt_heat_sources(self) -> None:
        """Stop every component that can (re-)apply heat.

        Used by the SafetyMonitor emergency hook and emergency-stop routes.
        Order: HeatingManager first (it re-sends HS=1 every second), then the
        execution engine (PROFILE mode applies phase heat). Never raises.
        """
        if self._heating_manager is not None:
            try:
                await self._heating_manager.stop()
            except Exception:
                _logger.warning("Error stopping heating manager", exc_info=True)
            self._heating_manager = None

        if self._execution_engine is not None:
            try:
                await self._execution_engine.stop()
            except Exception:
                _logger.warning("Error stopping execution engine", exc_info=True)
            self._execution_engine = None

    def create_driver(self, driver_type: DriverType | None = None) -> RoasterDriver:
        """Create a roaster driver instance.

        Creates a new driver instance based on the specified type.
        Use `connect_roaster` to actually connect to hardware.

        Args:
            driver_type: Type of driver to create. Defaults to configured type.

        Returns:
            New driver instance (not yet connected)

        Raises:
            ValueError: If driver_type is not a valid DriverType
        """
        dtype = driver_type or self._driver_type

        if dtype == "simulator":
            driver: RoasterDriver = SimulatorDriver()
            _logger.info("Created simulator driver")
        elif dtype == "kaleido":
            driver = KaleidoDriver(ping_interval=self.config.kaleido_ping_interval,
                                   ping_timeout=self.config.kaleido_ping_timeout)
            _logger.info("Created Kaleido driver")
        else:
            raise ValueError(f"Unknown driver type: {dtype!r}. Must be 'kaleido' or 'simulator'.")

        return driver

    async def connect_roaster(
        self,
        driver_type: DriverType | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Connect to a roaster.

        Creates a driver if needed and connects to the specified host/port.
        Uses configuration defaults if host/port not specified.

        Args:
            driver_type: Type of driver to use (kaleido or simulator)
            host: Roaster IP address (defaults to config.kaleido_host)
            port: Roaster port (defaults to config.kaleido_port)

        Raises:
            ConnectionError: If connection fails
            RuntimeError: If already connected
        """
        if self.driver is not None and self.driver.is_connected:
            raise RuntimeError("Already connected to roaster")

        # Clean up any existing driver (may have a ghost reconnect loop)
        if self.driver is not None:
            try:
                await self.driver.disconnect()
            except Exception:
                _logger.debug("Cleanup of old driver during connect", exc_info=True)
            self.driver = None

        # Create driver
        dtype = driver_type or self._driver_type
        self.driver = self.create_driver(dtype)
        self._driver_type = dtype

        # Determine connection parameters
        connect_host = host or self.config.kaleido_host
        connect_port = port or self.config.kaleido_port

        _logger.info("Connecting to roaster at %s:%d", connect_host, connect_port)

        try:
            await self.driver.connect(connect_host, connect_port)
            _logger.info("Connected to roaster")
        except Exception:
            _logger.exception("Failed to connect to roaster at %s:%d", connect_host, connect_port)
            if self.driver is not None:
                try:
                    await self.driver.disconnect()
                except Exception:
                    _logger.debug("Cleanup of failed driver connection", exc_info=True)
            self.driver = None
            raise

    async def disconnect_roaster(self) -> None:
        """Disconnect from the roaster.

        Safely disconnects from the roaster and clears the driver reference.
        No-op if not connected.
        """
        if self.driver is None:
            return

        try:
            await self.driver.disconnect()
            _logger.info("Disconnected from roaster")
        except Exception:
            _logger.warning("Error during disconnect", exc_info=True)

        self.driver = None

    def clear_roast_context(self) -> None:
        """Clear the current roast context and session.

        Called when a roast completes, aborts, or is reset.
        Callers should stop the monitor and execution engine before calling this method.
        """
        if self._monitor is not None:
            _logger.warning("clear_roast_context called while monitor still set — caller should stop it first")
        if self._execution_engine is not None:
            _logger.warning("clear_roast_context called while execution engine still set — caller should stop it first")
        self.roast_context = RoastContext()
        self.roast_session = None
        self._execution_engine = None
        _logger.debug("Roast context and session cleared")


# Global application state instance
_app_state: ApplicationState | None = None


def get_app_state() -> ApplicationState:
    """Get the global application state instance.

    Returns the singleton ApplicationState instance, creating it
    if necessary. The state must be initialized via the lifespan
    context manager before use.

    Returns:
        The global ApplicationState instance
    """
    global _app_state
    if _app_state is None:
        _app_state = ApplicationState()
    return _app_state


def reset_app_state() -> None:
    """Reset the global application state.

    This is primarily useful for testing to ensure a clean state
    between tests.
    """
    global _app_state
    _app_state = None


@asynccontextmanager
async def lifespan() -> AsyncGenerator[ApplicationState, None]:
    """Lifespan context manager for the MCP server.

    Handles initialization and cleanup of async resources.
    Use this as the lifespan handler for FastMCP.

    Usage:
        async with lifespan() as state:
            # Application is ready
            ...
        # Cleanup complete

    Yields:
        Initialized ApplicationState
    """
    state = get_app_state()
    try:
        await state.initialize()
        yield state
    finally:
        await state.shutdown()
