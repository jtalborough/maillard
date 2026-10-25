"""AI roast advisor — real-time suggestions during roasts.

Runs as a background asyncio task, calling Claude every ~8 seconds
during an active roast. Publishes AISuggestionEvent on the EventBus so
the web dashboard can display reasoning and action buttons.

Models are configured via MAILLARD_AI_LIVE_MODEL and MAILLARD_AI_PLAN_MODEL.

Two modes:
- **suggest**: Claude suggests, human applies via dashboard buttons.
- **auto**: Claude auto-applies heat/fan/drum; human confirms events and drop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import anthropic

from maillard.ai.calibration import format_calibration, get_calibration, load_calibration
from maillard.ai.history import format_history_for_prompt, load_roast_history
from maillard.core.config import get_config
from maillard.core.events import (
    AISuggestionEvent,
    AITranscriptEvent,
    DropSuggested,
    SafetyAlertEvent,
    SafetyAlertLevel,
)
from maillard.core.heating import HeatingManager
from maillard.roasting.curve import DuplicateEventError
from maillard.roasting.development import (
    DevCommand,
    DevelopmentController,
    DevSample,
    development_band,
    drop_target_c,
    plan_drop_conflict,
    predicted_fc_c,
)
from maillard.roasting.state import RoastPhase

if TYPE_CHECKING:
    from maillard.ai.history import RoastHistory
    from maillard.mcp.state import ApplicationState

_logger = logging.getLogger(__name__)

ANALYSIS_INTERVAL = 8.0  # seconds between analysis calls
MAX_CURVE_POINTS = 40  # data points in context
AUTO_FAIL_FALLBACK = 3
LINK_BLIP_MANUAL_S = 60.0
LINK_BLIP_AFTER_FC_S = 30.0
_FC_DENIAL = re.compile(
    r"\b(no|not|without|haven't|havent|hasn't|hasnt|didn't|didnt)\b.{0,40}\b(first crack|fc)\b"
    r"|\b(first crack|fc)\b.{0,24}\b(yet|not)\b"
    r"|\bno fc\b|\bfc yet\b|\bno first crack\b",
    re.IGNORECASE,
)


def message_contradicted(message: str, marked: set[str]) -> bool:
    """True when an operator note is already contradicted by a later marked event."""
    return "fc_start" in marked and _FC_DENIAL.search(message) is not None


def _clamp_control(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(value))))
    return None


class AIMode(Enum):
    OFF = auto()
    SUGGEST = auto()
    AUTO = auto()


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


SYSTEM_PROMPT = """\
You are an expert coffee roastmaster advising on a live Kaleido M10 roast.
You MUST set heat, fan AND drum every response. None of them is optional.

## Drum speed (Kaleido M10)
Drum speed changes heat transfer. Faster drum = more convective, more even contact
and a slight cooling effect (lower RoR, gentler surface heating); slower drum = more
conductive contact with the hot drum (higher RoR, more risk of tipping/scorching).
Usually hold drum steady; change it in small steps (5-10%) to fine-tune RoR or
evenness, e.g. raise drum into Maillard/FC to soften a rising RoR without a big heat cut.
Keep drum within 30-80% unless the plan says otherwise. Repeat the current drum value
if no change is needed.

## Charge and pace
At charge BT reads the preheated drum, then falls to TP. Maintain the operator's
charge settings through TP unless a safety issue occurs. Use the batch calibration
below and the locked plan for pace; do not prescribe fixed heat percentages.
Declining RoR is desirable after TP. Account for response lag before adjusting.
Ask about audible FC once near the calibrated FC temperature if not already marked.
After FC is marked focus on development, DTR and drop.

## Operator Event Authority
- When the operator marks ANY event, trust it unconditionally.
- NEVER question, second-guess, or call "premature" any operator-marked event.
- After an event is marked, use the ACTUAL marked temperature and time in your reasoning.

## Safety
- BT > 230C: suggest drop immediately.
- RoR > 20 early or > 15 after dry end: reduce heat aggressively.
- RoR negative after TP: increase heat immediately.
- From about 5C before the predicted FC temperature, step heat down so RoR declines.
- After FC is marked, heat may only stay the same or decrease. Never raise heat after first crack.
- The drop target is the calibrated temperature for the roast level. A plan drop temperature that conflicts with it is rejected.

## Past Roast Context
If "Past Roasts" is present, compare current pace to the best past roast.
Reference specific metrics (DTR, drop temp, phase times) in your reasoning.

## What NOT To Ask
- NEVER ask about batch weight, bean movement, or mechanical status.
- NEVER ask the same question twice.
- NEVER mention first crack if fc_start is already marked.
- ONLY ask about first crack (once, when conditions met) or drop confirmation.

## Response Format
Respond with ONLY valid JSON, no markdown fences:
{
  "reasoning": "1-2 sentence analysis of current state and what you're adjusting",
  "heat": <int 0-100>,
  "fan": <int 0-100>,
  "drum": <int 0-100>,
  "event": <"dry_end" or null>,
  "ask": <question for operator or null>,
  "drop": <true to suggest ending roast, or false>
}

Note: turning_point and fc_start are detected automatically — do not suggest them as events."""


class RoastAIAgent:
    """Background task calling Claude every ~8s during active roast."""

    def __init__(
        self, state: ApplicationState, client: anthropic.AsyncAnthropic, *, model: str | None = None
    ) -> None:
        self._locked_plan: dict[str, Any] | None = None
        self._plan_roast_id: str | None = None
        self._state = state
        self._client = client
        config = getattr(state, "config", None) or get_config()
        self._model = model or config.ai_live_model
        self._plan_model = config.ai_plan_model
        self._mode = AIMode.OFF
        self._task: asyncio.Task[None] | None = None
        self._marked_events: set[str] = set()
        self._last_suggestion: dict[str, Any] | None = None
        self._bean_name: str | None = None
        self._roast_level: str | None = None
        self._batch_grams: int | None = None
        # Ask deduplication
        self._last_ask_text: str | None = None
        self._last_ask_time: float = 0.0
        self._last_phase: RoastPhase | None = None
        # One-shot roast level resolution from profile DB
        self._roast_level_resolved: bool = False
        # Roast history state
        self._pending_coffee_id: str | None = None
        self._history_resolved: bool = False
        self._roast_history: RoastHistory | None = None
        self._plan_generated: bool = False
        self._transcript: list[dict[str, Any]] = []
        self._operator_messages: list[dict[str, Any]] = []
        self._last_prompt_snapshot: dict[str, Any] | None = None
        self._auto_fail_count = 0
        self._fallback_reason: str | None = None
        self._link_paused = False
        self._link_paused_at: float | None = None
        self._resume_blocked = False
        self._last_controls: dict[str, int] = {}
        self._development = DevelopmentController()
        self._guard_roast_id: str | None = None
        self._auto_drop_sent = False

    @property
    def mode(self) -> AIMode:
        return self._mode

    @property
    def status_mode(self) -> str:
        if self._mode == AIMode.AUTO and self._link_paused:
            return "auto-paused"
        return self._mode.name.lower()

    @property
    def last_suggestion(self) -> dict[str, Any] | None:
        return self._last_suggestion

    def set_roast_context(
        self,
        bean_name: str | None = None,
        roast_level: str | None = None,
        batch_grams: int | None = None,
    ) -> None:
        """Set bean/roast info for AI context."""
        self._bean_name = bean_name
        self._roast_level = roast_level
        self._batch_grams = batch_grams

    @property
    def auto_control_enabled(self) -> bool:
        """AI auto control (AI writes heat/fan) requires an explicit opt-in flag."""
        return bool(getattr(self._state.config, "ai_auto_control_enabled", False))

    async def set_mode(self, mode: AIMode) -> None:
        """Switch AI mode. Starts/stops the background loop as needed.

        Raises:
            PermissionError: If AUTO is requested while
                MAILLARD_AI_AUTO_CONTROL_ENABLED is false.
        """
        if mode == AIMode.AUTO and not self.auto_control_enabled:
            raise PermissionError(
                "AI auto control is disabled (set MAILLARD_AI_AUTO_CONTROL_ENABLED=true)"
            )
        old = self._mode
        self._mode = mode
        if mode == AIMode.AUTO:
            self._auto_fail_count = 0
            self._fallback_reason = None
            self._resume_blocked = False
            await self._clear_fallback_alert()

        if mode == AIMode.OFF:
            await self.stop()
        elif old == AIMode.OFF:
            await self.start()

    async def notify_operator_override(
        self,
        *,
        heat: int | None = None,
        fan: int | None = None,
        drum: int | None = None,
    ) -> bool:
        if self._mode != AIMode.AUTO and not self._link_paused:
            return False
        if heat is not None:
            self._last_controls["heat"] = heat
        if fan is not None:
            self._last_controls["fan"] = fan
        if drum is not None:
            self._last_controls["drum"] = drum
        parts = []
        if heat is not None:
            parts.append(f"heat={heat}%")
        if fan is not None:
            parts.append(f"fan={fan}%")
        if drum is not None:
            parts.append(f"drum={drum}%")
        message = "Operator adjusted " + ", ".join(parts or ["controls"])
        entry = self._record_transcript("operator", "action", message)
        with contextlib.suppress(Exception):
            await self._publish_transcript_entry(entry)
        return True

    def note_charge(self, roast_id: str | None) -> None:
        """A new charge may resume auto after a previous drop or emergency stop."""
        self._resume_blocked = False
        self._link_paused = False
        self._link_paused_at = None
        self._auto_drop_sent = False
        if roast_id != self._guard_roast_id:
            self._guard_roast_id = roast_id
            self._development.reset()

    def block_auto_resume(self) -> None:
        """Drop and emergency stop outrank a pending auto resume."""
        self._resume_blocked = True
        self._link_paused = False
        self._link_paused_at = None

    async def _fallback_from_auto(
        self,
        reason: str,
        *,
        stop_loop: bool = False,
        level: SafetyAlertLevel = SafetyAlertLevel.WARNING,
    ) -> None:
        if self._mode != AIMode.AUTO and not self._link_paused:
            return
        self._fallback_reason = reason
        self._mode = AIMode.OFF
        self._link_paused = False
        self._link_paused_at = None
        self._resume_blocked = True
        _logger.warning("AI AUTO falling back to manual: %s", reason)
        monitor = getattr(self._state, "_safety_monitor", None)
        if monitor is not None:
            with contextlib.suppress(Exception):
                monitor.set_watchdog_enforced(False)
        message = f"AI auto control stopped — fell back to manual. {reason}"
        with contextlib.suppress(Exception):
            await self._state.event_bus.publish(
                SafetyAlertEvent(
                    level=level,
                    message=message,
                    condition="ai_auto_fallback",
                    cleared=False,
                )
            )
        entry = self._record_transcript("system", "action", message)
        with contextlib.suppress(Exception):
            await self._publish_transcript_entry(entry)
        if stop_loop:
            await self.stop()

    def set_pending_coffee_id(self, coffee_id: str | None) -> None:
        """Set the coffee ID for roast history lookup (called at charge)."""
        self._pending_coffee_id = coffee_id

    def _mid_roast(self) -> bool:
        if getattr(self._state, "is_roast_active", False):
            return True
        session = getattr(self._state, "roast_session", None)
        return bool(session is not None and getattr(session, "is_active", False))

    def _reset_memory(self) -> None:
        self._marked_events.clear()
        self._last_ask_text = None
        self._last_ask_time = 0.0
        self._last_phase = None
        self._roast_level_resolved = False
        self._history_resolved = False
        self._roast_history = None
        self._plan_generated = False
        self._locked_plan = None
        self._plan_roast_id = None
        self._transcript.clear()
        self._operator_messages.clear()
        self._last_prompt_snapshot = None
        self._last_controls.clear()
        self._development.reset()
        self._guard_roast_id = None
        self._auto_drop_sent = False

    async def start(self) -> None:
        """Begin analysis loop. A restart during a roast keeps the transcript and plan."""
        if self._task is not None and not self._task.done():
            return
        if not self._mid_roast():
            self._reset_memory()
        self._task = asyncio.create_task(self._loop())
        _logger.info("AI agent started (mode=%s)", self._mode.name)

    async def stop(self) -> None:
        """Cancel background task."""
        self._resume_blocked = True
        self._link_paused = False
        self._link_paused_at = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._mode = AIMode.OFF
        _logger.info("AI agent stopped")

    async def apply_suggestion(self, suggestion: dict[str, Any]) -> dict[str, Any]:
        """Apply a suggestion from the dashboard (confirm button).

        Returns dict describing what was applied.
        """
        result: dict[str, Any] = {"applied": []}
        state = self._state
        driver = state.driver

        if driver is None or not driver.is_connected:
            return {"error": "Roaster not connected"}

        heat = _clamp_control(suggestion.get("heat"))
        if heat is not None:
            if heat > 0:
                if state._heating_manager is None:
                    state._heating_manager = HeatingManager(driver)
                await state._heating_manager.start(heat)
            else:
                if state._heating_manager is not None:
                    await state._heating_manager.stop()
                    state._heating_manager = None
            await driver.set_heat(heat)
            result["applied"].append(f"heat={heat}%")

        fan = _clamp_control(suggestion.get("fan"))
        if fan is not None:
            await driver.set_fan(fan)
            result["applied"].append(f"fan={fan}%")

        drum = _clamp_control(suggestion.get("drum"))
        if drum is not None:
            await driver.set_drum(drum)
            result["applied"].append(f"drum={drum}%")

        # Apply event
        event = suggestion.get("event")
        if event and state.roast_session is not None:
            ds = driver.state
            bt = ds.get("BT", 0.0)
            elapsed = state.roast_session.elapsed_time or 0
            try:
                state.roast_session.mark_event(event, temp=bt, time_seconds=elapsed)
                self._marked_events.add(event)
                result["applied"].append(f"event={event}")
            except (DuplicateEventError, Exception) as e:
                result["event_error"] = str(e)

        return result

    async def record_suggestion_action(
        self,
        action: str,
        suggestion: dict[str, Any] | None = None,
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an operator action for the current suggestion transcript."""
        allowed = {"accepted", "rejected", "dismissed", "applied"}
        if action not in allowed:
            return {"error": f"Invalid action: {action}"}

        parts = [action.capitalize()]
        suggestion = suggestion or {}
        if suggestion.get("heat") is not None:
            parts.append(f"heat {suggestion['heat']}%")
        if suggestion.get("fan") is not None:
            parts.append(f"fan {suggestion['fan']}%")
        if suggestion.get("drum") is not None:
            parts.append(f"drum {suggestion['drum']}%")
        if suggestion.get("event"):
            parts.append(f"event {suggestion['event']}")
        if suggestion.get("drop"):
            parts.append("drop recommendation")
        if result and result.get("applied"):
            parts.append(f"({', '.join(result['applied'])})")

        entry = self._record_transcript(
            "operator",
            "action",
            " - ".join(parts),
            suggestion=suggestion or None,
            metadata={"action": action, "result": result or {}},
        )
        await self._publish_transcript_entry(entry)
        return {"entry": entry}

    def get_transcript(self) -> list[dict[str, Any]]:
        """Return transcript entries for the current agent session."""
        return list(self._transcript)

    def get_prompt_snapshot(self) -> dict[str, Any]:
        """Return the latest prompt/context snapshot visible to the operator."""
        if self._last_prompt_snapshot is None:
            return {
                "available": False,
                "model": self._model,
                "interval_seconds": ANALYSIS_INTERVAL,
                "message": "No live AI prompt has been built yet.",
            }
        return {"available": True, **self._last_prompt_snapshot}

    async def add_operator_message(self, message: str) -> dict[str, Any]:
        """Add an operator observation/question to future AI prompt context."""
        text = " ".join(message.strip().split())
        if not text:
            return {"error": "Message cannot be empty"}
        if len(text) > 1000:
            return {"error": "Message must be 1000 characters or fewer"}

        item = {
            "timestamp": datetime.now(UTC).isoformat(),
            "message": text,
        }
        self._operator_messages.append(item)
        self._operator_messages = self._operator_messages[-20:]

        entry = self._record_transcript("operator", "message", text)
        await self._publish_transcript_entry(entry)
        return {"entry": entry}

    def _record_transcript(
        self,
        role: str,
        kind: str,
        content: str,
        *,
        suggestion: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": str(uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "role": role,
            "kind": kind,
            "content": content,
            "suggestion": suggestion,
            "metadata": metadata or {},
        }
        self._transcript.append(entry)
        self._transcript = self._transcript[-200:]
        return entry

    async def _publish_transcript_entry(self, entry: dict[str, Any]) -> None:
        await self._state.event_bus.publish(
            AITranscriptEvent(
                role=entry["role"],
                kind=entry["kind"],
                content=entry["content"],
                entry_id=entry["id"],
                suggestion=entry.get("suggestion"),
                metadata=entry.get("metadata") or {},
            )
        )

    async def _resolve_roast_level(self) -> None:
        """One-shot: resolve roast level from profile DB if not set."""
        if self._roast_level_resolved or self._roast_level:
            return
        self._roast_level_resolved = True

        session = self._state.roast_session
        if session is None or session.profile_id is None:
            return

        try:
            from maillard.db.repository import ProfileRepository

            profile_repo = ProfileRepository()
            profile = await profile_repo.get_by_id(session.profile_id)
            if profile and profile.roast_level:
                self._roast_level = profile.roast_level
                _logger.info("Resolved roast level from profile: %s", self._roast_level)
        except Exception:
            _logger.debug("Failed to resolve roast level from profile", exc_info=True)

    async def _resolve_roast_history(self) -> None:
        """One-shot: load past roast history for the current coffee."""
        await load_calibration()
        session = self._state.roast_session
        roast_id = getattr(session, "roast_id", None)
        if roast_id and roast_id != self._plan_roast_id:
            try:
                from maillard.db.models import Roast, RoastPlan

                roast = await Roast.get(roast_id)
                plan = await RoastPlan.get(roast.plan_id) if roast and roast.plan_id else None
                self._locked_plan = plan.model_dump() if plan and plan.locked_at else None
                self._plan_roast_id = roast_id
            except Exception:
                _logger.warning("Locked plan unavailable")
        if self._history_resolved:
            return
        coffee_id = self._pending_coffee_id
        if coffee_id is None:
            # Also try from the roast session
            session = self._state.roast_session
            if session is not None and session.coffee_id is not None:
                coffee_id = session.coffee_id
        if coffee_id is None:
            return
        self._history_resolved = True

        try:
            history = await load_roast_history(coffee_id)
            self._roast_history = history
            if history and history.past_roasts:
                _logger.info(
                    "Loaded %d past roasts for %s (best cupping: %s)",
                    len(history.past_roasts),
                    history.coffee_name,
                    history.best_roast.cupping_avg_total if history.best_roast else "N/A",
                )
            else:
                _logger.info("No past roasts found for coffee %s", coffee_id)

            # Generate pre-roast plan
            if not self._plan_generated:
                await self._generate_pre_roast_plan()
        except Exception:
            _logger.warning("Failed to resolve roast history", exc_info=True)

    async def _generate_pre_roast_plan(self) -> None:
        """One-shot: generate a pre-roast plan based on history or coffee characteristics."""
        if self._plan_generated:
            return
        self._plan_generated = True

        from maillard.ai.history import format_coffee_characteristics

        history = self._roast_history
        level = self._roast_level or "Medium"

        # Build the plan prompt
        sections = [format_calibration(self._batch_grams or 1200)]
        if self._locked_plan:
            sections.append(
                "Explain this locked plan; preserve its targets: "
                + str(self._locked_plan["phases"])
            )
        if not history:
            return  # No coffee info — skip plan

        # Always include coffee characteristics
        sections.append(format_coffee_characteristics(history))

        if history.past_roasts:
            past_text = format_history_for_prompt(history)
            if past_text:
                sections.append(past_text)
            sections.append(
                f"\nCreate a roast plan for {history.coffee_name} targeting {level}.\n"
                "Using both the coffee characteristics AND past roast data above, recommend:\n"
                "1. Target phase times (charge→TP, TP→dry end, dry end→FC, FC→drop)\n"
                "2. Target drop temp and DTR%\n"
                "3. Heat/fan/drum strategy per phase\n"
                "4. What to do differently vs the best past roast (if anything)\n"
                "Keep it concise — 3-5 bullet points max."
            )
        else:
            sections.append(
                f"\nThis is the FIRST roast of this coffee. Target: {level}.\n"
                "Based on the coffee characteristics above, recommend:\n"
                "1. Target phase times and drop temp/DTR%\n"
                "2. Heat/fan/drum strategy per phase\n"
                "3. Key things to watch for given origin/processing/altitude\n"
                "Keep it concise — 3-5 bullet points max."
            )

        prompt_text = "\n".join(sections)

        try:
            response = await self._client.messages.create(
                model=self._plan_model,
                max_tokens=512,
                system="You are an expert coffee roastmaster. Give a concise pre-roast plan based on the data provided. Do NOT ask for additional information — use what is given.",
                messages=[{"role": "user", "content": prompt_text}],
            )
            plan_text = response.content[0].text if response.content else ""
            if plan_text:
                entry = self._record_transcript("ai", "plan", f"Pre-roast plan:\n{plan_text}")
                event = AISuggestionEvent(reasoning=f"Pre-roast plan:\n{plan_text}")
                await self._state.event_bus.publish(event)
                await self._publish_transcript_entry(entry)
                _logger.info("Published pre-roast plan")
        except Exception:
            _logger.warning("Failed to generate pre-roast plan", exc_info=True)

    async def generate_post_roast_comparison(self, metrics: dict[str, Any]) -> None:
        """One-shot: generate post-roast comparison after drop.

        Called as a fire-and-forget task from the drop route.

        Args:
            metrics: Dict with drop_temp, total_time_seconds, dtr, fc_start_time, etc.
        """
        from maillard.ai.history import format_comparison_data

        comparison_text = format_comparison_data(metrics, self._roast_history)

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=512,
                system="You are an expert coffee roastmaster. Analyze this roast vs past roasts.",
                messages=[{"role": "user", "content": comparison_text}],
            )
            analysis = response.content[0].text if response.content else ""
            if analysis:
                entry = self._record_transcript(
                    "ai", "analysis", f"Post-roast analysis:\n{analysis}"
                )
                event = AISuggestionEvent(reasoning=f"Post-roast analysis:\n{analysis}")
                await self._state.event_bus.publish(event)
                await self._publish_transcript_entry(entry)
                _logger.info("Published post-roast comparison")
        except Exception:
            _logger.warning("Failed to generate post-roast comparison", exc_info=True)

    def _prune_operator_messages(self) -> None:
        if "fc_start" not in self._marked_events:
            return
        self._operator_messages = [
            item
            for item in self._operator_messages
            if not message_contradicted(str(item.get("message", "")), self._marked_events)
        ]

    def _previous_decisions(self) -> str:
        lines: list[str] = []
        for entry in reversed(self._transcript):
            if entry.get("role") != "ai":
                continue
            suggestion = entry.get("suggestion") or {}
            bits = []
            if suggestion.get("heat") is not None:
                bits.append(f"heat {suggestion['heat']}%")
            if suggestion.get("fan") is not None:
                bits.append(f"fan {suggestion['fan']}%")
            if suggestion.get("drum") is not None:
                bits.append(f"drum {suggestion['drum']}%")
            if not bits and not entry.get("content"):
                continue
            text = str(entry.get("content") or "").replace("\n", " ")[:160]
            lines.append(f"- {', '.join(bits) or 'note'}: {text}")
            if len(lines) == 4:
                break
        if not lines:
            return ""
        return "## Previous decisions\n" + "\n".join(reversed(lines))

    def _dedup_ask(self, decision: dict[str, Any]) -> None:
        """Suppress duplicate ask questions within 60s. Reset on phase change."""
        session = self._state.roast_session
        current_phase = session.current_phase if session else None

        # Phase changed — reset dedup state
        if current_phase != self._last_phase:
            self._last_phase = current_phase
            self._last_ask_text = None
            self._last_ask_time = 0.0

        ask = decision.get("ask")
        if ask is None:
            return

        # Suppress FC questions after FC is already marked
        if "fc_start" in self._marked_events and "crack" in ask.lower():
            decision["ask"] = None
            return

        now = time.monotonic()
        if ask == self._last_ask_text and (now - self._last_ask_time) < 60.0:
            decision["ask"] = None
            return

        self._last_ask_text = ask
        self._last_ask_time = now

    def _sync_marked_events(self) -> None:
        """Sync _marked_events from the session's machine state.

        Events marked via dashboard buttons (not AI suggestions) don't go
        through apply_suggestion, so _marked_events would miss them. This
        syncs from the authoritative source each cycle.
        """
        session = self._state.roast_session
        if session is None:
            return
        machine = session.machine
        if machine.turning_point_time is not None:
            self._marked_events.add("turning_point")
        if machine.dry_end_time is not None:
            self._marked_events.add("dry_end")
        if machine.fc_start_time is not None:
            self._marked_events.add("fc_start")
        if machine.fc_end_time is not None:
            self._marked_events.add("fc_end")
        if machine.sc_start_time is not None:
            self._marked_events.add("sc_start")

    def _auto_detect_turning_point(self) -> None:
        """Auto-detect and mark the turning point from curve data.

        TP is the minimum BT after charge. We detect it when:
        1. At least 20s have passed (avoid false positives from noise)
        2. BT has been falling and now starts rising (RoR goes from negative to positive)
        3. We have enough samples to be confident
        """
        session = self._state.roast_session
        if session is None:
            return

        # Already marked
        if "turning_point" in self._marked_events:
            return

        elapsed = session.elapsed_time
        if elapsed is None or elapsed < 20:
            return

        samples = session.collector.samples
        if len(samples) < 5:
            return

        # Find the minimum BT sample after at least 10s
        min_bt = float("inf")
        min_idx = -1
        for i, s in enumerate(samples):
            if s.time_seconds < 10:
                continue
            if s.bt < min_bt:
                min_bt = s.bt
                min_idx = i

        if min_idx < 0:
            return

        # TP is confirmed when we have at least 3 samples AFTER the minimum
        # and BT is rising (latest BT > min BT + 2C to filter noise)
        samples_after_min = len(samples) - 1 - min_idx
        latest_bt = samples[-1].bt
        if samples_after_min >= 3 and latest_bt > min_bt + 2.0:
            tp_sample = samples[min_idx]
            try:
                session.mark_event(
                    "turning_point",
                    temp=tp_sample.bt,
                    time_seconds=tp_sample.time_seconds,
                )
                self._marked_events.add("turning_point")
                _logger.info(
                    "Auto-detected turning point: %.1fC at %s",
                    tp_sample.bt,
                    _fmt_time(tp_sample.time_seconds),
                )
                # Publish notification
                event = AISuggestionEvent(
                    reasoning=f"Turning point detected: {tp_sample.bt:.1f}C at {_fmt_time(tp_sample.time_seconds)}. BT is now rising.",
                )
                asyncio.create_task(self._state.event_bus.publish(event))
                entry = self._record_transcript("system", "event", event.reasoning)
                asyncio.create_task(self._publish_transcript_entry(entry))
            except Exception:
                _logger.debug("Failed to auto-mark turning point", exc_info=True)

    async def _loop(self) -> None:
        """Main loop: build context, call the live model, publish event."""
        while True:
            try:
                if not self._state.is_roast_active or self._mode == AIMode.OFF:
                    await asyncio.sleep(2.0)
                    continue

                self._sync_marked_events()
                if self._mode == AIMode.AUTO:
                    link = await self._handle_link()
                    if link in {"paused", "fallback", "blocked"}:
                        await asyncio.sleep(1.0 if link == "paused" else ANALYSIS_INTERVAL)
                        continue

                self._auto_detect_turning_point()
                await self._resolve_roast_level()
                await self._resolve_roast_history()

                messages = self._build_messages()
                if messages is None:
                    await asyncio.sleep(ANALYSIS_INTERVAL)
                    continue

                decision = await self._call_llm(messages)
                failed = decision is None
                if decision is None:
                    decision = {
                        "reasoning": "",
                        "heat": None,
                        "fan": None,
                        "drum": None,
                        "event": None,
                        "ask": None,
                        "drop": False,
                    }

                cmd = self._constrain_decision(decision)
                auto_applied = False
                if (
                    self._mode == AIMode.AUTO
                    and self.auto_control_enabled
                    and not self._resume_blocked
                    and not self._link_is_down()
                    and (not failed or cmd.constrained or cmd.zero_heat)
                ):
                    auto_applied = await self._auto_apply(decision)
                elif cmd.zero_heat and self._mode != AIMode.OFF and not self._resume_blocked:
                    await self._auto_apply({"heat": 0})
                await self._publish_guard(cmd)

                if failed:
                    if self._mode == AIMode.AUTO:
                        self._auto_fail_count += 1
                        if self._auto_fail_count >= AUTO_FAIL_FALLBACK:
                            await self._fallback_from_auto(
                                f"AI loop failed {self._auto_fail_count} times"
                            )
                    await asyncio.sleep(ANALYSIS_INTERVAL)
                    continue

                self._auto_fail_count = 0

                evt = decision.get("event")
                if evt in ("turning_point", "fc_start"):
                    decision["event"] = None
                if evt and evt in self._marked_events:
                    decision["event"] = None

                self._dedup_ask(decision)

                if self._state._safety_monitor:
                    self._state._safety_monitor.reset_watchdog()

                self._last_suggestion = decision

                heat = _clamp_control(decision.get("heat"))
                fan = _clamp_control(decision.get("fan"))
                drum = _clamp_control(decision.get("drum"))
                event = AISuggestionEvent(
                    reasoning=decision.get("reasoning", ""),
                    suggested_heat=heat,
                    suggested_fan=fan,
                    suggested_drum=drum,
                    suggested_event=decision.get("event"),
                    ask_operator=decision.get("ask"),
                    drop=bool(decision.get("drop")),
                    auto_applied=auto_applied,
                )
                await self._state.event_bus.publish(event)
                transcript_kind = "suggestion"
                if event.ask_operator:
                    transcript_kind = "question"
                if event.drop:
                    transcript_kind = "drop"
                entry = self._record_transcript(
                    "ai",
                    transcript_kind,
                    event.reasoning,
                    suggestion={
                        "heat": event.suggested_heat,
                        "fan": event.suggested_fan,
                        "drum": event.suggested_drum,
                        "event": event.suggested_event,
                        "ask": event.ask_operator,
                        "drop": event.drop,
                        "auto_applied": event.auto_applied,
                    },
                    metadata={"model": self._model},
                )
                await self._publish_transcript_entry(entry)

            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("AI agent cycle failed", exc_info=True)
                if self._mode == AIMode.AUTO:
                    self._auto_fail_count += 1
                    if self._auto_fail_count >= AUTO_FAIL_FALLBACK:
                        with contextlib.suppress(Exception):
                            await self._fallback_from_auto(
                                f"AI loop failed {self._auto_fail_count} times"
                            )

            await asyncio.sleep(ANALYSIS_INTERVAL)

    def _link_is_down(self) -> bool:
        driver = self._state.driver
        if driver is None or not getattr(driver, "is_connected", False):
            return True
        monitor = getattr(self._state, "_monitor", None)
        return monitor is not None and bool(getattr(monitor, "connection_lost", False))

    def _should_fallback_for_link(self) -> bool:
        return self._link_is_down()

    async def _handle_link(self) -> str:
        """Pause auto across a link blip. Fresh data (alarm clear) resumes it."""
        if self._resume_blocked or not getattr(self._state, "is_roast_active", False):
            self._link_paused = False
            self._link_paused_at = None
            return "blocked"
        if self._link_is_down():
            now = time.monotonic()
            if not self._link_paused:
                self._link_paused = True
                self._link_paused_at = now
                _logger.warning("AI AUTO paused for roaster link blip")
            elapsed = now - (self._link_paused_at or now)
            after_fc = "fc_start" in self._marked_events
            limit = LINK_BLIP_AFTER_FC_S if after_fc else LINK_BLIP_MANUAL_S
            if elapsed > limit:
                why = (
                    f"Link down {elapsed:.0f}s after first crack"
                    if after_fc
                    else f"Link down {elapsed:.0f}s"
                )
                await self._fallback_from_auto(why, level=SafetyAlertLevel.CRITICAL)
                return "fallback"
            return "paused"
        if self._link_paused:
            await self._resend_last_controls()
            self._link_paused = False
            self._link_paused_at = None
            await self._clear_fallback_alert()
            _logger.info("AI AUTO resumed after fresh roaster data")
            return "resumed"
        return "ok"

    async def _clear_fallback_alert(self) -> None:
        with contextlib.suppress(Exception):
            await self._state.event_bus.publish(
                SafetyAlertEvent(
                    level=SafetyAlertLevel.WARNING,
                    message="AI auto control resumed",
                    condition="ai_auto_fallback",
                    cleared=True,
                )
            )

    async def _resend_last_controls(self) -> None:
        if self._resume_blocked or not getattr(self._state, "is_roast_active", False):
            return
        driver = self._state.driver
        if driver is None or not getattr(driver, "is_connected", False):
            return
        ds = driver.state if isinstance(getattr(driver, "state", None), dict) else {}
        heat = self._last_controls.get("heat", _clamp_control(ds.get("HP")))
        fan = self._last_controls.get("fan", _clamp_control(ds.get("FC")))
        drum = self._last_controls.get("drum", _clamp_control(ds.get("RC")))
        if self._resume_blocked or not getattr(self._state, "is_roast_active", False):
            return
        if heat is not None and heat > 0:
            manager = getattr(self._state, "_heating_manager", None)
            if manager is None:
                manager = HeatingManager(driver, event_bus=getattr(self._state, "event_bus", None))
                self._state._heating_manager = manager
            await manager.start(heat)
        if self._resume_blocked or not getattr(self._state, "is_roast_active", False):
            return
        if heat is not None:
            await driver.set_heat(heat)
            self._last_controls["heat"] = heat
        if fan is not None:
            await driver.set_fan(fan)
            self._last_controls["fan"] = fan
        if drum is not None:
            await driver.set_drum(drum)
            self._last_controls["drum"] = drum

    def _constrain_decision(self, decision: dict[str, Any]) -> DevCommand:
        session = self._state.roast_session
        driver = self._state.driver
        if session is None or driver is None:
            return DevCommand(0, 0, 0, False, False, False, False, False, "")
        ds = driver.state if isinstance(getattr(driver, "state", None), dict) else {}
        bt = ds.get("BT")
        if not isinstance(bt, (int, float)):
            return DevCommand(
                _clamp_control(decision.get("heat")) or 0,
                _clamp_control(decision.get("fan")) or 0,
                _clamp_control(decision.get("drum")) or 0,
                False,
                False,
                False,
                False,
                False,
                "",
            )
        machine = session.machine
        fc_marked = (
            "fc_start" in self._marked_events or getattr(machine, "fc_start_time", None) is not None
        )
        batch = self._batch_grams or 0
        cal = get_calibration(batch)
        level = self._roast_level
        (dtr_low, _dtr_high), (dev_min, dev_max) = development_band(level)
        heat_now = _clamp_control(ds.get("HP"))
        if heat_now is None:
            heat_now = self._last_controls.get("heat", 0)
        fan_now = _clamp_control(ds.get("FC"))
        if fan_now is None:
            fan_now = self._last_controls.get("fan", 0)
        drum_now = _clamp_control(ds.get("RC"))
        if drum_now is None:
            drum_now = self._last_controls.get("drum", 0)
        elapsed = session.elapsed_time
        sample = DevSample(
            bt=float(bt),
            ror=session.latest_ror,
            elapsed=float(elapsed) if elapsed is not None else None,
            heat=heat_now,
            fan=fan_now or 0,
            drum=drum_now or 0,
            ai_heat=_clamp_control(decision.get("heat")),
            ai_fan=_clamp_control(decision.get("fan")),
            ai_drum=_clamp_control(decision.get("drum")),
            fc_predicted=predicted_fc_c(cal, batch),
            fc_marked=fc_marked,
            fc_elapsed=getattr(machine, "fc_start_time", None),
            drop_target=drop_target_c(level, cal, batch),
            dev_min_s=float(dev_min),
            dev_max_s=float(dev_max),
            dtr_low=dtr_low,
            dtr_high=_dtr_high,
        )
        auto = bool(getattr(self._state.config, "auto_drop", False)) and self._mode == AIMode.AUTO
        cmd = self._development.step(sample, auto_drop=auto and not self._auto_drop_sent)
        decision["heat"] = cmd.heat
        decision["fan"] = cmd.fan
        decision["drum"] = cmd.drum
        if cmd.drop_prompt or cmd.forced_prompt:
            decision["drop"] = True
        return cmd

    async def _publish_guard(self, cmd: Any) -> None:
        if self._resume_blocked:
            return
        session = self._state.roast_session
        driver = self._state.driver
        if not cmd.drop_prompt and not cmd.forced_prompt and not cmd.auto_drop:
            return
        bt = 0.0
        elapsed = 0.0
        if driver is not None and isinstance(getattr(driver, "state", None), dict):
            raw = driver.state.get("BT")
            if isinstance(raw, (int, float)):
                bt = float(raw)
        if session is not None and session.elapsed_time is not None:
            elapsed = float(session.elapsed_time)
        target = drop_target_c(self._roast_level, batch_g=self._batch_grams)
        if cmd.drop_prompt or cmd.forced_prompt:
            reason = "second-crack guard" if cmd.forced_prompt else "drop target reached"
            with contextlib.suppress(Exception):
                await self._state.event_bus.publish(
                    DropSuggested(
                        temp_bt=bt,
                        elapsed_seconds=elapsed,
                        target_temp=target,
                        reason=reason,
                    )
                )
        if cmd.auto_drop and not self._auto_drop_sent:
            self._auto_drop_sent = True
            callback = getattr(self._state, "auto_drop_callback", None)
            if callback is not None:
                self.block_auto_resume()
                with contextlib.suppress(Exception):
                    await callback()

    async def _auto_apply(self, decision: dict[str, Any]) -> bool:
        """Auto-apply heat/fan/drum (not events or drop). Returns True if anything applied."""
        applied = False
        state = self._state
        driver = state.driver

        if driver is None or not driver.is_connected:
            return False

        heat = _clamp_control(decision.get("heat"))
        if heat is not None:
            if heat > 0:
                if state._heating_manager is None:
                    state._heating_manager = HeatingManager(driver)
                await state._heating_manager.start(heat)
            else:
                if state._heating_manager is not None:
                    await state._heating_manager.stop()
                    state._heating_manager = None
            await driver.set_heat(heat)
            applied = True

        fan = _clamp_control(decision.get("fan"))
        if fan is not None:
            await driver.set_fan(fan)
            applied = True

        drum = _clamp_control(decision.get("drum"))
        if drum is not None:
            await driver.set_drum(drum)
            applied = True

        if applied:
            if heat is not None:
                self._last_controls["heat"] = heat
            if fan is not None:
                self._last_controls["fan"] = fan
            if drum is not None:
                self._last_controls["drum"] = drum
        return applied

    def _build_messages(self) -> list[dict[str, str]] | None:
        """Build system + user messages from current roast state."""
        session = self._state.roast_session
        driver = self._state.driver
        if session is None or driver is None:
            return None

        elapsed = session.elapsed_time
        if elapsed is None:
            return None

        ds = driver.state
        bt = ds.get("BT")
        et = ds.get("ET")
        hp = ds.get("HP", 0)
        fc = ds.get("FC", 0)
        rc = ds.get("RC", 0)

        if bt is None or et is None:
            return None

        ror = session.latest_ror
        ror_s = f"{ror:.1f}" if ror is not None else "N/A"

        machine = session.machine

        # Build user message with current state and curve
        sections = [format_calibration(self._batch_grams or 1200)]
        batch = self._batch_grams or 0
        cal = get_calibration(batch)
        level = self._roast_level
        target = drop_target_c(level, cal, batch)
        (dtr_low, dtr_high), (dev_min, dev_max) = development_band(level)
        fc_predicted = predicted_fc_c(cal, batch)
        sections.append(
            f"## Drop target\n"
            f"Drop target is {target:.0f}C for {level or 'Medium'} "
            f"(DTR {dtr_low:.0f}-{dtr_high:.0f}%, development {dev_min}-{dev_max}s). "
            f"Predicted FC is {fc_predicted:.0f}C. "
            "From 5C before that FC, step heat down so RoR declines. "
            "After FC, heat may only stay the same or decrease."
        )
        plan = self._locked_plan
        if plan:
            from maillard.roasting.plan import planned_bt_at

            sections.append(
                "## Locked RoastPlan targets\n"
                + "\n".join(
                    f"{p['name']}: {p.get('target_time_seconds')}s BT={p.get('target_temp')}C "
                    f"heat={p.get('heat_percent')} fan={p.get('fan_percent')} drum={p.get('drum_percent')}"
                    for p in plan.get("phases", [])
                )
            )
            plan_bt = planned_bt_at(plan.get("phases", []), elapsed)
            if plan_bt is not None:
                sections.append(
                    f"Plan delta: BT {bt - plan_bt:+.1f}C relative to target {plan_bt:.1f}C."
                )
            sections.append(
                "Plan delta is a guide, especially unreliable before TP; do not chase it with heat. "
                "Trust actual operator events. Discuss deviations and confidence before suggesting changes."
            )
            sections.extend(plan.get("warnings", []))
            plan_drop = plan.get("target_drop_temp")
            if plan_drop is None:
                for phase in plan.get("phases", []):
                    if phase.get("name") == "drop" and phase.get("target_temp") is not None:
                        plan_drop = phase.get("target_temp")
            conflict = plan_drop_conflict(plan_drop, level, cal, batch)
            if conflict:
                sections.append(conflict + f" Use drop target {target:.0f}C.")

        # Roast info — always include roast level (default to Medium)
        info_parts = []
        if self._bean_name:
            info_parts.append(f"Bean: {self._bean_name}")
        if self._batch_grams:
            info_parts.append(f"Batch: {self._batch_grams}g")
        level = self._roast_level or "Medium"
        info_parts.append(f"Target: {level}" + ("" if self._roast_level else " (default)"))
        sections.append("## Roast Info\n" + "  |  ".join(info_parts))

        # Past roast history (injected once, static for roast duration)
        history_text = format_history_for_prompt(self._roast_history)
        if history_text:
            sections.append(history_text)

        sections.append(
            f"## Current State [{_fmt_time(elapsed)}]\n"
            f"BT={bt:.1f}C  ET={et:.1f}C  RoR={ror_s}C/min  "
            f"HP={hp}%  Fan={fc}%  Drum={rc}%"
        )

        # Phase context
        phase = session.current_phase
        if phase is not None:
            phase_name = phase.name
            phase_line = f"## Phase: {phase_name}"

            # Time in phase
            if phase == RoastPhase.DRYING and machine.turning_point_time is not None:
                phase_dur = elapsed - machine.turning_point_time
                phase_line += f"\nTime in phase: {_fmt_time(phase_dur)}"
            elif phase == RoastPhase.MAILLARD and machine.dry_end_time is not None:
                phase_dur = elapsed - machine.dry_end_time
                phase_line += f"\nTime in phase: {_fmt_time(phase_dur)}"
            elif phase == RoastPhase.DEVELOPMENT and machine.fc_start_time is not None:
                phase_dur = elapsed - machine.fc_start_time
                dev_time = phase_dur
                total_time = elapsed
                dtr = (dev_time / total_time * 100) if total_time > 0 else 0
                phase_line += f"\nTime in phase: {_fmt_time(phase_dur)} | Live DTR: {dtr:.1f}%"

            sections.append(phase_line)

        # Pace warnings
        pace_warnings = []
        has_dry_end = "dry_end" in self._marked_events or machine.dry_end_time is not None
        has_fc = "fc_start" in self._marked_events or machine.fc_start_time is not None

        c = get_calibration(self._batch_grams or 1200)
        if elapsed > c["dry_end_time_s"] + 60 and not has_dry_end:
            pace_warnings.append(
                "Dry end later than calibration; ask operator to assess color and pace."
            )
        if elapsed > c["fc_time_s"] + 60 and not has_fc:
            pace_warnings.append(
                "FC later than calibration; assess trend and confidence with operator."
            )
        if elapsed > c["total_time_s"] + 90:
            pace_warnings.append(
                "Beyond typical total time; discuss development and drop with operator."
            )

        if pace_warnings:
            sections.append("## Pace\n" + "\n".join(pace_warnings))

        # Curve history (sampled)
        samples = session.collector.samples
        if samples:
            ror_list = session.collector.calculate_ror(smoothing=True, window_seconds=30)
            step = max(1, len(samples) // MAX_CURVE_POINTS)
            curve_lines = ["\n## Curve"]
            for i in range(0, len(samples), step):
                s = samples[i]
                r = f"{ror_list[i]:.1f}" if i < len(ror_list) and ror_list[i] is not None else "N/A"
                curve_lines.append(
                    f"  {_fmt_time(s.time_seconds)} BT={s.bt:.1f} RoR={r} HP={s.heat} Fan={s.fan} Drum={s.drum}"
                )
            # Always include latest
            if len(samples) > 1 and (len(samples) - 1) % step != 0:
                s = samples[-1]
                curve_lines.append(
                    f"  {_fmt_time(s.time_seconds)} BT={s.bt:.1f} RoR={ror_s} HP={s.heat} Fan={s.fan} Drum={s.drum}"
                )
            sections.append("\n".join(curve_lines))

        # Events
        if session.collector.events:
            ev_parts = [
                f"{e.type} at {_fmt_time(e.time_seconds)} ({e.temp:.1f}C)"
                for e in session.collector.events
            ]
            sections.append("\n## Events\n" + "\n".join(f"- {e}" for e in ev_parts))

        if self._marked_events:
            sections.append(
                f"\nAlready marked (do NOT suggest again): {', '.join(sorted(self._marked_events))}"
            )

        self._prune_operator_messages()
        if self._operator_messages:
            lines = ["\n## Operator Observations and Questions"]
            for item in self._operator_messages[-8:]:
                lines.append(f"- {item['timestamp']}: {item['message']}")
            lines.append(
                "Use these operator observations as sensory context. If the operator marked an event, trust the event data above."
            )
            sections.append("\n".join(lines))

        prior = self._previous_decisions()
        if prior:
            sections.append(prior)

        user_content = "\n".join(sections)
        self._last_prompt_snapshot = {
            "model": self._model,
            "interval_seconds": ANALYSIS_INTERVAL,
            "system_prompt": SYSTEM_PROMPT + "\n\n" + format_calibration(self._batch_grams or 1200),
            "messages": [{"role": "user", "content": user_content}],
            "metadata": {
                "mode": self._mode.name.lower(),
                "bean_name": self._bean_name,
                "roast_level": self._roast_level or "Medium",
                "batch_grams": self._batch_grams,
                "operator_message_count": len(self._operator_messages),
                "transcript_count": len(self._transcript),
                "marked_events": sorted(self._marked_events),
                "history_roast_count": len(self._roast_history.past_roasts)
                if self._roast_history
                else 0,
                "curve_sample_count": len(samples),
            },
        }

        return [
            {"role": "user", "content": user_content},
        ]

    async def _call_llm(self, messages: list[dict[str, str]]) -> dict[str, Any] | None:
        """Call Claude and parse JSON response."""
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=SYSTEM_PROMPT + "\n\n" + format_calibration(self._batch_grams or 1200),
                messages=messages,
            )
        except Exception:
            _logger.warning("AI API call failed (model=%s)", self._model, exc_info=True)
            return None

        text = response.content[0].text if response.content else ""
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        """Parse JSON from the model response, handling code fences."""
        text = text.strip()

        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Remove code fences
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Find JSON object
        match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        _logger.debug("Failed to parse model response: %s", text[:200])
        return None

    def get_status(self) -> dict[str, Any]:
        """Return current AI agent status for the API."""
        return {
            "mode": self.status_mode,
            "auto_control_enabled": self.auto_control_enabled,
            "model": self._model,
            "live_model": self._model,
            "plan_model": self._plan_model,
            "interval_seconds": ANALYSIS_INTERVAL,
            "marked_events": sorted(self._marked_events),
            "last_suggestion": self._last_suggestion,
            "transcript_count": len(self._transcript),
            "operator_message_count": len(self._operator_messages),
            "prompt_available": self._last_prompt_snapshot is not None,
            "fallback_reason": self._fallback_reason,
        }
