"""Hardware drivers for coffee roasters.

This module provides the interface and implementations for communicating
with coffee roaster hardware. The primary supported roaster is the
Kaleido M10 with network control.
"""

from maillard.driver.base import (
    # Core types
    KaleidoState,
    KaleidoTimingConstants,
    RoasterDriver,
    RoasterState,  # Alias for KaleidoState
    RoastEvent,
    # Variable type constants
    FLOAT_VARS,
    INT_VARS,
    STR_VARS,
    # Variable type helper functions
    is_float_var,
    is_int_var,
    is_known_float_var,
    is_str_var,
)
from maillard.driver.kaleido import KaleidoDriver
from maillard.driver.simulator import (
    NetworkSimConfig,
    SimulatorDriver,
    ThermalModelConfig,
)

__all__ = [
    # Core types
    "KaleidoState",
    "KaleidoTimingConstants",
    "RoasterDriver",
    "RoasterState",
    "RoastEvent",
    # Variable type constants
    "FLOAT_VARS",
    "INT_VARS",
    "STR_VARS",
    # Variable type helper functions
    "is_float_var",
    "is_int_var",
    "is_known_float_var",
    "is_str_var",
    # Driver implementations
    "KaleidoDriver",
    "SimulatorDriver",
    # Simulator configuration
    "NetworkSimConfig",
    "ThermalModelConfig",
]
