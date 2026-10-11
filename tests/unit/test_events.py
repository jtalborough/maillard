"""Unit tests for the Event Bus system."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from maillard.core.events import (
    BaseEvent,
    ConnectionEvent,
    ConnectionState,
    EventBus,
    RoastEventMarker,
    SafetyAlertEvent,
    SafetyAlertLevel,
    StateChangeEvent,
)
from maillard.driver.base import KaleidoState, RoastEvent


class TestEventDataclasses:
    """Tests for event dataclass creation and properties."""

    def test_base_event_has_id_and_timestamp(self) -> None:
        """BaseEvent should automatically generate id and timestamp."""
        event = BaseEvent()
        assert isinstance(event.id, UUID)
        assert isinstance(event.timestamp, datetime)

    def test_timestamp_is_timezone_aware_utc(self) -> None:
        """Event timestamps should be timezone-aware UTC."""
        event = BaseEvent()
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.tzinfo == UTC

    def test_events_are_immutable(self) -> None:
        """Events should be frozen (immutable)."""
        event = BaseEvent()
        with pytest.raises(AttributeError):
            event.id = UUID("00000000-0000-0000-0000-000000000000")  # type: ignore[misc]

    def test_state_change_event_creation(self) -> None:
        """StateChangeEvent should store state and changed vars."""
        state: KaleidoState = {"BT": 150.0, "ET": 180.0, "HP": 80}
        changed = frozenset({"BT", "ET"})
        event = StateChangeEvent(state=state, changed_vars=changed)

        assert event.state == state
        assert event.changed_vars == changed
        assert isinstance(event.id, UUID)

    def test_state_change_event_default_changed_vars(self) -> None:
        """StateChangeEvent should default to empty changed_vars."""
        state: KaleidoState = {"BT": 150.0}
        event = StateChangeEvent(state=state)
        assert event.changed_vars == frozenset()

    def test_roast_event_marker_creation(self) -> None:
        """RoastEventMarker should store roast event details."""
        # kw_only=True means all args must be keyword args
        event = RoastEventMarker(
            event=RoastEvent.CHARGE,
            time_seconds=0.0,
            temp_bt=195.5,
            temp_et=220.0,
            auto_detected=False,
        )

        assert event.event == RoastEvent.CHARGE
        assert event.time_seconds == 0.0
        assert event.temp_bt == 195.5
        assert event.temp_et == 220.0
        assert event.auto_detected is False

    def test_roast_event_marker_defaults(self) -> None:
        """RoastEventMarker should have sensible defaults."""
        event = RoastEventMarker(
            event=RoastEvent.TURNING_POINT,
            time_seconds=75.0,
        )

        assert event.temp_bt is None
        assert event.temp_et is None
        assert event.auto_detected is False

    def test_safety_alert_event_creation(self) -> None:
        """SafetyAlertEvent should store safety condition details."""
        event = SafetyAlertEvent(
            level=SafetyAlertLevel.CRITICAL,
            message="BT exceeded maximum temperature",
            current_value=235.0,
            threshold=230.0,
            action_taken="Emergency stop initiated",
        )

        assert event.level == SafetyAlertLevel.CRITICAL
        assert event.message == "BT exceeded maximum temperature"
        assert event.current_value == 235.0
        assert event.threshold == 230.0
        assert event.action_taken == "Emergency stop initiated"

    def test_connection_event_creation(self) -> None:
        """ConnectionEvent should store connection state details."""
        event = ConnectionEvent(
            state=ConnectionState.CONNECTED,
            host="192.0.2.10",
            port=80,
        )

        assert event.state == ConnectionState.CONNECTED
        assert event.host == "192.0.2.10"
        assert event.port == 80
        assert event.error_message is None
        assert event.retry_count == 0

    def test_connection_event_with_error(self) -> None:
        """ConnectionEvent should store error details for failed connections."""
        event = ConnectionEvent(
            state=ConnectionState.FAILED,
            host="192.0.2.10",
            port=80,
            error_message="Connection refused",
            retry_count=3,
        )

        assert event.state == ConnectionState.FAILED
        assert event.error_message == "Connection refused"
        assert event.retry_count == 3


class TestEventBusBasic:
    """Basic tests for EventBus functionality."""

    @pytest.fixture
    def bus(self) -> EventBus:
        """Create a fresh EventBus for each test."""
        return EventBus()

    @pytest.mark.asyncio
    async def test_publish_to_no_subscribers(self, bus: EventBus) -> None:
        """Publishing with no subscribers should not raise."""
        event = StateChangeEvent(state={"BT": 150.0})
        await bus.publish(event)  # Should not raise

    @pytest.mark.asyncio
    async def test_subscribe_creates_subscription(self, bus: EventBus) -> None:
        """Subscribing should create a subscription object."""
        sub = await bus.subscribe(StateChangeEvent)
        assert sub is not None
        assert bus.subscriber_count == 1
        await sub.close()
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_publish_and_receive(self, bus: EventBus) -> None:
        """Published events should be received by subscribers."""
        sub = await bus.subscribe(StateChangeEvent)

        state: KaleidoState = {"BT": 150.0, "ET": 180.0}
        event = StateChangeEvent(state=state)
        await bus.publish(event)

        received = await sub.get(timeout=1.0)
        assert received is not None
        assert received.state == state
        await sub.close()

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bus: EventBus) -> None:
        """Multiple subscribers should all receive the event."""
        sub1 = await bus.subscribe(StateChangeEvent)
        sub2 = await bus.subscribe(StateChangeEvent)

        event = StateChangeEvent(state={"BT": 150.0})
        await bus.publish(event)

        received1 = await sub1.get(timeout=1.0)
        received2 = await sub2.get(timeout=1.0)

        assert received1 is not None
        assert received2 is not None
        assert received1.id == received2.id

        await sub1.close()
        await sub2.close()

    @pytest.mark.asyncio
    async def test_type_filtering(self, bus: EventBus) -> None:
        """Subscribers should only receive events of their subscribed type."""
        state_sub = await bus.subscribe(StateChangeEvent)
        safety_sub = await bus.subscribe(SafetyAlertEvent)

        state_event = StateChangeEvent(state={"BT": 150.0})
        safety_event = SafetyAlertEvent(
            level=SafetyAlertLevel.WARNING,
            message="Test warning",
        )

        await bus.publish(state_event)
        await bus.publish(safety_event)

        # State subscriber should get state event
        received_state = await state_sub.get(timeout=0.1)
        assert received_state is not None
        assert isinstance(received_state, StateChangeEvent)

        # State subscriber should not get safety event
        no_event = await state_sub.get(timeout=0.1)
        assert no_event is None

        # Safety subscriber should get safety event
        received_safety = await safety_sub.get(timeout=0.1)
        assert received_safety is not None
        assert isinstance(received_safety, SafetyAlertEvent)

        await state_sub.close()
        await safety_sub.close()


class TestEventBusCallbackHandlers:
    """Tests for callback-based event handling."""

    @pytest.fixture
    def bus(self) -> EventBus:
        """Create a fresh EventBus for each test."""
        return EventBus()

    @pytest.mark.asyncio
    async def test_sync_handler(self, bus: EventBus) -> None:
        """Synchronous handlers should be called for events."""
        received_events: list[StateChangeEvent] = []

        def handler(event: StateChangeEvent) -> None:
            received_events.append(event)

        bus.on(StateChangeEvent, handler)
        assert bus.handler_count == 1

        event = StateChangeEvent(state={"BT": 150.0})
        await bus.publish(event)

        assert len(received_events) == 1
        assert received_events[0].id == event.id

    @pytest.mark.asyncio
    async def test_async_handler(self, bus: EventBus) -> None:
        """Asynchronous handlers should be awaited for events."""
        received_events: list[SafetyAlertEvent] = []

        async def handler(event: SafetyAlertEvent) -> None:
            await asyncio.sleep(0.01)  # Simulate async work
            received_events.append(event)

        bus.on(SafetyAlertEvent, handler)

        event = SafetyAlertEvent(
            level=SafetyAlertLevel.WARNING,
            message="Test",
        )
        await bus.publish(event)

        assert len(received_events) == 1
        assert received_events[0].id == event.id

    @pytest.mark.asyncio
    async def test_unsubscribe_handler(self, bus: EventBus) -> None:
        """Unsubscribe function should remove the handler."""
        received_events: list[StateChangeEvent] = []

        def handler(event: StateChangeEvent) -> None:
            received_events.append(event)

        unsubscribe = bus.on(StateChangeEvent, handler)
        assert bus.handler_count == 1

        # Publish first event
        await bus.publish(StateChangeEvent(state={"BT": 150.0}))
        assert len(received_events) == 1

        # Unsubscribe
        unsubscribe()
        assert bus.handler_count == 0

        # Publish second event - should not be received
        await bus.publish(StateChangeEvent(state={"BT": 160.0}))
        assert len(received_events) == 1  # Still only 1

    @pytest.mark.asyncio
    async def test_handler_error_does_not_crash_bus(self, bus: EventBus) -> None:
        """Handler errors should not prevent other handlers from running."""
        good_events: list[StateChangeEvent] = []

        def bad_handler(event: StateChangeEvent) -> None:
            raise ValueError("Handler error")

        def good_handler(event: StateChangeEvent) -> None:
            good_events.append(event)

        bus.on(StateChangeEvent, bad_handler)
        bus.on(StateChangeEvent, good_handler)

        event = StateChangeEvent(state={"BT": 150.0})
        await bus.publish(event)  # Should not raise

        assert len(good_events) == 1


class TestEventSubscription:
    """Tests for EventSubscription functionality."""

    @pytest.fixture
    def bus(self) -> EventBus:
        """Create a fresh EventBus for each test."""
        return EventBus()

    @pytest.mark.asyncio
    async def test_context_manager(self, bus: EventBus) -> None:
        """Subscription should work as async context manager."""
        async with await bus.subscribe(StateChangeEvent) as sub:
            assert bus.subscriber_count == 1

            event = StateChangeEvent(state={"BT": 150.0})
            await bus.publish(event)

            received = await sub.get(timeout=1.0)
            assert received is not None

        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_async_iterator(self, bus: EventBus) -> None:
        """Subscription should work as async iterator."""
        sub = await bus.subscribe(StateChangeEvent)

        # Publish a few events
        events_to_send = [
            StateChangeEvent(state={"BT": 150.0}),
            StateChangeEvent(state={"BT": 160.0}),
            StateChangeEvent(state={"BT": 170.0}),
        ]
        for event in events_to_send:
            await bus.publish(event)

        # Receive via async iterator
        received_events = []
        for _ in range(len(events_to_send)):
            event = await sub.get(timeout=1.0)
            if event:
                received_events.append(event)

        assert len(received_events) == 3
        assert [e.state.get("BT") for e in received_events] == [150.0, 160.0, 170.0]

        await sub.close()

    @pytest.mark.asyncio
    async def test_get_timeout(self, bus: EventBus) -> None:
        """Get with timeout should return None when no event."""
        sub = await bus.subscribe(StateChangeEvent)

        result = await sub.get(timeout=0.05)
        assert result is None

        await sub.close()

    @pytest.mark.asyncio
    async def test_pending_count(self, bus: EventBus) -> None:
        """Pending property should show queued events."""
        sub = await bus.subscribe(StateChangeEvent)
        assert sub.pending == 0

        await bus.publish(StateChangeEvent(state={"BT": 150.0}))
        await bus.publish(StateChangeEvent(state={"BT": 160.0}))
        assert sub.pending == 2

        await sub.get(timeout=1.0)
        assert sub.pending == 1

        await sub.close()

    @pytest.mark.asyncio
    async def test_closed_subscription_returns_none(self, bus: EventBus) -> None:
        """Closed subscription should return None from get()."""
        sub = await bus.subscribe(StateChangeEvent)
        await sub.close()

        result = await sub.get(timeout=0.1)
        assert result is None


class TestEventBusEdgeCases:
    """Edge case and stress tests for EventBus."""

    @pytest.mark.asyncio
    async def test_queue_overflow(self) -> None:
        """When queue is full, oldest events should be dropped."""
        bus = EventBus(max_queue_size=3)
        sub = await bus.subscribe(StateChangeEvent)

        # Publish more events than queue can hold
        for i in range(5):
            await bus.publish(StateChangeEvent(state={"BT": float(i * 10)}))

        # Should only have last 3 events
        events = []
        for _ in range(5):
            event = await sub.get(timeout=0.05)
            if event:
                events.append(event)

        assert len(events) == 3
        # First two were dropped
        temps = [e.state.get("BT") for e in events]
        assert temps == [20.0, 30.0, 40.0]

        await sub.close()

    @pytest.mark.asyncio
    async def test_base_event_subscription_receives_all(self) -> None:
        """Subscribing to BaseEvent should receive all event types."""
        bus = EventBus()
        sub = await bus.subscribe(BaseEvent)

        await bus.publish(StateChangeEvent(state={"BT": 150.0}))
        await bus.publish(SafetyAlertEvent(level=SafetyAlertLevel.WARNING, message="Test"))
        await bus.publish(ConnectionEvent(state=ConnectionState.CONNECTED))

        event1 = await sub.get(timeout=1.0)
        event2 = await sub.get(timeout=1.0)
        event3 = await sub.get(timeout=1.0)

        assert isinstance(event1, StateChangeEvent)
        assert isinstance(event2, SafetyAlertEvent)
        assert isinstance(event3, ConnectionEvent)

        await sub.close()

    @pytest.mark.asyncio
    async def test_concurrent_publish_subscribe(self) -> None:
        """Bus should handle concurrent operations safely."""
        bus = EventBus()
        received_counts: dict[int, int] = {}

        async def subscriber(sub_id: int) -> None:
            received_counts[sub_id] = 0
            sub = await bus.subscribe(StateChangeEvent)
            for _ in range(10):
                event = await sub.get(timeout=2.0)
                if event:
                    received_counts[sub_id] += 1
            await sub.close()

        async def publisher() -> None:
            for i in range(10):
                await bus.publish(StateChangeEvent(state={"BT": float(i)}))
                await asyncio.sleep(0.01)

        # Start subscribers first, then publisher
        tasks = [
            asyncio.create_task(subscriber(1)),
            asyncio.create_task(subscriber(2)),
            asyncio.create_task(subscriber(3)),
        ]
        await asyncio.sleep(0.1)  # Let subscribers set up
        tasks.append(asyncio.create_task(publisher()))

        await asyncio.gather(*tasks)

        # Each subscriber should receive all 10 events
        for sub_id, count in received_counts.items():
            assert count == 10, f"Subscriber {sub_id} got {count} events, expected 10"


class TestSafetyAlertLevelEnum:
    """Tests for SafetyAlertLevel enum."""

    def test_warning_is_lowest(self) -> None:
        """WARNING should be the lowest severity."""
        assert SafetyAlertLevel.WARNING.value < SafetyAlertLevel.CRITICAL.value
        assert SafetyAlertLevel.CRITICAL.value < SafetyAlertLevel.EMERGENCY.value

    def test_all_levels_exist(self) -> None:
        """All expected safety levels should exist."""
        levels = [level.name for level in SafetyAlertLevel]
        assert "WARNING" in levels
        assert "CRITICAL" in levels
        assert "EMERGENCY" in levels


class TestConnectionStateEnum:
    """Tests for ConnectionState enum."""

    def test_all_states_exist(self) -> None:
        """All expected connection states should exist."""
        states = [state.name for state in ConnectionState]
        expected = [
            "CONNECTING",
            "CONNECTED",
            "DISCONNECTING",
            "DISCONNECTED",
            "RECONNECTING",
            "FAILED",
        ]
        for expected_state in expected:
            assert expected_state in states
