"""Roast management and execution."""

from maillard.roasting.curve import (
    DEFAULT_SAMPLE_INTERVAL,
    DEFAULT_SMOOTHING_WINDOW,
    MIN_SAMPLES_FOR_ROR,
    ROR_WINDOW_SECONDS,
    VALID_EVENT_TYPES,
    CurveCollector,
    CurveEvent,
    CurveSample,
    DuplicateEventError,
    InvalidEventTypeError,
    NonMonotonicTimeError,
)
from maillard.roasting.execution import (
    ControlMode,
    ExecutionPhase,
    RoastExecutionEngine,
    TargetSettings,
)
from maillard.roasting.session import RoastSession
from maillard.roasting.state import (
    DEFAULT_CHARGE_TEMP_TOLERANCE,
    DEFAULT_DRY_END_TEMP,
    DEFAULT_FC_TEMP,
    InvalidStateError,
    RoastPhase,
    RoastState,
    RoastStateMachine,
)

__all__ = [
    # Curve processing
    "CurveCollector",
    "CurveEvent",
    "CurveSample",
    "DEFAULT_SAMPLE_INTERVAL",
    "DEFAULT_SMOOTHING_WINDOW",
    "DuplicateEventError",
    "InvalidEventTypeError",
    "MIN_SAMPLES_FOR_ROR",
    "NonMonotonicTimeError",
    "ROR_WINDOW_SECONDS",
    "VALID_EVENT_TYPES",
    # Execution engine
    "ControlMode",
    "ExecutionPhase",
    "RoastExecutionEngine",
    "TargetSettings",
    # Session
    "RoastSession",
    # State machine
    "DEFAULT_CHARGE_TEMP_TOLERANCE",
    "DEFAULT_DRY_END_TEMP",
    "DEFAULT_FC_TEMP",
    "InvalidStateError",
    "RoastPhase",
    "RoastState",
    "RoastStateMachine",
]
