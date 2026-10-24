"""Kaleido M10 WebSocket driver implementation.

This module provides async WebSocket communication with the Kaleido M10
coffee roaster, ported and adapted from Artisan's kaleido.py.

Protocol Reference:
- Connection: ws://<host>:<port>/ws
- Message format: {[<tag> <value>]}\n
- Response format: {<sid>[,<var>:<value>]*}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import websockets

from maillard.driver.base import (
    KaleidoState,
    KaleidoTimingConstants,
    RoasterDriver,
    RoastEvent,
    is_int_var,
    is_str_var,
)

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

_log = logging.getLogger(__name__)


class KaleidoDriver(RoasterDriver):
    """Async WebSocket driver for Kaleido M10 roaster.

    This driver implements the RoasterDriver protocol using WebSocket
    communication with the Kaleido M10. It handles:
    - Connection lifecycle with safety handshake
    - Message encoding/decoding per Kaleido protocol
    - Async command queue for writes
    - Auto-reconnection on connection loss
    - State management and polling

    Usage:
        driver = KaleidoDriver()
        await driver.connect("192.0.2.10", 80)

        # Read current state
        state = await driver.read_state()
        print(f"BT: {state.get('BT')}")

        # Control roaster
        await driver.set_heat(80)
        await driver.set_fan(50)

        # Mark events
        await driver.trigger_event(RoastEvent.CHARGE)

        # Disconnect
        await driver.disconnect()
    """

    def __init__(
        self,
        temperature_unit: str = "C",
        *,
        connected_callback: Callable[[], None] | None = None,
        disconnected_callback: Callable[[], None] | None = None,
        logging_enabled: bool = False,
        ping_interval: float = KaleidoTimingConstants.PING_INTERVAL,
        ping_timeout: float = KaleidoTimingConstants.KEEPALIVE_TIMEOUT,
        max_missed_replies: int = 3,
        stall_close_seconds: float = 12.0,
        forced_close_reconnect_delay: float = 3.0,
    ) -> None:
        """Initialize the Kaleido driver.

        Args:
            temperature_unit: Temperature unit ('C' or 'F'), default 'C'
            connected_callback: Optional callback when connection established
            disconnected_callback: Optional callback when disconnected
            logging_enabled: Enable verbose protocol logging
        """
        self._ping_interval = ping_interval or None
        self._ping_timeout = ping_timeout or None
        self._max_missed_replies = max_missed_replies
        self._stall_close_seconds = stall_close_seconds
        self._forced_close_reconnect_delay = forced_close_reconnect_delay
        self._forced_close = False
        self._connected_at: float | None = None
        self._missed_replies = 0
        self._last_rx_time: float | None = None
        self._reconnect_failures = 0
        self.connection_events: deque[dict] = deque(maxlen=500)
        self._connection_listeners: list[Callable[[dict], None]] = []
        self._temperature_unit = temperature_unit
        self._connected_callback = connected_callback
        self._disconnected_callback = disconnected_callback
        self._logging_enabled = logging_enabled

        # Connection state
        self._websocket: ClientConnection | None = None
        self._write_queue: asyncio.Queue[str] | None = None
        self._running = False
        self._is_connected = False
        self._tu_verified = False

        # State management
        self._state: KaleidoState = {}
        self.last_bt_sample_time: float | None = None
        self._last_hp_sample_time: float | None = None
        self.connection_generation = 0
        self._pending_requests: dict[str, asyncio.Event] = {}
        self._single_await_var_prefix = "!"

        # Connection info
        self._host: str = ""
        self._port: int = 80
        self._path: str = "ws"

        # Background tasks
        self._connection_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._write_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        """Check if the driver is connected to the roaster."""
        return self._is_connected

    @property
    def missed_replies(self) -> int:
        return self._missed_replies

    @property
    def last_rx_time(self) -> float | None:
        return self._last_rx_time

    @property
    def reconnect_failures(self) -> int:
        return self._reconnect_failures

    @property
    def link_state(self) -> str:
        if self._is_connected:
            return "stalled" if self._missed_replies else "connected"
        return "reconnecting" if self._running else "disconnected"

    def add_connection_listener(self, cb: Callable[[dict], None]) -> None:
        if cb not in self._connection_listeners:
            self._connection_listeners.append(cb)

    def remove_connection_listener(self, cb: Callable[[dict], None]) -> None:
        if cb in self._connection_listeners:
            self._connection_listeners.remove(cb)

    def _connection_event(self, kind: str, reason: str | None = None,
                          close_code: int | None = None) -> None:
        event = {"ts": datetime.now(UTC).isoformat(), "mono": time.monotonic(),
                 "type": kind, "reason": reason, "close_code": close_code}
        self.connection_events.append(event)
        if kind in {"connected", "closed", "forced_close", "reconnect_failed"}:
            _log.info("Kaleido %s: code=%s reason=%s", kind, close_code, reason)
        for cb in tuple(self._connection_listeners):
            try:
                cb(event.copy())
            except Exception:
                _log.exception("Connection listener failed")

    @staticmethod
    def _next_backoff(attempt: int) -> float:
        return min(5.0, KaleidoTimingConstants.RECONNECT_DELAY * 2 ** min(attempt, 4)) * random.uniform(0.75, 1.25)

    @property
    def state(self) -> KaleidoState:
        """Get the current roaster state snapshot."""
        return self._state.copy()

    # -------------------------------------------------------------------------
    # Message Encoding/Decoding
    # -------------------------------------------------------------------------

    @staticmethod
    def encode_message(tag: str, value: str | None = None) -> str:
        """Encode a message in Kaleido protocol format.

        Message format: {[<tag>]}\n or {[<tag> <value>]}\n

        Args:
            tag: Command tag (e.g., 'PI', 'HP', 'FC')
            value: Optional value to send with the tag

        Returns:
            Encoded message string with newline terminator
        """
        if value is None:
            return f"{{[{tag}]}}\n"

        # Format value based on tag type
        encoded_value: str
        if is_str_var(tag):
            # String vars are sent as-is
            encoded_value = value
        elif is_int_var(tag):
            # Integer vars: remove decimals
            try:
                encoded_value = f"{float(value):.0f}"
            except ValueError:
                # Non-numeric values (like "UP", "DW") pass through
                encoded_value = value
        else:
            # Float vars: reduce to one decimal, strip trailing zeros
            try:
                encoded_value = f"{float(value):.1f}".rstrip("0").rstrip(".")
            except ValueError:
                encoded_value = value

        return f"{{[{tag} {encoded_value}]}}\n"

    @staticmethod
    def decode_message(message: str) -> tuple[int | None, dict[str, str | int | float]]:
        """Decode a Kaleido protocol response message.

        Response format: {<sid>[,<var>:<value>]*}

        Args:
            message: Raw message string from roaster

        Returns:
            Tuple of (sid, dict of var:value pairs)
            Returns (None, {}) if message format is invalid
        """
        message = message.strip()
        if len(message) < 3 or not message.startswith("{") or not message.endswith("}"):
            return None, {}

        content = message[1:-1]  # Strip { and }
        elements = content.split(",")

        try:
            sid = int(round(float(elements[0])))
        except (ValueError, IndexError):
            return None, {}

        values: dict[str, str | int | float] = {"sid": sid}

        for element in elements[1:]:
            parts = element.split(":")
            if len(parts) >= 2:
                var = parts[0]
                raw_value = parts[1]

                if is_int_var(var):
                    try:
                        values[var] = int(round(float(raw_value)))
                    except ValueError:
                        values[var] = raw_value
                elif is_str_var(var):
                    values[var] = raw_value
                else:
                    # Float or unknown - treat as float
                    try:
                        values[var] = float(raw_value)
                    except ValueError:
                        values[var] = raw_value

        return sid, values

    # -------------------------------------------------------------------------
    # State Management
    # -------------------------------------------------------------------------

    def get_state_value(self, var: str) -> str | int | float | None:
        """Get current value for a state variable.

        Args:
            var: Variable name (e.g., 'BT', 'HP', 'TU')

        Returns:
            Current value or None if not received, -1 for numeric unknown
        """
        if var in self._state:
            value: str | int | float = self._state[var]  # type: ignore[literal-required]
            return value
        if var in {"sid", "TU", "SC", "CL", "SN"}:
            return None
        if is_int_var(var):
            return -1
        return -1.0

    async def _set_state(self, var: str, value: str, single_response: bool) -> None:
        """Update internal state from received value.

        Args:
            var: Variable name
            value: Raw string value from protocol
            single_response: True if this is a single var/value response
        """
        if is_int_var(var):
            try:
                self._state[var] = int(round(float(value)))  # type: ignore[literal-required]
                if var == "HP":
                    self._last_hp_sample_time = time.monotonic()
            except (ValueError, OverflowError):
                pass
        elif is_str_var(var):
            self._state[var] = value  # type: ignore[literal-required]
        else:
            try:
                self._state[var] = float(value)  # type: ignore[literal-required]
            except ValueError:
                # Keep as string if conversion fails
                self._state[var] = value  # type: ignore[literal-required]

        bt = self._state.get("BT")
        if var == "BT" and isinstance(bt, (int, float)) and math.isfinite(bt) and bt >= 0:
            self.last_bt_sample_time = time.monotonic()
            self._missed_replies = 0

        # Clear pending request lock
        clear_var = var
        if single_response:
            clear_var = self._single_await_var_prefix + var
        await self._clear_request(clear_var)

    async def _process_message(self, message: str) -> None:
        """Process a received message and update state.

        Args:
            message: Raw message from roaster
        """
        self._last_rx_time = time.monotonic()
        sid, values = self.decode_message(message)
        if sid is None:
            return

        await self._set_state("sid", str(sid), False)

        # Check if this is a single var/value response
        single_response = len(values) == 2  # sid + one var

        for var, value in values.items():
            if var != "sid":
                await self._set_state(var, str(value), single_response)

    async def _add_request(self, var: str) -> asyncio.Event:
        """Add a pending request lock for a variable.

        Args:
            var: Variable name to wait for

        Returns:
            Event that will be set when response received
        """
        if var in self._pending_requests:
            return self._pending_requests[var]
        self._pending_requests[var] = asyncio.Event()
        return self._pending_requests[var]

    async def _clear_request(self, var: str) -> None:
        """Clear a pending request lock.

        Args:
            var: Variable name to clear
        """
        if var in self._pending_requests:
            self._pending_requests.pop(var).set()

    def _reset_state(self) -> None:
        """Reset all state to initial values."""
        self._state = {}
        self._pending_requests = {}

    # -------------------------------------------------------------------------
    # WebSocket Communication
    # -------------------------------------------------------------------------

    async def _ws_write(self, websocket: ClientConnection, message: str) -> None:
        """Write a message to the WebSocket.

        Args:
            websocket: Active WebSocket connection
            message: Message to send
        """
        if self._logging_enabled:
            _log.info("TX: %s", message.strip())
        await websocket.send(message)
        await asyncio.sleep(0.1)  # Yield control to event loop

    async def _ws_write_and_wait(
        self, websocket: ClientConnection, message: str, timeout: float | None = None
    ) -> None:
        """Write a message and wait for response.

        Used during initialization when we need synchronous request/response.

        Args:
            websocket: Active WebSocket connection
            message: Message to send
            timeout: Optional timeout override
        """
        if self._logging_enabled:
            _log.info("TX (sync): %s", message.strip())

        send_timeout = timeout or KaleidoTimingConstants.SEND_TIMEOUT
        await asyncio.wait_for(self._ws_write(websocket, message), send_timeout)

        response = await asyncio.wait_for(
            websocket.recv(), KaleidoTimingConstants.PING_TIMEOUT
        )
        response_str = (
            response.decode("utf-8") if isinstance(response, bytes) else response
        )

        if self._logging_enabled:
            _log.info("RX (sync): %s", response_str.strip())

        await self._process_message(response_str.strip())

    def _stalled_long_enough(self) -> bool:
        if self._missed_replies < self._max_missed_replies:
            return False
        ref = self.last_bt_sample_time or self._connected_at
        return ref is None or time.monotonic() - ref >= self._stall_close_seconds

    async def _handle_reads(self, websocket: ClientConnection) -> None:
        """Handle incoming messages from WebSocket.

        Runs as background task while connected.
        """
        while self._running:
            try:
                response = await asyncio.wait_for(
                    websocket.recv(), timeout=KaleidoTimingConstants.READ_TIMEOUT
                )
                message = (
                    response.decode("utf-8") if isinstance(response, bytes) else response
                )

                if self._logging_enabled:
                    _log.info("RX: %s", message.strip())

                await self._process_message(message)
            except TimeoutError:
                # No inbound traffic for READ_TIMEOUT. Outside a roast nothing may be
                # polling, so silence alone is not proof of a dead link: probe with
                # RD A0 and count it as a miss. A BT reply resets the counter; only
                # max_missed_replies consecutive misses force a reconnect.
                self._missed_replies += 1
                if self._missed_replies == 1:
                    self._connection_event("stall", "Read timeout")
                if self._stalled_long_enough():
                    self._forced_close = True
                    self._connection_event("forced_close", "Read timeout after missed replies")
                    self._is_connected = False
                    self.connection_generation += 1
                    await websocket.close()
                    raise
                if self._write_queue is not None:
                    with contextlib.suppress(asyncio.QueueFull):
                        self._write_queue.put_nowait(self.encode_message("RD", "A0"))

    async def _handle_writes(
        self, websocket: ClientConnection, queue: asyncio.Queue[str]
    ) -> None:
        """Handle outgoing messages from write queue.

        Runs as background task while connected.
        """
        while self._running:
            message = await queue.get()
            if message == "":
                # Empty string signals shutdown
                break
            await asyncio.wait_for(self._ws_write(websocket, message),
                                   KaleidoTimingConstants.SEND_TIMEOUT)

    async def _initialize_connection(
        self, websocket: ClientConnection, mode: str
    ) -> None:
        """Perform connection initialization handshake.

        Sequence:
        1. Send PI (ping) until response received
        2. Send TU (temperature unit) command
        3. Send SC AR (start safety guard)

        Args:
            websocket: Active WebSocket connection
            mode: Temperature mode ('C' or 'F')
        """
        # Ping until we get a response
        while self._running:
            try:
                await self._ws_write_and_wait(
                    websocket, self.encode_message("PI")
                )
            except TimeoutError:
                _log.debug("Ping response timeout, retrying...")
                await asyncio.sleep(KaleidoTimingConstants.PING_RETRY_DELAY)
                continue

            # Check if we got a valid response
            if self.get_state_value("sid") is not None:
                break

            await asyncio.sleep(KaleidoTimingConstants.PING_RETRY_DELAY)

        # Set temperature unit (retry up to 3 times — firmware may ignore)
        self._tu_verified = False
        for attempt in range(1, 4):
            try:
                await self._ws_write_and_wait(
                    websocket, self.encode_message("TU", mode), timeout=2.0
                )
            except TimeoutError:
                _log.debug("TU response timeout (attempt %d/3)", attempt)
                continue

            # Verify the unit was accepted
            tu_value = self.get_state_value("TU")
            if tu_value == mode:
                self._tu_verified = True
                _log.info("Temperature unit set to %s (attempt %d)", mode, attempt)
                break
            _log.debug("TU verify mismatch: got %r, expected %r (attempt %d)", tu_value, mode, attempt)

        if not self._tu_verified:
            _log.warning(
                "Temperature unit not confirmed after 3 attempts — "
                "readings may be in Fahrenheit"
            )

        # Send start safety guard
        try:
            await self._ws_write_and_wait(
                websocket, self.encode_message("SC", "AR")
            )
        except TimeoutError:
            _log.debug("SC AR response timeout")

    async def _connection_loop(self) -> None:
        """Main connection loop with auto-reconnection.

        Handles WebSocket connection lifecycle including reconnection
        on disconnection.
        """
        attempt = 0
        while self._running:
            websocket = None
            initialized = False
            self._connection_event("reconnect_attempt")
            try:
                uri = f"ws://{self._host}:{self._port}/{self._path}"
                websocket = await websockets.connect(
                    uri, open_timeout=KaleidoTimingConstants.OPEN_TIMEOUT,
                    ping_interval=self._ping_interval, ping_timeout=self._ping_timeout,
                    close_timeout=2,
                )
                self._websocket = websocket
                self._write_queue = asyncio.Queue()
                await asyncio.wait_for(
                    self._initialize_connection(websocket, self._temperature_unit),
                    timeout=KaleidoTimingConstants.INIT_TIMEOUT,
                )
                initialized = True
                attempt = 0
                self._reconnect_failures = 0
                self._missed_replies = 0
                self._connected_at = time.monotonic()
                self._is_connected = True
                self._connection_event("connected")
                if self._connected_callback:
                    try:
                        self._connected_callback()
                    except Exception:
                        _log.exception("Connected callback error")
                self._read_task = asyncio.create_task(self._handle_reads(websocket))
                self._write_task = asyncio.create_task(self._handle_writes(websocket, self._write_queue))
                done, _ = await asyncio.wait(
                    [self._read_task, self._write_task], return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not initialized:
                    self._reconnect_failures += 1
                    self._connection_event("reconnect_failed", str(exc))
            finally:
                self._is_connected = False
                self.connection_generation += 1
                for task in (self._read_task, self._write_task):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(t for t in (self._read_task, self._write_task)
                                       if t is not None), return_exceptions=True)
                if websocket is not None:
                    await websocket.close()
                    self._connection_event("closed", getattr(websocket, "close_reason", None),
                                           getattr(websocket, "close_code", None))
                self._reset_state()
                self._websocket = None
                self._write_queue = None
                if self._disconnected_callback:
                    try:
                        self._disconnected_callback()
                    except Exception:
                        _log.exception("Disconnected callback error")
            if self._running:
                delay = self._next_backoff(attempt)
                if self._forced_close:
                    delay = max(delay, self._forced_close_reconnect_delay)
                    self._forced_close = False
                await asyncio.sleep(delay)
                attempt += 1

    # -------------------------------------------------------------------------
    # Public API - Connection
    # -------------------------------------------------------------------------

    async def connect(self, host: str, port: int = 80) -> None:
        """Connect to the Kaleido roaster.

        Starts the connection loop which handles initialization and
        auto-reconnection.

        Args:
            host: IP address or hostname of the roaster
            port: Port number (default 80)

        Raises:
            ConnectionError: If already connected
        """
        if self._running:
            raise ConnectionError("Already connected or connecting")

        self._host = host
        self._port = port
        self._running = True

        self._connection_task = asyncio.create_task(self._connection_loop())

        # Wait for initial connection with timeout
        timeout = KaleidoTimingConstants.OPEN_TIMEOUT + KaleidoTimingConstants.INIT_TIMEOUT
        start_time = asyncio.get_event_loop().time()

        while not self._is_connected:
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(f"Connection timeout to {host}:{port}")
            await asyncio.sleep(0.1)

    async def disconnect(self) -> None:
        """Disconnect from the roaster.

        Sends safety guard clear command before closing connection.
        """
        if not self._running:
            return

        # Send CL AR (clear safety guard) before disconnecting
        try:
            await self._send_request(
                "CL", "AR", "SN",
                timeout=2 * KaleidoTimingConstants.SEND_TIMEOUT,
                single_request=True
            )
        except Exception as e:
            _log.debug("CL AR send error: %s", e)

        self._running = False

        # Signal write queue to stop
        if self._write_queue:
            await self._write_queue.put("")

        # Cancel connection task
        if self._connection_task:
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task

        self._reset_state()
        self._is_connected = False
        _log.info("Disconnected from Kaleido")

    # -------------------------------------------------------------------------
    # Public API - Commands
    # -------------------------------------------------------------------------

    async def _send_message(
        self, tag: str, value: str | None = None, timeout: float | None = None
    ) -> None:
        """Send a message through the write queue (fire and forget).

        Args:
            tag: Command tag
            value: Optional value
            timeout: Optional timeout for queue put
        """
        if self._write_queue is None:
            raise ConnectionError("Not connected")

        send_timeout = timeout or KaleidoTimingConstants.SEND_TIMEOUT
        message = self.encode_message(tag, value)

        try:
            await asyncio.wait_for(self._write_queue.put(message), send_timeout)
        except TimeoutError:
            _log.warning("Send queue timeout for %s", message.strip())
            raise ConnectionError(f"Send queue timeout for {tag}") from None

    async def _send_request(
        self,
        target: str,
        value: str | None = None,
        var: str | None = None,
        timeout: float | None = None,
        single_request: bool = False,
    ) -> str | None:
        """Send a message and await response.

        Args:
            target: Command tag
            value: Optional value
            var: Variable to wait for in response (defaults to target)
            timeout: Optional timeout override
            single_request: If True, await single var/value response

        Returns:
            Response value as string, or None on timeout
        """
        if self._write_queue is None:
            raise ConnectionError("Not connected")

        send_timeout = timeout or KaleidoTimingConstants.SEND_TIMEOUT
        variable = target if var is None else var
        await_var = (
            self._single_await_var_prefix + variable
            if var is None or single_request
            else var
        )

        message = self.encode_message(target, value)
        event = await self._add_request(await_var)
        await self._write_queue.put(message)

        try:
            await asyncio.wait_for(event.wait(), send_timeout)
            result = self.get_state_value(variable)
            return str(result) if result is not None else None
        except TimeoutError:
            if self._logging_enabled:
                _log.info("Request timeout for %s", message.strip())
            return None

    async def set_heat(self, power: int) -> None:
        """Set heating power (0-100%).

        Args:
            power: Heating power percentage (0-100)

        Raises:
            ValueError: If power is not in range 0-100
        """
        if not 0 <= power <= 100:
            raise ValueError(f"Power must be 0-100, got {power}")

        current = self.get_state_value("HP")
        if current != power:
            await self._send_message("HP", str(power))

    async def set_fan(self, speed: int) -> None:
        """Set fan speed (0-100%).

        Args:
            speed: Fan speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
        """
        if not 0 <= speed <= 100:
            raise ValueError(f"Speed must be 0-100, got {speed}")

        current = self.get_state_value("FC")
        if current != speed:
            await self._send_message("FC", str(speed))

    async def set_drum(self, speed: int) -> None:
        """Set drum rotation speed (0-100%).

        Args:
            speed: Drum speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
        """
        if not 0 <= speed <= 100:
            raise ValueError(f"Speed must be 0-100, got {speed}")

        current = self.get_state_value("RC")
        if current != speed:
            await self._send_message("RC", str(speed))

    async def set_target_temp(self, temp: float) -> None:
        """Set the PID target temperature.

        Args:
            temp: Target temperature in Celsius (100-260)
        """
        if not 100 <= temp <= 260:
            raise ValueError("Target temperature must be 100-260°C")
        current = self.get_state_value("TS")
        if current != temp:
            # Format: remove trailing zeros and decimal point if whole number
            temp_str = f"{temp:0.1f}".rstrip("0").rstrip(".")
            await self._send_message("TS", temp_str)

    async def set_pid_mode(self, enabled: bool) -> None:
        """Enable or disable PID auto-heating mode.

        Args:
            enabled: True to enable PID, False for manual control
        """
        current = self.get_state_value("AH")
        target = 1 if enabled else 0
        if current != target:
            await self._send_message("AH", str(target))

    async def set_heating_state(self, enabled: bool) -> None:
        """Enable or disable heating element.

        This controls the HS (Heating State) variable which must be enabled
        for the heater to actually produce heat, separate from HP (heat power).

        Args:
            enabled: True to enable heating, False to disable
        """
        current = self.get_state_value("HS")
        target = 1 if enabled else 0
        if current != target:
            await self._send_message("HS", str(target))

    async def trigger_event(self, event: RoastEvent) -> None:
        """Trigger a roast event marker.

        Args:
            event: The roast event to trigger

        Note:
            DROP uses EV 9, not EV 8. Event 8 is detected via sid flag bits.
        """
        await self._send_message("EV", str(event.value))

    async def emergency_stop(self) -> bool:
        """Attempt all stop commands; confirm a fresh HP=0 within 1.5s."""
        _log.warning("Emergency stop triggered!")
        started = time.monotonic()
        sent = True
        for tag, value in (("HP", "0"), ("HS", "0"), ("FC", "100"), ("RD", "A0")):
            try:
                await self._send_message(tag, value, timeout=0.25)
            except Exception:
                sent = False
                _log.critical("Emergency stop send failed: %s", tag, exc_info=True)
        deadline = time.monotonic() + 1.5
        while self.is_connected and time.monotonic() < deadline:
            if (self._last_hp_sample_time is not None
                    and self._last_hp_sample_time >= started
                    and self.get_state_value("HP") == 0):
                return sent
            await asyncio.sleep(0.05)
        _log.critical("EMERGENCY STOP UNCONFIRMED - CUT HEAT ON ROASTER")
        return False

    async def read_state(self) -> KaleidoState:
        """Request and return current state from roaster.

        Sends a state request (RD A0) and waits for the response.

        Returns:
            Current state snapshot after update
        """
        # Only request if initialized (has sid from ping handshake)
        if self.get_state_value("sid") is not None:
            result = await self._send_request("RD", "A0", "BT", timeout=1.0)
            if result is None:
                self._missed_replies += 1
                if self._missed_replies == 1:
                    self._connection_event("stall", "RD reply timeout")
                if self._websocket and self._is_connected and self._stalled_long_enough():
                    self._forced_close = True
                    self._connection_event("forced_close", "Consecutive missed RD replies")
                    self._is_connected = False
                    self.connection_generation += 1
                    await self._websocket.close()

        return self.state

    # -------------------------------------------------------------------------
    # Convenience Methods
    # -------------------------------------------------------------------------

    def get_temperatures(self) -> tuple[float, float, int]:
        """Get current BT, ET, and device status.

        Returns:
            Tuple of (BT, ET, sid) or (-1, -1, 0) if not available
        """
        bt = self.get_state_value("BT")
        et = self.get_state_value("ET")
        sid = self.get_state_value("sid")

        if sid is not None:
            return (
                bt if isinstance(bt, float) else -1.0,
                et if isinstance(et, float) else -1.0,
                sid if isinstance(sid, int) else 0,
            )
        return -1.0, -1.0, 0

    def get_controls(self) -> tuple[int, int, int]:
        """Get current HP, FC, RC (heat, fan, drum).

        Returns:
            Tuple of (HP, FC, RC) percentages
        """
        hp = self.get_state_value("HP")
        fc = self.get_state_value("FC")
        rc = self.get_state_value("RC")

        return (
            hp if isinstance(hp, int) else -1,
            fc if isinstance(fc, int) else -1,
            rc if isinstance(rc, int) else -1,
        )

    def get_pid_state(self) -> tuple[float, int]:
        """Get PID target temperature and auto-heat mode.

        Returns:
            Tuple of (TS, AH) - target temp and auto-heat flag
        """
        ts = self.get_state_value("TS")
        ah = self.get_state_value("AH")

        return (
            ts if isinstance(ts, float) else -1.0,
            ah if isinstance(ah, int) else -1,
        )
