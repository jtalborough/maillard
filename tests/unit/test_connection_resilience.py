"""Deterministic connection recovery and fresh-sample regression checks."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from maillard.core.config import MaillardConfig
from maillard.core.events import EventBus, SafetyAlertEvent, StateChangeEvent
from maillard.db.schemas import CurveDataSchema
from maillard.driver.kaleido import KaleidoDriver
from maillard.mcp.monitoring import RoastMonitor
from maillard.roasting.curve import CurveCollector
from maillard.roasting.session import RoastSession


def test_backoff_sequence_and_jitter():
    with patch('maillard.driver.kaleido.random.uniform', return_value=1):
        assert [KaleidoDriver._next_backoff(i) for i in range(7)] == [.5, 1, 2, 4, 5, 5, 5]
    for attempt, base in enumerate([.5, 1, 2, 4, 5]):
        assert all(base * .75 <= KaleidoDriver._next_backoff(attempt) <= base * 1.25
                   for _ in range(30))


@pytest.mark.asyncio
async def test_missed_replies_close_and_bt_reset():
    driver = KaleidoDriver(stall_close_seconds=0)
    driver._state = {'sid': 1}
    driver._is_connected = True
    driver._websocket = AsyncMock()
    driver._send_request = AsyncMock(return_value=None)
    await driver.read_state()
    await driver.read_state()
    assert driver.missed_replies == 2
    driver._websocket.close.assert_not_awaited()
    await driver._set_state('BT', '180', False)
    assert driver.missed_replies == 0
    for _ in range(3):
        await driver.read_state()
    driver._websocket.close.assert_awaited_once()
    assert driver.connection_events[-1]['type'] == 'forced_close'
    assert not driver.is_connected


@pytest.mark.asyncio
@pytest.mark.parametrize('interval,timeout', [(5, 10), (0, 0)])
async def test_keepalive_and_backoff_reset(interval, timeout):
    driver = KaleidoDriver(ping_interval=interval, ping_timeout=timeout)
    driver._running = True
    websocket = AsyncMock(close_code=1000, close_reason='done')
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            driver._running = False

    async def reads(_ws):
        return

    with patch('maillard.driver.kaleido.websockets.connect',
               AsyncMock(side_effect=[OSError('offline'), websocket])) as connect, \
         patch.object(driver, '_initialize_connection', AsyncMock()), \
         patch.object(driver, '_handle_reads', reads), \
         patch.object(driver, '_next_backoff', side_effect=lambda a: .5 * 2 ** a), \
         patch('maillard.driver.kaleido.asyncio.sleep', sleep):
        await driver._connection_loop()
    assert sleeps == [.5, .5]
    assert driver.reconnect_failures == 0
    assert connect.call_args.kwargs == {
        'open_timeout': 6, 'ping_interval': interval or None,
        'ping_timeout': timeout or None, 'close_timeout': 2,
    }
    assert [e['type'] for e in driver.connection_events].count('reconnect_failed') == 1


@pytest.mark.asyncio
async def test_link_states_and_continuous_fresh_clear():
    clock = [100.0]
    driver = SimpleNamespace(is_connected=True, last_bt_sample_time=100.0,
                             reconnect_failures=0, read_state=AsyncMock(return_value={'BT': 180}))
    bus = EventBus()
    alerts = []
    bus.on(SafetyAlertEvent, alerts.append)
    with patch('maillard.mcp.monitoring.time.monotonic', side_effect=lambda: clock[0]):
        monitor = RoastMonitor(driver, bus, SimpleNamespace(elapsed_time=0, current_phase=None,
                                                          latest_ror=None))
        clock[0] = 108
        await monitor._poll_once()
        assert monitor.link_state == 'degraded'
        assert not monitor.connection_lost
        await asyncio.sleep(0)
        assert not alerts
        clock[0] = 115
        await monitor._poll_once()
        assert monitor.connection_lost
        clock[0] = driver.last_bt_sample_time = 116
        await monitor._poll_once()
        clock[0] = 118
        await monitor._poll_once()  # stale flap resets clear window
        clock[0] = driver.last_bt_sample_time = 120
        await monitor._poll_once()
        for now in [121, 122, 123, 124]:
            clock[0] = driver.last_bt_sample_time = now
            await monitor._poll_once()
            assert monitor.connection_lost
        clock[0] = driver.last_bt_sample_time = 125
        await monitor._poll_once()
        assert monitor.link_state == 'ok'
        driver.is_connected = False
        await monitor._check_freshness()
        assert monitor.link_state == 'degraded'
        driver.reconnect_failures = 1
        await monitor._check_freshness()
        assert monitor.connection_lost
    await asyncio.sleep(0)
    assert [e.cleared for e in alerts] == [False, True, False]


def test_session_gaps_roundtrip_and_finalize():
    session = RoastSession.start(EventBus(), 180)
    def emit(elapsed, stale=False, connected=True):
        session.machine._charge_time = datetime.now(UTC) - timedelta(seconds=elapsed)
        session._on_state_change(StateChangeEvent(state={'BT': 180 + elapsed, 'ET': 200},
                                                 stale=stale, connected=connected))
    emit(0)
    emit(2, stale=True)
    emit(10, stale=True, connected=False)
    emit(22)
    assert session.sample_count == 2
    gap = session.collector.gaps[0]
    assert gap['start'] == pytest.approx(2, abs=.01)
    assert gap['end'] == pytest.approx(22, abs=.01)
    assert gap['duration'] == pytest.approx(20, abs=.01)
    assert gap['reason'] == 'disconnected'
    emit(25, stale=True)
    session.machine._charge_time = datetime.now(UTC) - timedelta(seconds=30)
    curve = session.finalize()
    assert curve.gaps[-1]['end'] == pytest.approx(30, abs=.01)
    assert curve.gaps[-1]['reason'] == 'stale'
    restored = CurveCollector.from_schema(CurveDataSchema.model_validate(curve.model_dump()))
    assert restored.gaps == curve.gaps
    assert restored.calculate_ror() == curve.bt_ror
    assert curve.bt[0] != curve.bt[1]
    assert CurveDataSchema.model_validate({'bt': [100]}).gaps == []


@pytest.mark.parametrize('mode', ['enabled', 'disabled', 'unwritable'])
def test_rotating_logs(tmp_path, mode):
    from logging.handlers import RotatingFileHandler

    from maillard.__main__ import _setup_logging

    path = tmp_path / 'logs' / 'maillard.log'
    if mode == 'unwritable':
        blocker = tmp_path / 'blocker'
        blocker.write_text('file')
        path = blocker / 'maillard.log'
    config = MaillardConfig(log_file='' if mode == 'disabled' else str(path))
    root = logging.getLogger()
    original = root.handlers[:]
    original_level = root.level
    root.handlers = []
    try:
        with patch('maillard.core.config.get_config', return_value=config):
            _setup_logging()
        files = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(files) == (1 if mode == 'enabled' else 0)
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        if files:
            assert files[0].maxBytes == 10 * 1024 * 1024
            assert files[0].backupCount == 5
            assert path.exists()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = original
        root.setLevel(original_level)


def test_ror_smoothing_and_detection_across_twenty_second_gap():
    collector = CurveCollector()
    times = list(range(0, 61, 2)) + list(range(80, 141, 2))
    for elapsed in times:
        collector.record_sample(elapsed, 100 + elapsed / 10, 150 + elapsed / 10,
                                heat=50, fan=30, drum=80)
    curve = collector.to_schema()
    assert curve.time_seconds == times
    assert all(b > a for a, b in zip(curve.bt, curve.bt[1:], strict=False))
    assert len(collector.smooth_bt()) == len(times)
    assert all(value == pytest.approx(6) for value in collector.calculate_ror(smoothing=False)
               if value is not None)
    collector.detect_turning_point()


@pytest.mark.asyncio
async def test_idle_read_timeout_probes_then_closes_after_three_misses():
    driver = KaleidoDriver()
    driver._running = True
    driver._is_connected = True
    driver._write_queue = asyncio.Queue()
    websocket = AsyncMock()
    websocket.recv.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await driver._handle_reads(websocket)
    # Two silent windows are probed with RD A0 before the third forces a reconnect.
    assert [driver._write_queue.get_nowait() for _ in range(2)] == [
        KaleidoDriver.encode_message("RD", "A0")] * 2
    websocket.close.assert_awaited_once()
    assert not driver.is_connected
    assert [e['type'] for e in driver.connection_events] == ['stall', 'forced_close']


@pytest.mark.asyncio
async def test_idle_read_timeout_reply_resets_misses():
    driver = KaleidoDriver()
    driver._running = True
    driver._is_connected = True
    driver._write_queue = asyncio.Queue()
    websocket = AsyncMock()
    replies = [TimeoutError(), "{0,BT:150.0,ET:180.0}", TimeoutError(), TimeoutError()]

    async def recv():
        item = replies.pop(0) if replies else asyncio.CancelledError()
        if isinstance(item, BaseException):
            raise item
        return item

    websocket.recv.side_effect = recv
    with pytest.raises(asyncio.CancelledError):
        await driver._handle_reads(websocket)
    websocket.close.assert_not_awaited()
    assert driver.is_connected
    assert driver.missed_replies == 2


@pytest.mark.asyncio
async def test_three_misses_reconnect_through_connection_loop():
    driver = KaleidoDriver(stall_close_seconds=0, forced_close_reconnect_delay=0)
    driver._running = True
    first, second = AsyncMock(), AsyncMock()
    closed = asyncio.Event()
    first.close.side_effect = closed.set
    driver._send_request = AsyncMock(return_value=None)
    inits = 0
    real_sleep = asyncio.sleep

    async def initialize(_ws, _unit):
        nonlocal inits
        inits += 1
        driver._state = {'sid': 1}
        if inits == 2:
            driver._running = False

    async def reads(ws):
        if ws is first:
            await closed.wait()

    async def sleep(_delay):
        return

    with patch('maillard.driver.kaleido.websockets.connect',
               AsyncMock(side_effect=[first, second])), \
         patch.object(driver, '_initialize_connection', initialize), \
         patch.object(driver, '_handle_reads', reads), \
         patch('maillard.driver.kaleido.asyncio.sleep', sleep):
        task = asyncio.create_task(driver._connection_loop())
        while not driver.is_connected:
            await real_sleep(0)
        for _ in range(3):
            await driver.read_state()
        await asyncio.wait_for(task, 1)
    assert inits == 2
    kinds = [event['type'] for event in driver.connection_events]
    assert kinds.count('connected') == 2
    assert kinds.count('forced_close') == 1
    assert driver.missed_replies == 0


@pytest.mark.asyncio
async def test_short_stall_keeps_socket_open():
    import time as _t
    driver = KaleidoDriver(stall_close_seconds=12)
    driver._state = {'sid': 1}
    driver._is_connected = True
    driver._websocket = AsyncMock()
    driver._send_request = AsyncMock(return_value=None)
    driver.last_bt_sample_time = _t.monotonic() - 5
    for _ in range(5):
        await driver.read_state()
    driver._websocket.close.assert_not_awaited()
    assert driver.is_connected
    driver.last_bt_sample_time = _t.monotonic() - 13
    await driver.read_state()
    driver._websocket.close.assert_awaited_once()
    assert driver._forced_close
