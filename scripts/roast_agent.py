#!/usr/bin/env python3
"""AI Roastmaster — Claude controls your roast in real-time.

Instead of mechanically following a profile, Claude analyzes temperature
curves, RoR trends, and roast progression every ~5 seconds, making
autonomous decisions about heat, fan, event timing, and development.

Uses Claude Code CLI (`claude -p`) so it bills against your Max/Pro
subscription — no API key needed.

The human operator provides:
  - Physical actions (charging beans, moving to cooling tray)
  - Sensory input (hearing first crack, observing color)
  - Safety oversight (type "stop" or Ctrl+C for emergency stop)

Usage:
    # Test with simulator
    python scripts/roast_agent.py

    # Real hardware
    python scripts/roast_agent.py --driver kaleido
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.core.config import get_config
from maillard.core.events import BaseEvent, SafetyAlertEvent
from maillard.roasting.curve import DuplicateEventError
from maillard.core.heating import HeatingManager
from maillard.core.safety import SafetyMonitor
from maillard.db.models import Coffee, Profile, Roast, RoastStatus
from maillard.driver.base import RoastEvent
from maillard.mcp.monitoring import RoastMonitor
from maillard.mcp.state import ApplicationState, lifespan
from maillard.roasting.curve import CurveCollector
from maillard.roasting.session import RoastSession
from maillard.roasting.state import RoastStateMachine

# ── ANSI Colors ──────────────────────────────────────────────────────

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"

# ── Configuration ────────────────────────────────────────────────────

DECISION_INTERVAL = 5.0  # seconds between AI decisions
MAX_CURVE_POINTS = 60  # data points in context (~5 min at 5s intervals)
CHARGE_TEMP = 200.0  # preheat target (°C)
CLAUDE_TIMEOUT = 30  # seconds max for a claude -p call

COFFEE_INFO = {
    "name": "Kenya Moringa Nadira AA",
    "origin": "Kenya",
    "processing": "Washed",
    "batch_grams": 600,
}

ROAST_GOAL = {
    "level": "Full City",
    "method": "Pour-over",
    "flavor": "Chocolate, caramel, slight fruit sweetness",
    "drop_temp": 218,
    "time_range": "10-12",
}


# ── Prompt Template ──────────────────────────────────────────────────

SYSTEM_INSTRUCTIONS = f"""\
You are an expert coffee roastmaster controlling a Kaleido M10 roaster \
in real-time.

## Your Roast
- Coffee: {COFFEE_INFO["name"]} ({COFFEE_INFO["origin"]}, {COFFEE_INFO["processing"]})
- Batch: {COFFEE_INFO["batch_grams"]}g
- Target: {ROAST_GOAL["level"]} for {ROAST_GOAL["method"]}
- Flavor goal: {ROAST_GOAL["flavor"]}
- Approximate drop: ~{ROAST_GOAL["drop_temp"]}°C at ~{ROAST_GOAL["time_range"]} minutes

## Roasting Phases
1. Charge to Turning Point (~60-90s): BT drops from cold beans. Keep heat \
high (80-90%). Mark turning_point when BT stops falling and starts rising.
2. Turning Point to Dry End (~4-5min): Moisture evaporates. Gradually \
reduce heat. Declining RoR target: 10-12 C/min.
3. Dry End to First Crack (~7-9min): Maillard reactions. Mark dry_end \
~150C. Declining RoR: 7-9 C/min.
4. First Crack to Drop (~9-12min): Development. Reduce heat to 30-40%. \
RoR 4-6 C/min. DTR target: 22-25% for Full City.

## Key Principles
- DECLINING RoR: Rate of Rise must decrease throughout the roast.
- MOMENTUM: Reduce heat BEFORE you want RoR to drop (15-30s delay).
- FIRST CRACK: ~196-200C. You CANNOT detect this from data. ASK the \
operator when BT approaches ~190C.
- FULL CITY: Drop before second crack. BT ~215-220C.
- Make meaningful changes (5-15%). Tiny adjustments have no effect.

## Safety
- BT > 230C: set drop immediately
- RoR > 20 early or > 15 after dry end: reduce heat aggressively
- RoR negative after turning point: increase heat"""


RESPONSE_FORMAT = """\

## Your Response
Respond with ONLY valid JSON, no markdown fences, no other text:
{
  "reasoning": "1-2 sentence analysis of what you see and why you act",
  "heat": <int 0-100 or null for no change>,
  "fan": <int 0-100 or null for no change>,
  "event": <"turning_point" or "dry_end" or "fc_start" or null>,
  "ask": <question for operator or null>,
  "drop": <reason string to end roast, or null>
}"""


# ── Data Tracking ────────────────────────────────────────────────────


@dataclass
class DataPoint:
    elapsed: float
    bt: float
    et: float
    ror: float | None
    hp: int
    fc: int
    rc: int


@dataclass
class RoastTracker:
    """Tracks roast data for AI context."""

    points: list[DataPoint] = field(default_factory=list)
    events: list[tuple[str, float, float]] = field(default_factory=list)
    marked_events: set[str] = field(default_factory=set)
    actions: list[tuple[float, str]] = field(default_factory=list)
    operator_messages: list[str] = field(default_factory=list)
    pending_answers: list[str] = field(default_factory=list)
    drop_requested: bool = False
    drop_reason: str = ""

    def add_point(self, dp: DataPoint) -> None:
        self.points.append(dp)

    def add_event(self, name: str, elapsed: float, temp: float) -> None:
        self.events.append((name, elapsed, temp))

    def add_action(self, elapsed: float, desc: str) -> None:
        self.actions.append((elapsed, desc))
        # Keep last 20 actions
        if len(self.actions) > 20:
            self.actions = self.actions[-20:]

    def recent_points(self, n: int = MAX_CURVE_POINTS) -> list[DataPoint]:
        return self.points[-n:]


# ── Operator Input ───────────────────────────────────────────────────


class OperatorInput:
    """Non-blocking stdin reader via background thread."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._running = True
        self._loop = loop
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _reader(self) -> None:
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                text = line.strip()
                if text and self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._queue.put(text), self._loop
                    )
            except Exception:
                break

    async def get_nowait(self) -> str | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def wait_with_watchdog(self, safety: SafetyMonitor | None) -> str:
        """Wait for input, resetting safety watchdog every 10s."""
        while True:
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=10.0)
            except TimeoutError:
                if safety and safety.is_running:
                    safety.reset_watchdog()


# ── Formatting ───────────────────────────────────────────────────────


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def build_prompt(tracker: RoastTracker) -> str:
    """Build full prompt for Claude including state and instructions."""
    if not tracker.points:
        return SYSTEM_INSTRUCTIONS + "\n\nNo data yet." + RESPONSE_FORMAT

    cur = tracker.points[-1]
    ror_s = f"{cur.ror:.1f}" if cur.ror is not None else "N/A"

    sections = [SYSTEM_INSTRUCTIONS, ""]

    # Current state
    sections.append(
        f"## Current State [{fmt_time(cur.elapsed)}]\n"
        f"BT={cur.bt:.1f}C  ET={cur.et:.1f}C  RoR={ror_s}C/min  "
        f"HP={cur.hp}%  Fan={cur.fc}%  Drum={cur.rc}%"
    )

    # Curve history
    recent = tracker.recent_points()
    step = max(1, len(recent) // 20)
    curve_lines = ["\n## Curve"]
    shown = set()
    for i in range(0, len(recent), step):
        p = recent[i]
        r = f"{p.ror:.1f}" if p.ror is not None else "N/A"
        curve_lines.append(
            f"  {fmt_time(p.elapsed)} BT={p.bt:.1f} RoR={r} HP={p.hp} Fan={p.fc}"
        )
        shown.add(id(recent[i]))
    if recent and id(recent[-1]) not in shown:
        p = recent[-1]
        r = f"{p.ror:.1f}" if p.ror is not None else "N/A"
        curve_lines.append(
            f"  {fmt_time(p.elapsed)} BT={p.bt:.1f} RoR={r} HP={p.hp} Fan={p.fc}"
        )
    sections.append("\n".join(curve_lines))

    # Events
    if tracker.events:
        ev_parts = [
            f"{n} at {fmt_time(t)} ({temp:.1f}C)" for n, t, temp in tracker.events
        ]
        sections.append(f"\n## Events\n" + "\n".join(f"- {e}" for e in ev_parts))
        sections.append(
            f"\nAlready marked (do NOT suggest again): {', '.join(sorted(tracker.marked_events))}"
        )

    # Recent actions (so Claude sees what it previously decided)
    if tracker.actions:
        act_lines = [f"  {fmt_time(t)} {desc}" for t, desc in tracker.actions[-10:]]
        sections.append("\n## Recent AI Actions\n" + "\n".join(act_lines))

    # Operator messages
    if tracker.pending_answers:
        sections.append(
            "\n## Operator Response\n" + "\n".join(tracker.pending_answers)
        )
    elif tracker.operator_messages:
        sections.append(
            "\n## Operator Says\n" + "\n".join(tracker.operator_messages)
        )

    sections.append(RESPONSE_FORMAT)
    return "\n".join(sections)


def display_state_line(dp: DataPoint) -> None:
    ror_s = f"{dp.ror:5.1f}" if dp.ror is not None else "  N/A"
    print(
        f"  {DIM}{fmt_time(dp.elapsed)}  "
        f"BT={dp.bt:6.1f}  ET={dp.et:6.1f}  RoR={ror_s}  "
        f"HP={dp.hp:3}%  FC={dp.fc:3}%{RESET}"
    )


def display_ai(reasoning: str, actions: list[str]) -> None:
    if reasoning:
        for line in reasoning.strip().split("\n"):
            print(f"  {CYAN}AI: {line}{RESET}")
    for a in actions:
        print(f"  {BOLD}{GREEN}  > {a}{RESET}")


# ── Claude Code CLI ──────────────────────────────────────────────────


def parse_json_response(text: str) -> dict | None:
    """Extract JSON from Claude's response, handling code fences."""
    text = text.strip()

    # Direct parse
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(text)

    # Remove code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(match.group(1).strip())

    # Find JSON object anywhere in text
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(match.group(0))

    return None


async def call_claude(prompt: str) -> dict | None:
    """Call Claude Code CLI and return parsed JSON decision."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        print(f"  {RED}[Claude timed out]{RESET}")
        return None
    except FileNotFoundError:
        print(f"  {RED}[claude CLI not found — is Claude Code installed?]{RESET}")
        return None

    if proc.returncode != 0:
        err = stderr.decode().strip()[:100] if stderr else "unknown error"
        print(f"  {RED}[Claude error: {err}]{RESET}")
        return None

    raw = stdout.decode()

    # claude --output-format json wraps response in {"result": "..."}
    try:
        outer = json.loads(raw)
        response_text = outer.get("result", raw)
    except json.JSONDecodeError:
        response_text = raw

    return parse_json_response(response_text)


# ── Action Execution ─────────────────────────────────────────────────


async def execute_decision(
    decision: dict,
    driver,
    heating_manager: HeatingManager,
    session: RoastSession,
    tracker: RoastTracker,
    operator_input: OperatorInput,
    app_state: ApplicationState,
    safety: SafetyMonitor | None,
) -> list[str]:
    """Execute parsed AI decision. Returns list of display strings."""
    elapsed = session.elapsed_time or 0
    displays = []

    # Heat adjustment
    heat = decision.get("heat")
    if heat is not None and isinstance(heat, int) and 0 <= heat <= 100:
        await heating_manager.start(heat)
        await driver.set_heat(heat)
        displays.append(f"Heat -> {heat}%")
        tracker.add_action(elapsed, f"Heat -> {heat}%")

    # Fan adjustment
    fan = decision.get("fan")
    if fan is not None and isinstance(fan, int) and 0 <= fan <= 100:
        await driver.set_fan(fan)
        displays.append(f"Fan -> {fan}%")
        tracker.add_action(elapsed, f"Fan -> {fan}%")

    # Event marking
    event = decision.get("event")
    if event in ("turning_point", "dry_end", "fc_start"):
        if event in tracker.marked_events:
            displays.append(f"Event {event} already marked — skipped")
        else:
            ds = await driver.read_state()
            bt = ds.get("BT", 0.0)

            try:
                session.mark_event(event, temp=bt, time_seconds=elapsed)
            except DuplicateEventError:
                tracker.marked_events.add(event)
                displays.append(f"Event {event} already marked — skipped")
            else:
                tracker.marked_events.add(event)

                event_map = {
                    "turning_point": RoastEvent.TURNING_POINT,
                    "dry_end": RoastEvent.DRY_END,
                    "fc_start": RoastEvent.FC_START,
                }
                driver_ev = event_map.get(event)
                if driver_ev:
                    await driver.trigger_event(driver_ev)

                tracker.add_event(event, elapsed, bt)

                label = event.replace("_", " ").upper()
                displays.append(f"Event: {label} at {bt:.1f}C ({fmt_time(elapsed)})")
                tracker.add_action(elapsed, f"Marked {label} at {bt:.1f}C")

        # Persist to DB
        roast_id = session.roast_id
        if roast_id:
            async with app_state.session() as db:
                roast = await db.get(Roast, roast_id)
                if roast:
                    if event == "turning_point":
                        roast.turning_point_time = int(elapsed)
                        roast.turning_point_temp = bt
                    elif event == "dry_end":
                        roast.dry_end_time = int(elapsed)
                        roast.dry_end_temp = bt
                    elif event == "fc_start":
                        roast.fc_start_time = int(elapsed)
                        roast.fc_start_temp = bt
                    await db.commit()

    # Ask operator
    ask = decision.get("ask")
    if ask and isinstance(ask, str):
        print(f"\n  {YELLOW}{BOLD}>> CLAUDE ASKS: {ask}{RESET}")
        print(f"  {YELLOW}   Type your response and press Enter:{RESET}", flush=True)
        response = await operator_input.wait_with_watchdog(safety)
        displays.append(f'Operator: "{response}"')
        tracker.pending_answers.append(f"Q: {ask}\nA: {response}")
        tracker.add_action(elapsed, f"Asked operator, got: {response}")

    # Drop
    drop = decision.get("drop")
    if drop and isinstance(drop, str):
        tracker.drop_requested = True
        tracker.drop_reason = drop
        displays.append(f"DROP: {drop}")
        tracker.add_action(elapsed, f"DROP: {drop}")

    return displays


# ── Preheat ──────────────────────────────────────────────────────────


async def preheat(driver, heating_manager: HeatingManager, target: float) -> None:
    """Preheat roaster to target temperature (mechanical, no AI)."""
    print(f"\n  Preheating to {target}°C...")
    print("  Drum=50%, Fan=30%, Heat=100%")

    await driver.set_drum(50)
    await driver.set_fan(30)
    await driver.set_target_temp(target)
    await heating_manager.start(100)

    reduced = False
    while True:
        ds = await driver.read_state()
        bt = ds.get("BT", 0)
        et = ds.get("ET", 0)
        delta = target - bt
        bar_len = min(40, max(0, int(bt / target * 40)))
        bar = "=" * bar_len + " " * (40 - bar_len)

        print(
            f"\r  [{bar}] BT:{bt:5.1f}  ET:{et:5.1f}  D{delta:+5.1f}  ",
            end="",
            flush=True,
        )

        if not reduced and bt >= target - 15:
            await heating_manager.start(60)
            await driver.set_heat(60)
            reduced = True
            print("\n  Heat -> 60% (approaching target)")

        if bt >= target - 3:
            await heating_manager.start(30)
            await driver.set_heat(30)
            print("\n  Stabilizing...")
            await asyncio.sleep(5)
            ds = await driver.read_state()
            print(f"  Stable at BT={ds.get('BT', 0):.1f}°C")
            break

        await asyncio.sleep(2)


# ── AI Control Loop ──────────────────────────────────────────────────


async def run_ai_loop(
    driver,
    session: RoastSession,
    heating_manager: HeatingManager,
    safety: SafetyMonitor,
    tracker: RoastTracker,
    operator_input: OperatorInput,
    app_state: ApplicationState,
) -> None:
    """Claude makes roasting decisions every DECISION_INTERVAL seconds."""
    print(f"  {BOLD}{'─' * 52}{RESET}")
    print(
        f"  {BOLD}{'Time':>6}  {'BT':>7}  {'ET':>7}  "
        f"{'RoR':>6}  {'Heat':>5}  {'Fan':>4}{RESET}"
    )
    print(f"  {BOLD}{'─' * 52}{RESET}")

    while session.is_active and not tracker.drop_requested:
        # ── Read current state ───────────────────────────────
        ds = await driver.read_state()
        elapsed = session.elapsed_time or 0
        bt = ds.get("BT", 0.0)
        et = ds.get("ET", 0.0)
        ror = session.latest_ror
        hp = ds.get("HP", 0)
        fc_val = ds.get("FC", 0)
        rc = ds.get("RC", 0)

        dp = DataPoint(
            elapsed=elapsed, bt=bt, et=et, ror=ror,
            hp=hp, fc=fc_val, rc=rc,
        )
        tracker.add_point(dp)
        display_state_line(dp)

        # ── Safety checks ────────────────────────────────────
        if safety.is_running:
            safety.reset_watchdog()

        if bt > 230:
            print(f"\n  {RED}{BOLD}*** BT > 230°C — EMERGENCY STOP ***{RESET}")
            await heating_manager.stop()
            await driver.emergency_stop()
            return

        # ── Check operator input ─────────────────────────────
        msg = await operator_input.get_nowait()
        if msg:
            lower = msg.lower()
            if lower in ("stop", "e-stop", "emergency", "quit"):
                print(f"\n  {RED}{BOLD}*** OPERATOR EMERGENCY STOP ***{RESET}")
                await heating_manager.stop()
                await driver.emergency_stop()
                return
            tracker.operator_messages.append(msg)
            print(f"  {YELLOW}Operator: {msg}{RESET}")

        # ── Call Claude ──────────────────────────────────────
        prompt = build_prompt(tracker)
        decision = await call_claude(prompt)

        # Clear consumed messages
        tracker.operator_messages.clear()
        tracker.pending_answers.clear()

        if decision is None:
            print(f"  {DIM}(no AI response this cycle){RESET}")
            await asyncio.sleep(DECISION_INTERVAL)
            continue

        # ── Execute and display ──────────────────────────────
        reasoning = decision.get("reasoning", "")
        actions = await execute_decision(
            decision, driver, heating_manager, session, tracker,
            operator_input, app_state, safety,
        )

        display_ai(reasoning, actions)

        if tracker.drop_requested:
            print(f"\n  {BOLD}Claude says DROP — ending roast...{RESET}")
            break

        await asyncio.sleep(DECISION_INTERVAL)


# ── Main Roast Lifecycle ─────────────────────────────────────────────


async def run_roast(driver_type: str, state: ApplicationState) -> None:
    config = state.config

    # ── 1. Connect ────────────────────────────────────────────
    print(f"\n[1/6] Connecting ({driver_type})...")
    await state.connect_roaster(driver_type=driver_type)
    driver = state.driver
    assert driver is not None

    ds = await driver.read_state()
    print(f"  Connected. BT={ds.get('BT', 0):.1f}°C  ET={ds.get('ET', 0):.1f}°C")

    # ── 2. DB records ─────────────────────────────────────────
    print("\n[2/6] Setting up coffee and profile...")

    async with state.session() as db:
        coffee = Coffee(
            name=COFFEE_INFO["name"],
            origin=COFFEE_INFO["origin"],
            processing=COFFEE_INFO["processing"],
        )
        db.add(coffee)
        await db.commit()
        await db.refresh(coffee)
        coffee_id = coffee.id
        print(f"  Coffee: {coffee.name} ({coffee_id[:8]}...)")

    async with state.session() as db:
        # Phases are required by schema but Claude doesn't follow them mechanically —
        # these are reference waypoints only
        phases_json = json.dumps([
            {"name": "charge", "target_time_seconds": 0, "target_temp": CHARGE_TEMP,
             "heat_percent": 85, "fan_percent": 30, "drum_percent": 50},
            {"name": "turning_point", "target_time_seconds": 75, "target_temp": 165,
             "heat_percent": 80, "fan_percent": 30, "drum_percent": 50, "target_ror": 12.0},
            {"name": "dry_end", "target_time_seconds": 300, "target_temp": 150,
             "heat_percent": 60, "fan_percent": 40, "drum_percent": 50, "target_ror": 9.0},
            {"name": "fc_start", "target_time_seconds": 540, "target_temp": 198,
             "heat_percent": 40, "fan_percent": 50, "drum_percent": 50, "target_ror": 6.0},
            {"name": "drop", "target_time_seconds": 660, "target_temp": ROAST_GOAL["drop_temp"],
             "heat_percent": 30, "fan_percent": 50, "drum_percent": 50, "target_ror": 4.0},
        ])
        profile = Profile(
            name=f"{ROAST_GOAL['level']} {ROAST_GOAL['method']}",
            coffee_id=coffee_id,
            roast_level=ROAST_GOAL["level"],
            flavor_goal=ROAST_GOAL["flavor"],
            brew_method=ROAST_GOAL["method"],
            batch_size_grams=COFFEE_INFO["batch_grams"],
            phases=phases_json,
            charge_temp=CHARGE_TEMP,
            target_dtr=23.0,
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)
        profile_id = profile.id
        print(f"  Profile: {profile.name} ({profile_id[:8]}...)")

    # ── 3. Preheat ────────────────────────────────────────────
    print("\n[3/6] Preheating...")
    heating_manager = HeatingManager(driver)
    state._heating_manager = heating_manager

    machine = RoastStateMachine()
    machine.start_preheat(target_temp=CHARGE_TEMP)
    state.roast_session = RoastSession(
        machine=machine,
        collector=CurveCollector(),
        event_bus=state.event_bus,
    )

    await preheat(driver, heating_manager, CHARGE_TEMP)

    # ── 4. Charge ─────────────────────────────────────────────
    print("\n[4/6] Ready to CHARGE")
    ds = await driver.read_state()
    bt = ds.get("BT", 0)
    print(f"  BT: {bt:.1f}°C")
    print(f"  Drop {COFFEE_INFO['batch_grams']}g into the drum now.")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input("\n  >>> Press Enter when beans are IN: ")

    await driver.trigger_event(RoastEvent.CHARGE)
    charge_time = datetime.now(UTC)
    ds = await driver.read_state()
    charge_bt = ds.get("BT", bt)

    await heating_manager.start(85)
    await driver.set_heat(85)
    await driver.set_fan(30)

    async with state.session() as db:
        roast_record = Roast(
            profile_id=profile_id,
            coffee_id=coffee_id,
            batch_size_grams=COFFEE_INFO["batch_grams"],
            start_time=charge_time.isoformat(),
            charge_temp=charge_bt,
            status=RoastStatus.IN_PROGRESS,
        )
        db.add(roast_record)
        await db.commit()
        await db.refresh(roast_record)
        roast_id = roast_record.id

    session = RoastSession.start(
        event_bus=state.event_bus,
        charge_temp=charge_bt,
        roast_id=roast_id,
        coffee_id=coffee_id,
        profile_id=profile_id,
        batch_size_grams=COFFEE_INFO["batch_grams"],
        machine=machine,
    )
    state.roast_session = session
    state.roast_context.is_active = True
    state.roast_context.charge_time = charge_time.timestamp()
    state.roast_context.roast_id = roast_id
    state.roast_context.coffee_id = coffee_id
    state.roast_context.profile_id = profile_id

    print(
        f"\n  CHARGED at {charge_bt:.1f}°C — "
        f"{BOLD}AI roastmaster taking over!{RESET}"
    )

    # ── 5. Start monitors + AI loop ──────────────────────────
    print("\n[5/6] Claude is now controlling your roast.\n")
    print(f"  {DIM}(Type a message anytime — Claude will see it.")
    print(f"   Type 'stop' for emergency stop. Ctrl+C also works.){RESET}\n")

    monitor = RoastMonitor(
        driver=driver,
        event_bus=state.event_bus,
        session=session,
        poll_interval=1.5,
    )
    await monitor.start()
    state._monitor = monitor

    safety = SafetyMonitor(
        driver=driver,
        event_bus=state.event_bus,
        session=session,
        config=config,
    )
    await safety.start()
    state._safety_monitor = safety

    state.event_bus.on(BaseEvent, lambda e: (
        print(f"\n  {RED}{BOLD}*** SAFETY: {e.message} ***{RESET}")
        if isinstance(e, SafetyAlertEvent) else None
    ))

    operator_input = OperatorInput()
    operator_input.start(asyncio.get_running_loop())

    tracker = RoastTracker()
    tracker.add_event("charge", 0.0, charge_bt)

    try:
        await run_ai_loop(
            driver, session, heating_manager,
            safety, tracker, operator_input, state,
        )
    finally:
        operator_input.stop()

    # ── 6. Drop ───────────────────────────────────────────────
    print("\n[6/6] Executing DROP...")

    await heating_manager.stop()
    state._heating_manager = None
    await monitor.stop()
    state._monitor = None
    await safety.stop()
    state._safety_monitor = None

    await driver.trigger_event(RoastEvent.DROP)
    ds = await driver.read_state()
    drop_bt = ds.get("BT", 0)
    total_seconds = int(session.elapsed_time or 0)

    session.machine.drop(temp=drop_bt, time_seconds=float(total_seconds))
    session.machine.complete()
    session.collector.add_event("drop", float(total_seconds), drop_bt)

    curve_data = session.finalize(status="completed")
    dtr = session.collector.calculate_dtr(drop_time=float(total_seconds))

    async with state.session() as db:
        roast = await db.get(Roast, roast_id)
        if roast:
            roast.drop_time = total_seconds
            roast.drop_temp = drop_bt
            roast.end_time = datetime.now(UTC).isoformat()
            roast.total_time_seconds = total_seconds
            roast.curve_data = curve_data.model_dump_json()
            roast.dtr = dtr
            roast.status = RoastStatus.COMPLETED
            await db.commit()

    state.clear_roast_context()
    await driver.set_fan(100)

    print(f"\n{'=' * 56}")
    print(f"  {BOLD}{GREEN}ROAST COMPLETE!{RESET}")
    print(f"{'=' * 56}")
    print(f"  Coffee:     {COFFEE_INFO['name']}")
    print(f"  Charge:     {charge_bt:.1f}°C")
    print(f"  Drop:       {drop_bt:.1f}°C")
    print(f"  Total:      {fmt_time(total_seconds)}")
    if dtr:
        print(f"  DTR:        {dtr:.1f}%")
    if tracker.drop_reason:
        print(f"  AI reason:  {tracker.drop_reason}")
    print(f"  Samples:    {session.sample_count}")
    print(f"  Roast ID:   {roast_id}")
    print(f"{'=' * 56}")

    print("\n  Cooling at Fan=100%. Press Enter to disconnect.")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()
    await driver.disconnect()
    print("  Disconnected. Roast saved.")


# ── Entry Point ──────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Roastmaster — Claude controls your roast",
    )
    parser.add_argument(
        "--driver",
        choices=["simulator", "kaleido"],
        default="simulator",
        help="Roaster driver (default: simulator)",
    )
    args = parser.parse_args()

    # Verify claude CLI is available
    check = await asyncio.create_subprocess_exec(
        "claude", "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await check.communicate()
    if check.returncode != 0:
        print(f"{RED}Error: 'claude' CLI not found.{RESET}")
        print("Install Claude Code: https://docs.anthropic.com/en/docs/claude-code")
        sys.exit(1)
    claude_version = stdout.decode().strip()

    config = get_config()

    print(f"\n{'=' * 56}")
    print(f"  {BOLD}MAILLARD AI ROASTMASTER{RESET}")
    print(f"{'=' * 56}")
    print(f"  Coffee:  {COFFEE_INFO['name']}")
    print(f"  Target:  {ROAST_GOAL['level']} for {ROAST_GOAL['method']}")
    print(f"  Batch:   {COFFEE_INFO['batch_grams']}g")
    print(f"  Charge:  {CHARGE_TEMP}°C")
    print(f"  Claude:  {claude_version}")
    print(f"  Driver:  {args.driver}")
    if args.driver == "kaleido":
        print(f"  Host:    {config.kaleido_host}:{config.kaleido_port}")
    print(f"{'=' * 56}")

    async with lifespan() as state:
        # Start web dashboard
        web_task = None
        uv_server = None
        try:
            import uvicorn

            from maillard.web import create_app

            web_app = create_app(state)
            uv_config = uvicorn.Config(
                web_app,
                host=config.api_host,
                port=config.api_port,
                log_level="warning",
            )
            uv_config.setup_event_loop = lambda: None
            uv_server = uvicorn.Server(uv_config)
            web_task = asyncio.create_task(uv_server.serve())
            print(f"\n  Dashboard: http://{config.api_host}:{config.api_port}")
        except Exception as e:
            print(f"\n  (Dashboard failed: {e})")

        try:
            await run_roast(args.driver, state)
        except KeyboardInterrupt:
            print(f"\n\n  {RED}{BOLD}*** CTRL+C — EMERGENCY STOP ***{RESET}")
            if state.driver and state.driver.is_connected:
                if state._heating_manager:
                    await state._heating_manager.stop()
                await state.driver.emergency_stop()
                print("  Heat=0%, Fan=100%. Safe.")
        finally:
            if uv_server:
                uv_server.should_exit = True
            if web_task:
                web_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await web_task


if __name__ == "__main__":
    asyncio.run(main())
