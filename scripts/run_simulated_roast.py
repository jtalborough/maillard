#!/usr/bin/env python3
"""Run a complete simulated roast to test the full system.

This script demonstrates the complete roast workflow:
1. Connect to simulator
2. Preheat (with manual heat control - simulator doesn't have real PID)
3. Charge (start roast)
4. Monitor progress, mark events
5. Drop (end roast)
6. Add cupping notes

Run with: python scripts/run_simulated_roast.py
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.simulator import SimulatorDriver, ThermalModelConfig
from maillard.mcp import lifespan
from maillard.mcp.tools.roaster import (
    connect_roaster,
    disconnect_roaster,
    execute_charge,
    execute_drop,
    get_roaster_status,
    mark_dry_end,
    mark_first_crack,
    mark_turning_point,
    set_heat,
    set_fan,
    start_preheat,
)
from maillard.mcp.tools.monitoring import get_roast_progress
from maillard.mcp.tools.cupping import add_cupping, get_cupping


def ctx(state):
    mock = MagicMock()
    mock.request_context.lifespan_context = {"state": state}
    return mock


def fmt_temp(t):
    return f"{t:.1f}°C" if t is not None else "--"


def fmt_time(s):
    if s is None:
        return "--:--"
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


async def wait_for_temp(state, target, label, timeout=120):
    """Wait for BT to reach target temperature."""
    print(f"  Waiting for {label} ({target}°C)...", end="", flush=True)
    start = asyncio.get_event_loop().time()
    while True:
        # Read directly from driver for accurate temps
        driver_state = state.driver.state
        bt = driver_state.get("BT")
        if bt is not None and bt >= target:
            print(f" reached {bt:.1f}°C")
            return bt
        if asyncio.get_event_loop().time() - start > timeout:
            print(f" TIMEOUT (last: {fmt_temp(bt)})")
            return bt
        print(".", end="", flush=True)
        await asyncio.sleep(0.5)


async def main():
    print("=" * 60)
    print("  SIMULATED ROAST TEST")
    print("=" * 60)
    print()

    async with lifespan() as state:
        c = ctx(state)

        # 1. Connect with fast thermal model for testing
        print("[1/8] Connecting to simulator (fast mode)...")

        # Create a fast simulator for testing (10x faster than real)
        fast_thermal = ThermalModelConfig(
            max_heat_rate=150.0,  # 10x faster heating
            thermal_mass=0.3,     # Less inertia
            initial_bt=100.0,     # Start warmer
            initial_et=120.0,
        )
        fast_sim = SimulatorDriver(thermal_config=fast_thermal)
        await fast_sim.connect("simulator", 0)
        state.driver = fast_sim  # Set on public property

        print("      Status: connected (fast thermal model)")
        print()

        # 2. Preheat
        # Note: Simulator doesn't have real PID, so we set heat manually
        # Using lower temp for faster testing
        print("[2/8] Starting preheat to 160°C...")
        await set_heat(c, percent=100)  # Manual heat for simulator
        print("      Heat set to 100%")

        # Wait for preheat (in sim, this takes ~15-20 seconds with fast model)
        await wait_for_temp(state, 160, "preheat", timeout=30)
        print()

        # 3. Charge
        print("[3/8] Executing CHARGE (beans in)...")
        result = await execute_charge(
            c,
            batch_size_grams=200,
            confirmed=True,
        )
        status = result.get("status")
        print(f"      Status: {status}")
        if status == "error":
            print(f"      Error: {result.get('error')}")
            print("      Aborting test - charge failed")
            return
        roast_id = result.get("roast_id")
        print(f"      Roast ID: {roast_id}")
        print()

        # 4. Monitor and mark events
        print("[4/8] Roasting - monitoring progress...")
        print()

        # Keep heat high during roast
        await set_heat(c, percent=80)

        tp_marked = False
        de_marked = False
        fc_marked = False

        while True:
            progress = await get_roast_progress(c)

            if progress.get("status") == "no_active_roast":
                print("      No active roast!")
                break

            # Get temps from driver for accuracy
            driver_state = state.driver.state
            bt = driver_state.get("BT") or 0
            elapsed = progress.get("elapsed_seconds") or 0
            phase = progress.get("current_phase") or "?"
            ror = progress.get("ror_smoothed")

            # Display
            ror_str = f"{ror:+.1f}" if ror else "--"
            print(f"\r      {fmt_time(elapsed)} | BT: {bt:5.1f}°C | RoR: {ror_str:>5}°/min | Phase: {phase:<12}  ", end="", flush=True)

            # Mark turning point around minimum temp (usually ~90-100°C after charge drop)
            if not tp_marked and elapsed > 30 and bt > 85 and ror and ror > 0:
                print()
                print("\n[5/8] Marking TURNING POINT...")
                result = await mark_turning_point(c)
                print(f"      {result.get('message', result.get('status'))}")
                tp_marked = True
                print()

            # Mark dry end around 150°C
            if not de_marked and bt >= 150:
                print()
                print("\n[6/8] Marking DRY END...")
                result = await mark_dry_end(c)
                print(f"      {result.get('message', result.get('status'))}")
                de_marked = True
                print()

            # Mark first crack around 196°C
            if not fc_marked and bt >= 196:
                print()
                print("\n[7/8] Marking FIRST CRACK...")
                result = await mark_first_crack(c, confirmed=True)
                print(f"      {result.get('message', result.get('status'))}")
                fc_marked = True
                # Let development continue for 60 seconds
                print("      Development time: 60 seconds...")
                await asyncio.sleep(5)  # Shortened for test
                break

            await asyncio.sleep(1)

        # 5. Drop
        print()
        print("[8/8] Executing DROP (roast complete)...")
        result = await execute_drop(c, confirmed=True)
        print(f"      Status: {result.get('status')}")
        print()

        # 6. Final summary
        print("=" * 60)
        print("  ROAST COMPLETE")
        print("=" * 60)
        print()

        # Add cupping notes
        print("Adding cupping evaluation...")
        cupping = await add_cupping(
            c,
            roast_id=roast_id,
            aroma=8.0,
            acidity=7.5,
            sweetness=8.0,
            body=7.0,
            aftertaste=7.5,
            overall=8.0,
            flavor_notes=["citrus", "caramel", "clean finish"],
            rest_days=0,
            brew_method="Simulated cupping",
            notes="Test roast via simulator - good development",
        )
        print(f"  Cupping ID: {cupping['id']}")
        print(f"  Total Score: {cupping['total_score']}/60")
        print(f"  Flavor Notes: {cupping['flavor_notes']}")
        print()

        # Disconnect
        print("Disconnecting...")
        await disconnect_roaster(c)

        print()
        print("✓ Full roast cycle completed successfully!")
        print()


if __name__ == "__main__":
    asyncio.run(main())
