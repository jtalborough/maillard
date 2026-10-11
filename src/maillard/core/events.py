"""Event bus for internal component communication.

This module provides a typed event system for async pub/sub messaging
between components in the Maillard system.

Events are dataclasses representing state changes, roast events,
safety alerts, and connection events. The EventBus allows components
to subscribe to specific event types and receive events asynchronously.

Note on concurrency: The EventBus is async-safe (safe to use from multiple
coroutines within a single asyncio event loop), but NOT thread-safe. All
publish/subscribe operations must occur within the same event loop. If you
need to publish events from a different thread, use `loop.call_soon_threadsafe`.

Event ordering: Each subscriber queue preserves publish order. However, there
is no global ordering guarantee across multiple subscribers or handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any, TypeVar
from uuid import UUID, uuid4

from maillard.driver.base import KaleidoState, RoastEvent

_logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


class SafetyAlertLevel(Enum):
    """Severity level for safety alerts."""

    WARNING = auto()  # Informational, no action required
    CRITICAL = auto()  # Requires immediate attention
    EMERGENCY = auto()  # System taking automatic action


class ConnectionState(Enum):
    """Connection state for roaster connection events."""

    CONNECTING = auto()  # Attempting to connect
    CONNECTED = auto()  # Successfully connected
    DISCONNECTING = auto()  # Attempting to disconnect
    DISCONNECTED = auto()  # Connection closed normally
    RECONNECTING = auto()  # Attempting to reconnect after failure
    FAILED = auto()  # Connection failed


@dataclass(frozen=True)
class BaseEvent:
    """Base class for all events.

    All events have a unique ID and timestamp for tracking and ordering.
    Events are frozen (immutable) to ensure thread-safety when passed
    between async tasks.
    """

    id: UUID = field(default_factory=uuid4, compare=False)
    timestamp: datetime = field(default_factory=_utc_now, compare=False)


@dataclass(frozen=True, kw_only=True)
class StateChangeEvent(BaseEvent):
    """Event emitted when roaster state changes.

    This event is fired whenever the roaster driver receives updated
    state from the hardware. It contains both the full state snapshot
    and optionally highlights which variables changed.

    Attributes:
        state: Current roaster state snapshot
        changed_vars: Set of variable names that changed (if known)
    """

    state: KaleidoState
    stale: bool = False
    data_age_s: float | None = None
    connected: bool = True
    changed_vars: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, kw_only=True)
class RoastEventMarker(BaseEvent):
    """Event emitted when a roast milestone is marked.

    This event is fired when the user or system marks a roast event
    like CHARGE, TURNING_POINT, FIRST_CRACK, etc.

    Attributes:
        event: The roast event type
        time_seconds: Time since charge in seconds (0 for CHARGE event)
        temp_bt: Bean temperature at event time (if available)
        temp_et: Environment temperature at event time (if available)
        auto_detected: True if automatically detected by the system
    """

    event: RoastEvent
    time_seconds: float
    temp_bt: float | None = None
    temp_et: float | None = None
    auto_detected: bool = False


@dataclass(frozen=True, kw_only=True)
class SafetyAlertEvent(BaseEvent):
    """Event emitted when a safety condition is detected.

    Safety alerts indicate conditions that require attention or action.
    The level indicates severity, and action_taken shows what (if any)
    automatic action the system performed.

    Attributes:
        level: Severity level of the alert
        condition: Stable key for persistent dashboard alarms
        cleared: True when a keyed alarm is resolved
        message: Human-readable description of the condition
        current_value: The value that triggered the alert (if applicable)
        threshold: The threshold that was exceeded (if applicable)
        action_taken: Description of automatic action taken (if any)
    """

    level: SafetyAlertLevel
    message: str
    condition: str | None = None
    cleared: bool = False
    current_value: float | None = None
    threshold: float | None = None
    action_taken: str | None = None


@dataclass(frozen=True, kw_only=True)
class ConnectionEvent(BaseEvent):
    """Event emitted when connection state changes.

    This event tracks the connection lifecycle between the driver
    and the roaster hardware.

    Attributes:
        state: New connection state
        host: Host address (if applicable)
        port: Port number (if applicable)
        error_message: Error details if state is FAILED
        retry_count: Number of reconnection attempts (if reconnecting)
    """

    state: ConnectionState
    host: str | None = None
    port: int | None = None
    error_message: str | None = None
    retry_count: int = 0


# =============================================================================
# Execution Engine Suggestion Events
# =============================================================================


@dataclass(frozen=True, kw_only=True)
class TurningPointSuggested(BaseEvent):
    """Event emitted when the execution engine detects a likely turning point.

    The turning point is the lowest BT after charge. This event is published
    when BT shows a rising trend after reaching a minimum. Claude should
    confirm and mark the turning point.

    Attributes:
        temp_bt: Bean temperature at suggested turning point
        elapsed_seconds: Time since charge in seconds
        min_bt_detected: The minimum BT value detected
        confidence: Confidence level (0.0 to 1.0) based on trend confirmation
    """

    temp_bt: float
    elapsed_seconds: float
    min_bt_detected: float
    confidence: float = 0.8


@dataclass(frozen=True, kw_only=True)
class DryEndSuggested(BaseEvent):
    """Event emitted when approaching the dry end temperature threshold.

    Dry end typically occurs around 150°C when moisture has evaporated.
    Claude should observe color/smell changes and confirm.

    Attributes:
        temp_bt: Current bean temperature
        elapsed_seconds: Time since charge in seconds
        threshold_temp: The dry end threshold temperature (typically ~150°C)
    """

    temp_bt: float
    elapsed_seconds: float
    threshold_temp: float = 150.0


@dataclass(frozen=True, kw_only=True)
class FirstCrackSuggested(BaseEvent):
    """Event emitted when approaching first crack temperature threshold.

    First crack typically occurs around 195°C. Claude should listen for
    audible cracks and confirm when heard.

    Attributes:
        temp_bt: Current bean temperature
        elapsed_seconds: Time since charge in seconds
        threshold_temp: The first crack threshold temperature (typically ~195°C)
    """

    temp_bt: float
    elapsed_seconds: float
    threshold_temp: float = 195.0


@dataclass(frozen=True, kw_only=True)
class DropSuggested(BaseEvent):
    """Event emitted when the profile target has been reached.

    This event indicates the roast has reached its target endpoint.
    The roast does NOT auto-drop; Claude must confirm and execute drop.

    Attributes:
        temp_bt: Current bean temperature
        elapsed_seconds: Time since charge in seconds
        target_temp: The profile's target drop temperature
        target_time: The profile's target drop time
        reason: Why drop is being suggested (e.g., "profile target reached")
    """

    temp_bt: float
    elapsed_seconds: float
    target_temp: float | None = None
    target_time: float | None = None
    reason: str = "profile target reached"


# =============================================================================
# AI Agent Events
# =============================================================================


@dataclass(frozen=True, kw_only=True)
class AISuggestionEvent(BaseEvent):
    """Event emitted when the AI agent produces a suggestion.

    Published by the RoastAIAgent after each Haiku analysis cycle.
    The dashboard displays reasoning and action buttons.

    Attributes:
        reasoning: Short explanation of the AI's analysis
        suggested_heat: Heat power to suggest (0-100), or None
        suggested_fan: Fan speed to suggest (0-100), or None
        suggested_event: Event to suggest marking, or None
        ask_operator: Question for the operator, or None
        drop: Whether the AI suggests dropping
        auto_applied: True if changes were auto-applied (auto mode)
    """

    reasoning: str
    suggested_heat: int | None = None
    suggested_fan: int | None = None
    suggested_event: str | None = None
    ask_operator: str | None = None
    drop: bool = False
    auto_applied: bool = False


@dataclass(frozen=True, kw_only=True)
class AITranscriptEvent(BaseEvent):
    """Event emitted when the AI/operator transcript gains an entry.

    The dashboard uses this for a durable live AI timeline that survives
    replacing the current suggestion card.

    Attributes:
        role: Entry source: ai, operator, or system.
        kind: Entry kind such as suggestion, question, message, action, plan.
        content: Human-readable transcript text.
        entry_id: Stable ID for UI/repository correlation.
        suggestion: Optional suggestion payload associated with this entry.
        metadata: Optional non-secret metadata for UI display.
    """

    role: str
    kind: str
    content: str
    entry_id: str
    suggestion: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Type variable for event types
EventT = TypeVar("EventT", bound=BaseEvent)

# Type alias for event handlers
EventHandler = Any  # Should be Callable[[EventT], Awaitable[None] | None] but mypy struggles with variance

# Type alias for unsubscribe function
UnsubscribeFunc = Any  # Should be Callable[[], None]


class EventBus:
    """Async event bus for component communication.

    The EventBus implements an async pub/sub pattern using asyncio queues.
    Subscribers can listen for specific event types and receive events
    asynchronously.

    Usage:
        bus = EventBus()

        # Subscribe to events using async context manager
        async with await bus.subscribe(StateChangeEvent) as sub:
            async for event in sub:
                print(f"State changed: {event.state}")

        # Or get subscription and iterate manually
        sub = await bus.subscribe(StateChangeEvent)
        event = await sub.get(timeout=5.0)

        # Publish events
        await bus.publish(StateChangeEvent(state={...}))

        # Or use callback pattern
        async def on_state_change(event: StateChangeEvent):
            print(f"State changed: {event.state}")

        unsubscribe = bus.on(StateChangeEvent, on_state_change)
        # Later: unsubscribe()
    """

    def __init__(self, max_queue_size: int = 100) -> None:
        """Initialize the event bus.

        Args:
            max_queue_size: Maximum number of events to buffer per subscriber.
                When the queue is full, old events are dropped.
        """
        self._max_queue_size = max_queue_size
        # Map from event type to set of subscriber queues
        self._subscribers: dict[type[BaseEvent], set[asyncio.Queue[BaseEvent]]] = {}
        # Map from event type to set of callback functions
        self._handlers: dict[type[BaseEvent], set[EventHandler]] = {}
        # Lock for async-safe subscription management (not thread-safe)
        self._lock = asyncio.Lock()

    async def publish(self, event: BaseEvent) -> None:
        """Publish an event to all subscribers.

        The event is delivered to all queue subscribers and callbacks
        registered for this event type or any of its base classes.

        Args:
            event: The event to publish
        """
        # Find all matching subscriber sets (including base classes)
        for registered_type, queues in self._subscribers.items():
            if isinstance(event, registered_type):
                for queue in queues.copy():  # Copy to avoid modification during iteration
                    try:
                        # Non-blocking put, drop oldest if full
                        if queue.full():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                queue.get_nowait()
                        queue.put_nowait(event)
                    except Exception:
                        _logger.debug(
                            "Failed to enqueue event %s to subscriber",
                            type(event).__name__,
                            exc_info=True,
                        )

        # Call registered handlers
        for registered_type, handlers in self._handlers.items():
            if isinstance(event, registered_type):
                for handler in handlers.copy():
                    try:
                        result = handler(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        _logger.warning(
                            "Handler %s raised exception for event %s",
                            getattr(handler, "__name__", repr(handler)),
                            type(event).__name__,
                            exc_info=True,
                        )

    async def subscribe(
        self,
        event_type: type[EventT],
    ) -> EventSubscription[EventT]:
        """Create a subscription for events of the given type.

        Returns an EventSubscription that can be used as an async iterator
        or context manager.

        Args:
            event_type: The type of events to subscribe to

        Returns:
            EventSubscription for iterating over events
        """
        queue: asyncio.Queue[BaseEvent] = asyncio.Queue(maxsize=self._max_queue_size)

        async with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = set()
            self._subscribers[event_type].add(queue)

        return EventSubscription(self, event_type, queue)

    async def _unsubscribe(
        self,
        event_type: type[BaseEvent],
        queue: asyncio.Queue[BaseEvent],
    ) -> None:
        """Remove a subscription.

        Args:
            event_type: The event type that was subscribed to
            queue: The subscriber's queue to remove
        """
        async with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type].discard(queue)
                if not self._subscribers[event_type]:
                    del self._subscribers[event_type]

    def on(
        self,
        event_type: type[EventT],
        handler: EventHandler,
    ) -> UnsubscribeFunc:
        """Register a callback handler for events.

        This is an alternative to the async iterator pattern. The handler
        will be called for each event of the given type.

        Args:
            event_type: The type of events to handle
            handler: Async or sync function to call for each event

        Returns:
            A function that can be called to unsubscribe
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = set()
        self._handlers[event_type].add(handler)

        def unsubscribe() -> None:
            if event_type in self._handlers:
                self._handlers[event_type].discard(handler)
                if not self._handlers[event_type]:
                    del self._handlers[event_type]

        return unsubscribe

    @property
    def subscriber_count(self) -> int:
        """Get total number of queue subscribers."""
        return sum(len(queues) for queues in self._subscribers.values())

    @property
    def handler_count(self) -> int:
        """Get total number of callback handlers."""
        return sum(len(handlers) for handlers in self._handlers.values())


class EventSubscription[EventT: BaseEvent]:
    """Async subscription to events from the event bus.

    Can be used as an async iterator or context manager.

    Usage:
        async with await bus.subscribe(StateChangeEvent) as sub:
            async for event in sub:
                print(event)
    """

    def __init__(
        self,
        bus: EventBus,
        event_type: type[EventT],
        queue: asyncio.Queue[BaseEvent],
    ) -> None:
        self._bus = bus
        self._event_type = event_type
        self._queue = queue
        self._closed = False

    async def __aenter__(self) -> EventSubscription[EventT]:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def __aiter__(self) -> EventSubscription[EventT]:
        return self

    async def __anext__(self) -> EventT:
        if self._closed:
            raise StopAsyncIteration

        try:
            return await self._queue.get()  # type: ignore[return-value]
        except asyncio.CancelledError:
            raise StopAsyncIteration from None

    async def get(self, timeout: float | None = None) -> EventT | None:
        """Get the next event with optional timeout.

        Args:
            timeout: Maximum time to wait in seconds. None means wait forever.

        Returns:
            The next event, or None if timeout expires or subscription closed
        """
        if self._closed:
            return None

        try:
            if timeout is None:
                return await self._queue.get()  # type: ignore[return-value]
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)  # type: ignore[arg-type]
        except (TimeoutError, asyncio.CancelledError):
            return None

    async def close(self) -> None:
        """Close the subscription and clean up."""
        if not self._closed:
            self._closed = True
            await self._bus._unsubscribe(self._event_type, self._queue)

    @property
    def pending(self) -> int:
        """Number of events waiting in the queue."""
        return self._queue.qsize()
