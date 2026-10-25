"""Base interfaces and types for roaster drivers.

This module defines the abstract roaster interface and type definitions
for the Kaleido M10 roaster protocol.
"""

from enum import IntEnum
from typing import Final, Protocol, TypedDict


class RoastEvent(IntEnum):
    """Roast event codes sent with the Kaleido EV command.

    Community protocol lists EV 8 as Drop and EV 9 as Cool End. The wire
    code for DROP is ``kaleido_drop_event_code`` (default 8) so a machine
    that still expects 9 can be overridden without a code change.
    """

    CHARGE = 1
    TURNING_POINT = 2
    DRY_END = 3
    FC_START = 4
    FC_END = 5
    SC_START = 6
    SC_END = 7
    DROP = 8
    COOL_END = 9


class KaleidoState(TypedDict, total=False):
    """State variables from Kaleido M10.

    This TypedDict represents all state variables that can be received
    from or sent to the Kaleido M10 roaster.

    Variable Type Classification:
    - Integer vars: sid, HP, FC, RC, AH, HS, EV, CS
    - String vars: TU, SC, CL, SN
    - Float vars: All others (BT, ET, AT, TS, etc.)

    Note: The Kaleido protocol may include additional variables not
    explicitly defined here. Unknown variables are treated as float
    by default (see is_float_var). If you need to handle a specific
    new variable, add it to INT_VARS or STR_VARS as appropriate.
    """

    # Device status (integer, 0-10 per Artisan kaleido.py)
    # Note: Specific meaning of each status value is not documented
    # in the Artisan source. Do not branch on specific sid values
    # without verifying against actual hardware behavior.
    sid: int

    # Temperature unit (string: 'C' or 'F')
    TU: str

    # Temperatures (float, in configured unit)
    BT: float  # Bean temperature
    ET: float  # Environmental/exhaust temperature
    AT: float  # Ambient temperature
    TS: float  # Target temperature setpoint (for PID)

    # Control percentages (integer, 0-100)
    HP: int  # Heating power %
    FC: int  # Fan speed %
    RC: int  # Drum/rotor speed %

    # Operating modes (integer: 0=off, 1=on)
    AH: int  # Auto heating mode (PID)
    HS: int  # Heating state

    # Event and command state (integer)
    EV: int  # Last event code
    CS: int  # Command state

    # String identifiers
    SN: str  # Serial number
    SC: str  # Safety control (start guard response)
    CL: str  # Clear guard (disconnect response)


# Alias for backwards compatibility
RoasterState = KaleidoState


class KaleidoTimingConstants:
    """Protocol timing constants for Kaleido M10 communication.

    These values are derived from the Artisan kaleido.py implementation
    and represent optimal timeouts for reliable communication.
    """

    # Connection timeouts (seconds)
    OPEN_TIMEOUT: Final[float] = 6.0  # Connection open timeout
    INIT_TIMEOUT: Final[float] = 6.0  # Initialization timeout

    # Communication timeouts (seconds)
    PING_TIMEOUT: Final[float] = 1.2  # Ping response timeout
    SEND_TIMEOUT: Final[float] = 0.6  # Send timeout
    READ_TIMEOUT: Final[float] = 5.0  # Read timeout
    BUTTON_TIMEOUT: Final[float] = 1.2  # Button command timeout

    # Retry/reconnect delays (seconds)
    PING_RETRY_DELAY: Final[float] = 1.0  # Delay between ping retries
    RECONNECT_DELAY: Final[float] = 0.5  # Delay before reconnection attempt

    PING_INTERVAL: Final[float] = 5.0
    KEEPALIVE_TIMEOUT: Final[float] = 10.0

    # Default polling interval (seconds)
    POLL_INTERVAL: Final[float] = 1.5  # State polling interval


# Integer variables in Kaleido protocol
# Source: Artisan kaleido.py intVar() method
INT_VARS: Final[frozenset[str]] = frozenset({'sid', 'HP', 'FC', 'RC', 'AH', 'HS', 'EV', 'CS'})

# String variables in Kaleido protocol
# Source: Artisan kaleido.py strVar() method
STR_VARS: Final[frozenset[str]] = frozenset({'TU', 'SC', 'CL', 'SN'})

# Known float variables in Kaleido protocol (for documentation)
# Any variable not in INT_VARS or STR_VARS is treated as float
FLOAT_VARS: Final[frozenset[str]] = frozenset({'BT', 'ET', 'AT', 'TS'})


def is_int_var(var: str) -> bool:
    """Check if a variable is an integer type in the Kaleido protocol.

    Based on Artisan kaleido.py intVar() method.
    """
    return var in INT_VARS


def is_str_var(var: str) -> bool:
    """Check if a variable is a string type in the Kaleido protocol.

    Based on Artisan kaleido.py strVar() method.
    """
    return var in STR_VARS


def is_float_var(var: str) -> bool:
    """Check if a variable should be parsed as float in the Kaleido protocol.

    This returns True for any variable not in INT_VARS or STR_VARS,
    matching Artisan's kaleido.py behavior where floatVar() is defined as:
        return not intVar(var) and not strVar(var)

    WARNING: Unknown/new protocol variables will be treated as float by
    default. If the Kaleido firmware adds new integer variables, they
    should be added to INT_VARS to ensure correct parsing.

    For explicit type safety, use is_known_float_var() instead.
    """
    return not is_int_var(var) and not is_str_var(var)


def is_known_float_var(var: str) -> bool:
    """Check if a variable is a known float type in the Kaleido protocol.

    Unlike is_float_var(), this only returns True for explicitly known
    float variables (BT, ET, AT, TS). Use this when you need strict
    type checking and want to handle unknown variables separately.
    """
    return var in FLOAT_VARS


class RoasterDriver(Protocol):
    """Protocol defining the interface for roaster drivers.

    This protocol enables multiple driver implementations (real hardware,
    simulator) to be used interchangeably. All methods are async to
    support non-blocking I/O.

    While designed with the Kaleido M10 in mind, implementations should
    adapt the interface to their specific hardware. The state dict keys
    and control methods represent common roaster capabilities.
    """

    @property
    def is_connected(self) -> bool:
        """Check if the driver is connected to the roaster."""
        ...

    @property
    def state(self) -> KaleidoState:
        """Get the current roaster state.

        Returns a snapshot of all known state variables. Values that
        have not been received yet will be absent from the dict.
        """
        ...

    async def connect(self, host: str, port: int) -> None:
        """Connect to the roaster.

        Establishes connection and performs any required initialization
        handshake.

        Args:
            host: IP address or hostname of the roaster
            port: Port number

        Raises:
            ConnectionError: If connection cannot be established
            TimeoutError: If connection times out
        """
        ...

    async def disconnect(self) -> None:
        """Disconnect from the roaster.

        Performs any cleanup required before closing the connection.
        """
        ...

    async def set_heat(self, power: int) -> None:
        """Set heating power (0-100%).

        Args:
            power: Heating power percentage (0-100)

        Raises:
            ValueError: If power is not in range 0-100
        """
        ...

    async def set_fan(self, speed: int) -> None:
        """Set fan speed (0-100%).

        Args:
            speed: Fan speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
        """
        ...

    async def set_drum(self, speed: int) -> None:
        """Set drum rotation speed (0-100%).

        Args:
            speed: Drum speed percentage (0-100)

        Raises:
            ValueError: If speed is not in range 0-100
        """
        ...

    async def set_target_temp(self, temp: float) -> None:
        """Set the PID target temperature.

        Args:
            temp: Target temperature in Celsius (100-260)
        """
        ...

    async def set_pid_mode(self, enabled: bool) -> None:
        """Enable or disable PID auto-heating mode.

        When enabled, the roaster automatically adjusts heat power
        to reach and maintain the target temperature. When disabled,
        heat power is controlled manually.

        Args:
            enabled: True to enable PID, False for manual control
        """
        ...

    async def set_heating_state(self, enabled: bool) -> None:
        """Enable or disable the heating element.

        This controls whether the heater can produce heat at all,
        separate from the power level (HP). Must be enabled for
        any heating to occur.

        Args:
            enabled: True to enable heating, False to disable
        """
        ...

    async def trigger_event(self, event: RoastEvent) -> None:
        """Trigger a roast event marker.

        Records a roast milestone (charge, turning point, first crack, etc.)
        in the roaster's event log.

        Args:
            event: The roast event to trigger
        """
        ...

    async def emergency_stop(self) -> bool:
        """Execute emergency stop.

        Immediately sets heat to 0% and fan to 100% for rapid cooling.
        Returns True only when heat-off is confirmed; False or an exception
        means the operator must use the roaster controls.
        """
        ...

    async def read_state(self) -> KaleidoState:
        """Request and return current state from roaster.

        Sends a state request and waits for the response containing
        updated state variables.

        Returns:
            Current state snapshot after update
        """
        ...
