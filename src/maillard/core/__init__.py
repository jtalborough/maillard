"""Core business logic for Maillard."""

from maillard.core.config import MaillardConfig, get_config
from maillard.core.events import (
    AITranscriptEvent,
    BaseEvent,
    ConnectionEvent,
    ConnectionState,
    EventBus,
    EventSubscription,
    RoastEventMarker,
    SafetyAlertEvent,
    SafetyAlertLevel,
    StateChangeEvent,
)

__all__ = [
    "MaillardConfig",
    "get_config",
    # Events
    "AITranscriptEvent",
    "BaseEvent",
    "ConnectionEvent",
    "ConnectionState",
    "EventBus",
    "EventSubscription",
    "RoastEventMarker",
    "SafetyAlertEvent",
    "SafetyAlertLevel",
    "StateChangeEvent",
]
