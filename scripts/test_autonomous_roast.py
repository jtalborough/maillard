#!/usr/bin/env python3
"""Test script for autonomous roasting with the simulator.

This script exercises the full roast flow:
1. Connect to simulator with accelerated thermal model
2. Create a test coffee and profile
3. Start a roast with the profile (execution engine in PROFILE mode)
4. Monitor progress, mark events as suggested
5. Execute drop when profile target reached

Run with: python scripts/test_autonomous_roast.py
"""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

# Configure logging to see roast progress
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

# Reduce noise from some loggers
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

logger = logging.getLogger("test_roast")


# Accelerated thermal model for fast testing
# Real roasts take 9-15 minutes; this runs in ~30-60 seconds
FAST_THERMAL_CONFIG = {
    "initial_bt": 140.0,  # Start below dry end threshold (150°C)
    "initial_et": 180.0,
    "max_heat_rate": 120.0,  # °C/min at 100% - ~8x faster than real
    "max_cool_rate": 30.0,
    "thermal_mass": 0.5,  # Moderate inertia
    "fc_trigger_temp": 196.0,
}


async def main() -> None:
    """Run the autonomous roast test."""
    # Import after logging setup
    from maillard.db.models import Profile
    from maillard.db.schemas import PhaseSchema
    from maillard.driver.simulator import SimulatorDriver, ThermalModelConfig
    from maillard.mcp.state import lifespan
    from maillard.mcp.tools.coffee import add_coffee
    from maillard.mcp.tools.execution import (
        get_execution_status,
        start_roast,
        switch_control_mode,
    )
    from maillard.mcp.tools.monitoring import get_roast_progress
    from maillard.mcp.tools.roaster import (
        disconnect_roaster,
        execute_drop,
        get_roaster_status,
        mark_dry_end,
        mark_first_crack,
        mark_turning_point,
    )
    from maillard.roasting.execution import ControlMode

    def make_ctx(state):
        """Create a mock MCP context."""
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"state": state}
        return ctx

    logger.info("=" * 60)
    logger.info("AUTONOMOUS ROAST TEST - SIMULATOR (ACCELERATED)")
    logger.info("=" * 60)

    async with lifespan() as state:
        ctx = make_ctx(state)

        # Step 1: Connect to simulator with fast thermal config
        logger.info("\n--- STEP 1: Connect to Simulator (Fast Mode) ---")
        thermal_config = ThermalModelConfig(**FAST_THERMAL_CONFIG)
        driver = SimulatorDriver(thermal_config=thermal_config)
        await driver.connect("simulator", 0)
        state.driver = driver
        state._driver_type = "simulator"
        logger.info(f"Connected - BT: {driver.state['BT']:.1f}°C, max_heat_rate: {thermal_config.max_heat_rate}°C/min")

        # Step 2: Create test coffee
        logger.info("\n--- STEP 2: Create Test Coffee ---")
        coffee_result = await add_coffee(
            ctx,
            name="Test Ethiopia Yirgacheffe",
            origin="Ethiopia",
            processing="Washed",
            altitude=1900,
            notes="Test coffee for simulator roast",
        )
        coffee_id = coffee_result["id"]
        logger.info(f"Coffee created: {coffee_result['name']} (id: {coffee_id})")

        # Step 3: Create test profile (accelerated times for fast thermal model)
        # Real roast ~9 min, test roast ~60 sec with fast thermal
        logger.info("\n--- STEP 3: Create Test Profile (Accelerated) ---")
        phases = [
            PhaseSchema(
                name="charge",
                target_time_seconds=0,
                target_temp=140.0,  # Charge temp (matches initial_bt)
                heat_percent=85,
                fan_percent=35,
                drum_percent=60,
            ),
            PhaseSchema(
                name="turning_point",
                target_time_seconds=5,  # Quick TP
                target_temp=135.0,  # Slight dip after beans absorb heat
                heat_percent=80,
                fan_percent=35,
                drum_percent=60,
            ),
            PhaseSchema(
                name="dry_end",
                target_time_seconds=15,
                target_temp=155.0,  # Dry end ~155
                heat_percent=75,
                fan_percent=40,
                drum_percent=60,
            ),
            PhaseSchema(
                name="maillard",
                target_time_seconds=30,
                target_temp=180.0,
                heat_percent=70,
                fan_percent=45,
                drum_percent=60,
            ),
            PhaseSchema(
                name="fc_start",
                target_time_seconds=45,
                target_temp=198.0,  # FC start
                heat_percent=60,
                fan_percent=50,
                drum_percent=60,
            ),
            PhaseSchema(
                name="development",
                target_time_seconds=55,
                target_temp=205.0,
                heat_percent=50,
                fan_percent=55,
                drum_percent=60,
            ),
            PhaseSchema(
                name="drop",
                target_time_seconds=65,  # ~65 sec total
                target_temp=210.0,
                heat_percent=0,
                fan_percent=100,
                drum_percent=60,
            ),
        ]

        # Save profile to DB
        async with state.session() as db:
            profile = Profile(
                name="Test Fast Roast Profile",
                coffee_id=coffee_id,
                roast_level="Light",
                phases=json.dumps([p.model_dump() for p in phases]),
                charge_temp=140.0,  # Matches initial_bt
                target_dtr=20.0,
            )
            db.add(profile)
            await db.commit()
            await db.refresh(profile)
            profile_id = profile.id

        logger.info(f"Profile created: {profile.name} (id: {profile_id})")
        logger.info(f"  Phases: {len(phases)}")
        logger.info(f"  Target roast time: {phases[-1].target_time_seconds}s")

        # Step 4: Start the roast
        logger.info("\n--- STEP 4: Start Roast ---")
        # First without confirmation to see the message
        start_result = await start_roast(ctx, coffee_id=coffee_id, profile_id=profile_id)
        logger.info(f"Confirmation required: {start_result.get('message', '')[:80]}...")

        # Now with confirmation
        start_result = await start_roast(
            ctx, coffee_id=coffee_id, profile_id=profile_id, confirmed=True
        )
        logger.info(f"Roast started: {start_result['status']}")
        logger.info(f"  Roast ID: {start_result['roast_id']}")
        logger.info(f"  Execution mode: {start_result['execution_mode']}")
        logger.info(f"  Charge temp: {start_result['charge_temp']:.1f}°C")

        # Step 5: Monitor the roast
        logger.info("\n--- STEP 5: Monitor Roast Progress ---")
        logger.info("(Simulator will run at accelerated speed)")

        events_marked = set()
        last_phase = None
        poll_count = 0
        max_polls = 500  # Safety limit (test should complete in ~150 polls)

        while poll_count < max_polls:
            poll_count += 1
            await asyncio.sleep(0.2)  # Poll every 0.2s for fast test

            # Get execution status
            exec_status = await get_execution_status(ctx)

            if not exec_status.get("roast_active"):
                logger.info("Roast no longer active")
                break

            engine = exec_status.get("execution_engine", {})
            current_state = exec_status.get("current_state", {})
            session = exec_status.get("session", {})

            bt = current_state.get("bt", 0)
            elapsed = engine.get("elapsed_seconds", 0)
            mode = engine.get("mode", "unknown")
            suggested = engine.get("suggested_events", [])

            # Get current phase
            current_phase = engine.get("current_phase", {}).get("name", "unknown")
            if current_phase != last_phase:
                logger.info(f"Phase change: {last_phase} -> {current_phase}")
                last_phase = current_phase

            # Log progress every 25 polls (~5 seconds)
            if poll_count % 25 == 0:
                targets = engine.get("targets", {})
                logger.info(
                    f"Progress: {elapsed:.0f}s | BT={bt:.1f}°C | "
                    f"Heat={current_state.get('heat_percent', 0)}% (target: {targets.get('heat_percent', 0)}%) | "
                    f"Phase={current_phase} | Mode={mode}"
                )

            # Mark suggested events
            if "turning_point" in suggested and "turning_point" not in events_marked:
                logger.info(f">>> Turning point suggested at {bt:.1f}°C ({elapsed:.0f}s)")
                result = await mark_turning_point(ctx)
                logger.info(f"    Marked: {result.get('status')}")
                events_marked.add("turning_point")

            if "dry_end" in suggested and "dry_end" not in events_marked:
                logger.info(f">>> Dry end suggested at {bt:.1f}°C ({elapsed:.0f}s)")
                result = await mark_dry_end(ctx)
                logger.info(f"    Marked: {result.get('status')}")
                events_marked.add("dry_end")

            if "fc_start" in suggested and "fc_start" not in events_marked:
                logger.info(f">>> First crack suggested at {bt:.1f}°C ({elapsed:.0f}s)")
                result = await mark_first_crack(ctx, confirmed=True)
                logger.info(f"    Marked: {result.get('status')}")
                events_marked.add("fc_start")

            if "drop" in suggested and "drop" not in events_marked:
                logger.info(f">>> Drop suggested at {bt:.1f}°C ({elapsed:.0f}s)")
                # Get final progress before dropping
                progress = await get_roast_progress(ctx)
                logger.info(f"    Final sample count: {progress.get('sample_count', 0)}")
                logger.info(f"    Final RoR: {progress.get('ror_instant', 'N/A')}")

                # Execute drop
                drop_result = await execute_drop(ctx, confirmed=True)
                logger.info(f"    Drop executed: {drop_result.get('status')}")
                logger.info(f"    Total time: {drop_result.get('total_time_seconds', 0)}s")
                logger.info(f"    DTR: {drop_result.get('dtr', 'N/A')}")
                break

        # Step 6: Final summary
        logger.info("\n--- STEP 6: Roast Complete ---")
        logger.info(f"Events marked: {events_marked}")
        logger.info(f"Total polls: {poll_count}")

        # Disconnect
        await disconnect_roaster(ctx)
        logger.info("Disconnected from simulator")

        logger.info("\n" + "=" * 60)
        logger.info("TEST COMPLETE")
        logger.info("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest interrupted by user")
        sys.exit(1)
