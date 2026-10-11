"""Unit tests for Kaleido driver message encoding/decoding and state parsing.

These tests verify the protocol-level functionality of the KaleidoDriver
without requiring an actual hardware connection.
"""

import asyncio

import pytest

from maillard.driver.base import (
    KaleidoTimingConstants,
    RoastEvent,
    is_float_var,
    is_int_var,
    is_str_var,
)
from maillard.driver.kaleido import KaleidoDriver


class TestMessageEncoding:
    """Test message encoding to Kaleido protocol format."""

    def test_encode_tag_only(self) -> None:
        """Test encoding a message with just a tag (no value)."""
        result = KaleidoDriver.encode_message("PI")
        assert result == "{[PI]}\n"

    def test_encode_tag_with_string_value(self) -> None:
        """Test encoding string variables."""
        result = KaleidoDriver.encode_message("TU", "C")
        assert result == "{[TU C]}\n"

        result = KaleidoDriver.encode_message("SC", "AR")
        assert result == "{[SC AR]}\n"

    def test_encode_tag_with_integer_value(self) -> None:
        """Test encoding integer variables (decimals removed)."""
        result = KaleidoDriver.encode_message("HP", "80")
        assert result == "{[HP 80]}\n"

        # Float input should be truncated to integer (using :.0f format)
        # Note: :.0f uses truncation, not rounding
        result = KaleidoDriver.encode_message("HP", "80.5")
        assert result == "{[HP 80]}\n"

        result = KaleidoDriver.encode_message("HP", "80.9")
        assert result == "{[HP 81]}\n"

        result = KaleidoDriver.encode_message("FC", "50.0")
        assert result == "{[FC 50]}\n"

    def test_encode_tag_with_float_value(self) -> None:
        """Test encoding float variables (one decimal, trailing zeros stripped)."""
        result = KaleidoDriver.encode_message("TS", "200.5")
        assert result == "{[TS 200.5]}\n"

        # Trailing zeros should be stripped
        result = KaleidoDriver.encode_message("TS", "200.0")
        assert result == "{[TS 200]}\n"

        result = KaleidoDriver.encode_message("BT", "185.30")
        assert result == "{[BT 185.3]}\n"

    def test_encode_non_numeric_value_passthrough(self) -> None:
        """Test that non-numeric values like 'UP' pass through unchanged."""
        # Some controls use string values like "UP" or "DW"
        result = KaleidoDriver.encode_message("HP", "UP")
        assert result == "{[HP UP]}\n"

    def test_encode_event_codes(self) -> None:
        """Test encoding event codes."""
        result = KaleidoDriver.encode_message("EV", "1")  # CHARGE
        assert result == "{[EV 1]}\n"

        result = KaleidoDriver.encode_message("EV", "9")  # DROP
        assert result == "{[EV 9]}\n"

    def test_encode_read_data_request(self) -> None:
        """Test encoding the read data request."""
        result = KaleidoDriver.encode_message("RD", "A0")
        assert result == "{[RD A0]}\n"


class TestMessageDecoding:
    """Test message decoding from Kaleido protocol format."""

    def test_decode_simple_status(self) -> None:
        """Test decoding a simple status response."""
        sid, values = KaleidoDriver.decode_message("{5}")
        assert sid == 5
        assert values == {"sid": 5}

    def test_decode_status_with_temperatures(self) -> None:
        """Test decoding response with temperature values."""
        message = "{5,BT:185.5,ET:220.3}"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["sid"] == 5
        assert values["BT"] == 185.5
        assert values["ET"] == 220.3

    def test_decode_full_state_response(self) -> None:
        """Test decoding a full state response with multiple variables."""
        message = "{5,BT:185.5,ET:220.3,AT:25.0,TS:200,HP:80,FC:50,RC:60,AH:1}"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["BT"] == 185.5
        assert values["ET"] == 220.3
        assert values["AT"] == 25.0
        assert values["TS"] == 200.0  # Float var
        assert values["HP"] == 80  # Int var
        assert values["FC"] == 50  # Int var
        assert values["RC"] == 60  # Int var
        assert values["AH"] == 1  # Int var

    def test_decode_string_values(self) -> None:
        """Test decoding string variable responses."""
        message = "{5,TU:C,SN:KL12345}"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["TU"] == "C"
        assert values["SN"] == "KL12345"

    def test_decode_safety_guard_response(self) -> None:
        """Test decoding safety guard responses."""
        message = "{5,SC:AR}"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["SC"] == "AR"

        message = "{5,CL:AR}"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["CL"] == "AR"

    def test_decode_invalid_message_empty(self) -> None:
        """Test decoding empty or invalid messages."""
        sid, values = KaleidoDriver.decode_message("")
        assert sid is None
        assert values == {}

        sid, values = KaleidoDriver.decode_message("  ")
        assert sid is None
        assert values == {}

    def test_decode_invalid_message_no_braces(self) -> None:
        """Test decoding messages without proper braces."""
        sid, values = KaleidoDriver.decode_message("5,BT:185")
        assert sid is None
        assert values == {}

        sid, values = KaleidoDriver.decode_message("{5")
        assert sid is None
        assert values == {}

    def test_decode_invalid_sid(self) -> None:
        """Test decoding with invalid sid."""
        sid, values = KaleidoDriver.decode_message("{abc}")
        assert sid is None
        assert values == {}

    def test_decode_strips_whitespace(self) -> None:
        """Test that whitespace is properly stripped."""
        message = "  {5,BT:185.5}  \n"
        sid, values = KaleidoDriver.decode_message(message)

        assert sid == 5
        assert values["BT"] == 185.5

    def test_decode_float_sid(self) -> None:
        """Test that float sid values are rounded to int."""
        sid, values = KaleidoDriver.decode_message("{5.7,BT:185}")
        assert sid == 6  # Rounded

        sid, values = KaleidoDriver.decode_message("{5.2,BT:185}")
        assert sid == 5  # Rounded


class TestVariableTypeClassification:
    """Test variable type classification functions."""

    def test_integer_vars(self) -> None:
        """Test that integer variables are correctly identified."""
        for var in ["sid", "HP", "FC", "RC", "AH", "HS", "EV", "CS"]:
            assert is_int_var(var), f"{var} should be int var"
            assert not is_str_var(var), f"{var} should not be str var"
            assert not is_float_var(var), f"{var} should not be float var"

    def test_string_vars(self) -> None:
        """Test that string variables are correctly identified."""
        for var in ["TU", "SC", "CL", "SN"]:
            assert is_str_var(var), f"{var} should be str var"
            assert not is_int_var(var), f"{var} should not be int var"
            assert not is_float_var(var), f"{var} should not be float var"

    def test_float_vars(self) -> None:
        """Test that float variables are correctly identified."""
        for var in ["BT", "ET", "AT", "TS"]:
            assert is_float_var(var), f"{var} should be float var"
            assert not is_int_var(var), f"{var} should not be int var"
            assert not is_str_var(var), f"{var} should not be str var"

    def test_unknown_vars_treated_as_float(self) -> None:
        """Test that unknown variables are treated as float."""
        assert is_float_var("UNKNOWN"), "Unknown vars should be float"
        assert is_float_var("NewVar"), "Unknown vars should be float"


class TestRoastEvent:
    """Test RoastEvent enum values."""

    def test_event_values(self) -> None:
        """Test that event codes have correct values."""
        assert RoastEvent.CHARGE == 1
        assert RoastEvent.TURNING_POINT == 2
        assert RoastEvent.DRY_END == 3
        assert RoastEvent.FC_START == 4
        assert RoastEvent.FC_END == 5
        assert RoastEvent.SC_START == 6
        assert RoastEvent.SC_END == 7
        assert RoastEvent.DROP == 9  # Note: NOT 8

    def test_drop_uses_ev_9(self) -> None:
        """Verify DROP uses EV 9, not EV 8.

        This is a critical test - the Kaleido protocol uses EV 9 for DROP.
        Event 8 is detected via sid flag bits, not sent as a command.
        """
        assert RoastEvent.DROP.value == 9, "DROP must use EV 9, not EV 8"


class TestTimingConstants:
    """Test timing constant values."""

    def test_timeout_values(self) -> None:
        """Test that timeout constants have expected values."""
        assert KaleidoTimingConstants.OPEN_TIMEOUT == 6.0
        assert KaleidoTimingConstants.INIT_TIMEOUT == 6.0
        assert KaleidoTimingConstants.PING_TIMEOUT == 1.2
        assert KaleidoTimingConstants.SEND_TIMEOUT == 0.6
        assert KaleidoTimingConstants.READ_TIMEOUT == 5.0
        assert KaleidoTimingConstants.BUTTON_TIMEOUT == 1.2

    def test_delay_values(self) -> None:
        """Test retry and reconnect delay values."""
        assert KaleidoTimingConstants.PING_RETRY_DELAY == 1.0
        assert KaleidoTimingConstants.RECONNECT_DELAY == 0.5

    def test_poll_interval(self) -> None:
        """Test polling interval matches Kaleido's 1.5s sampling."""
        assert KaleidoTimingConstants.POLL_INTERVAL == 1.5


class TestKaleidoDriverState:
    """Test KaleidoDriver state management."""

    def test_initial_state(self) -> None:
        """Test that driver starts with empty state."""
        driver = KaleidoDriver()
        assert driver.state == {}
        assert not driver.is_connected

    def test_get_state_value_unknown(self) -> None:
        """Test getting unknown state values."""
        driver = KaleidoDriver()

        # Special vars return None when unknown
        assert driver.get_state_value("sid") is None
        assert driver.get_state_value("TU") is None
        assert driver.get_state_value("SN") is None

        # Int vars return -1 when unknown
        assert driver.get_state_value("HP") == -1
        assert driver.get_state_value("FC") == -1

        # Float vars return -1.0 when unknown
        assert driver.get_state_value("BT") == -1.0
        assert driver.get_state_value("ET") == -1.0

    @pytest.mark.asyncio
    async def test_process_message_updates_state(self) -> None:
        """Test that processing messages updates internal state."""
        driver = KaleidoDriver()

        # Process a message
        await driver._process_message("{5,BT:185.5,HP:80,TU:C}")

        assert driver.get_state_value("sid") == 5
        assert driver.get_state_value("BT") == 185.5
        assert driver.get_state_value("HP") == 80
        assert driver.get_state_value("TU") == "C"

    @pytest.mark.asyncio
    async def test_set_state_type_conversion(self) -> None:
        """Test that state values are converted to correct types."""
        driver = KaleidoDriver()

        # Integer var - uses int(round(float(value))) which does banker's rounding
        # 80.5 rounds to 80 (even), 80.6 rounds to 81
        await driver._set_state("HP", "80.5", False)
        assert driver.get_state_value("HP") == 80  # Banker's rounding: .5 rounds to even

        await driver._set_state("HP", "80.6", False)
        assert driver.get_state_value("HP") == 81  # Rounds up

        # Float var
        await driver._set_state("BT", "185.5", False)
        assert driver.get_state_value("BT") == 185.5

        # String var
        await driver._set_state("TU", "C", False)
        assert driver.get_state_value("TU") == "C"

    def test_state_copy_returned(self) -> None:
        """Test that state property returns a copy."""
        driver = KaleidoDriver()
        state1 = driver.state
        state2 = driver.state

        assert state1 is not state2

    def test_reset_state(self) -> None:
        """Test that reset clears all state."""
        driver = KaleidoDriver()
        driver._state = {"BT": 185.5, "HP": 80}  # type: ignore[typeddict-item]
        driver._pending_requests = {"BT": asyncio.Event()}

        driver._reset_state()

        assert driver._state == {}
        assert driver._pending_requests == {}


class TestKaleidoDriverConvenienceMethods:
    """Test convenience methods for accessing state."""

    @pytest.mark.asyncio
    async def test_get_temperatures(self) -> None:
        """Test getting temperature tuple."""
        driver = KaleidoDriver()

        # Before any data
        bt, et, sid = driver.get_temperatures()
        assert (bt, et, sid) == (-1.0, -1.0, 0)

        # After receiving data
        await driver._process_message("{5,BT:185.5,ET:220.3}")
        bt, et, sid = driver.get_temperatures()
        assert bt == 185.5
        assert et == 220.3
        assert sid == 5

    @pytest.mark.asyncio
    async def test_get_controls(self) -> None:
        """Test getting control settings tuple."""
        driver = KaleidoDriver()

        # Before any data
        hp, fc, rc = driver.get_controls()
        assert (hp, fc, rc) == (-1, -1, -1)

        # After receiving data
        await driver._process_message("{5,HP:80,FC:50,RC:60}")
        hp, fc, rc = driver.get_controls()
        assert hp == 80
        assert fc == 50
        assert rc == 60

    @pytest.mark.asyncio
    async def test_get_pid_state(self) -> None:
        """Test getting PID state tuple."""
        driver = KaleidoDriver()

        # Before any data
        ts, ah = driver.get_pid_state()
        assert (ts, ah) == (-1.0, -1)

        # After receiving data
        await driver._process_message("{5,TS:200,AH:1}")
        ts, ah = driver.get_pid_state()
        assert ts == 200.0
        assert ah == 1


class TestKaleidoDriverValidation:
    """Test input validation for driver methods."""

    @pytest.mark.asyncio
    async def test_set_heat_validation(self) -> None:
        """Test heat power validation."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()  # Mock queue

        with pytest.raises(ValueError, match="Power must be 0-100"):
            await driver.set_heat(-1)

        with pytest.raises(ValueError, match="Power must be 0-100"):
            await driver.set_heat(101)

    @pytest.mark.asyncio
    async def test_set_fan_validation(self) -> None:
        """Test fan speed validation."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()  # Mock queue

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_fan(-1)

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_fan(101)

    @pytest.mark.asyncio
    async def test_set_drum_validation(self) -> None:
        """Test drum speed validation."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()  # Mock queue

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_drum(-1)

        with pytest.raises(ValueError, match="Speed must be 0-100"):
            await driver.set_drum(101)


class TestKaleidoDriverNotConnected:
    """Test error handling when driver is not connected."""

    @pytest.mark.asyncio
    async def test_send_message_not_connected(self) -> None:
        """Test that sending when not connected raises error."""
        driver = KaleidoDriver()

        with pytest.raises(ConnectionError, match="Not connected"):
            await driver._send_message("HP", "80")

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self) -> None:
        """Test that request when not connected raises error."""
        driver = KaleidoDriver()

        with pytest.raises(ConnectionError, match="Not connected"):
            await driver._send_request("RD", "A0", "BT")


class TestKaleidoDriverCallbacks:
    """Test callback functionality."""

    def test_callbacks_stored(self) -> None:
        """Test that callbacks are stored on initialization."""

        def on_connect() -> None:
            pass

        def on_disconnect() -> None:
            pass

        driver = KaleidoDriver(
            connected_callback=on_connect,
            disconnected_callback=on_disconnect,
        )

        assert driver._connected_callback is on_connect
        assert driver._disconnected_callback is on_disconnect

    def test_logging_flag(self) -> None:
        """Test logging flag is stored."""
        driver = KaleidoDriver(logging_enabled=True)
        assert driver._logging_enabled is True

        driver = KaleidoDriver(logging_enabled=False)
        assert driver._logging_enabled is False


class TestKaleidoDriverCommandQueuing:
    """Test command queuing behavior."""

    @pytest.mark.asyncio
    async def test_command_only_sent_on_change(self) -> None:
        """Test that commands are only sent when value changes."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        # Set initial state
        await driver._process_message("{5,HP:80}")

        # Setting same value should not queue
        await driver.set_heat(80)
        assert driver._write_queue.empty()

        # Setting different value should queue
        await driver.set_heat(85)
        assert not driver._write_queue.empty()
        message = await driver._write_queue.get()
        assert message == "{[HP 85]}\n"

    @pytest.mark.asyncio
    async def test_pid_mode_queuing(self) -> None:
        """Test PID mode command queuing."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        # Set initial state - PID off
        await driver._process_message("{5,AH:0}")

        # Enable PID should queue
        await driver.set_pid_mode(True)
        message = await driver._write_queue.get()
        assert message == "{[AH 1]}\n"

        # Update state
        await driver._process_message("{5,AH:1}")

        # Disable PID should queue
        await driver.set_pid_mode(False)
        message = await driver._write_queue.get()
        assert message == "{[AH 0]}\n"


class TestKaleidoDriverEmergencyStop:
    """Test emergency stop functionality."""

    @pytest.mark.asyncio
    async def test_emergency_stop_sends_commands(self) -> None:
        """Test that emergency stop sends HP=0 and FC=100."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        assert await driver.emergency_stop() is False

        # Commands queued without hardware confirmation
        msg1 = await driver._write_queue.get()
        msg2 = await driver._write_queue.get()

        assert msg1 == "{[HP 0]}\n"
        assert msg2 == "{[HS 0]}\n"
        assert await driver._write_queue.get() == "{[FC 100]}\n"


class TestKaleidoDriverEventTrigger:
    """Test event triggering."""

    @pytest.mark.asyncio
    async def test_trigger_charge_event(self) -> None:
        """Test triggering CHARGE event."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        await driver.trigger_event(RoastEvent.CHARGE)
        message = await driver._write_queue.get()
        assert message == "{[EV 1]}\n"

    @pytest.mark.asyncio
    async def test_trigger_drop_event(self) -> None:
        """Test triggering DROP event uses EV 9."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        await driver.trigger_event(RoastEvent.DROP)
        message = await driver._write_queue.get()
        assert message == "{[EV 9]}\n"  # Not EV 8!

    @pytest.mark.asyncio
    async def test_trigger_all_events(self) -> None:
        """Test triggering all event types."""
        driver = KaleidoDriver()
        driver._write_queue = asyncio.Queue()

        for event in RoastEvent:
            await driver.trigger_event(event)
            message = await driver._write_queue.get()
            assert message == f"{{[EV {event.value}]}}\n"
