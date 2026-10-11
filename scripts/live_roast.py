#!/usr/bin/env python3
"""Live roasting session with real Kaleido M10.

This script connects to your roaster and provides an interactive
monitoring display. Commands are entered via this terminal.

Run with: python scripts/live_roast.py
"""

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.mcp import lifespan
from maillard.mcp.tools.roaster import (
    connect_roaster,
    disconnect_roaster,
    execute_charge,
    execute_drop,
    get_roaster_status,
    mark_dry_end,
    mark_first_crack,
    mark_first_crack_end,
    mark_turning_point,
    set_heat,
    set_fan,
    set_drum,
    start_preheat,
    emergency_stop,
)
from maillard.mcp.tools.monitoring import get_roast_progress
from maillard.mcp.tools.cupping import add_cupping


def ctx(state):
    mock = MagicMock()
    mock.request_context.lifespan_context = {"state": state}
    return mock


def clear_line():
    print("\r" + " " * 80 + "\r", end="")


def fmt(val, unit="", width=6):
    if val is None:
        return "-".center(width)
    if isinstance(val, float):
        return f"{val:.1f}{unit}".rjust(width)
    return f"{val}{unit}".rjust(width)


async def display_status(state, c):
    """Display current status."""
    status = await get_roaster_status(c)
    progress = await get_roast_progress(c)

    bt = status.get("bt")
    et = status.get("et")
    heat = status.get("heat_percent")
    fan = status.get("fan_percent")
    drum = status.get("drum_percent")

    # Roast info
    roast_active = progress.get("status") != "no_active_roast"
    elapsed = progress.get("elapsed_seconds") if roast_active else None
    ror = progress.get("ror_smoothed") if roast_active else None
    phase = progress.get("current_phase") if roast_active else None

    # Format time
    if elapsed is not None:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins:02d}:{secs:02d}"
    else:
        time_str = "--:--"

    # Build display line
    line = f"  BT:{fmt(bt, '°')} ET:{fmt(et, '°')} | H:{fmt(heat, '%', 4)} F:{fmt(fan, '%', 4)} D:{fmt(drum, '%', 4)}"
    if roast_active:
        ror_str = f"{ror:+.1f}" if ror else "--"
        line += f" | {time_str} RoR:{ror_str:>6} {phase or ''}"

    clear_line()
    print(line, end="", flush=True)


async def monitor_loop(state, c, interval=1.0):
    """Continuously display status."""
    while True:
        try:
            await display_status(state, c)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            clear_line()
            print(f"  Error: {e}", end="", flush=True)
            await asyncio.sleep(interval)


async def main():
    print("=" * 60)
    print("  MAILLARD LIVE ROAST SESSION")
    print("=" * 60)
    print()

    async with lifespan() as state:
        c = ctx(state)

        # Connect
        print("Connecting to Kaleido M10...")
        try:
            result = await connect_roaster(c, host=os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10"), driver_type="kaleido")
            if result.get("status") != "connected":
                print(f"Connection failed: {result}")
                return
            print("Connected!")
            print()
        except Exception as e:
            print(f"Connection error: {e}")
            return

        # Start monitoring in background
        monitor_task = asyncio.create_task(monitor_loop(state, c))

        print("Commands:")
        print("  preheat <temp>  - Start preheating (e.g., 'preheat 200')")
        print("  heat <pct>      - Set heat percentage")
        print("  fan <pct>       - Set fan percentage")
        print("  drum <pct>      - Set drum percentage")
        print("  charge          - Execute CHARGE (start roast)")
        print("  tp              - Mark turning point")
        print("  de              - Mark dry end")
        print("  fc              - Mark first crack")
        print("  drop            - Execute DROP (end roast)")
        print("  stop            - Emergency stop")
        print("  quit            - Exit")
        print()
        print("-" * 60)

        roast_id = None

        try:
            while True:
                # Read command (this blocks, so monitor keeps running)
                await asyncio.sleep(0.1)  # Let monitor update

                # Use asyncio-compatible input
                loop = asyncio.get_event_loop()
                try:
                    cmd = await asyncio.wait_for(
                        loop.run_in_executor(None, input, "\n> "),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                cmd = cmd.strip().lower()
                if not cmd:
                    continue

                clear_line()
                print()

                try:
                    if cmd.startswith("preheat"):
                        parts = cmd.split()
                        temp = float(parts[1]) if len(parts) > 1 else 200
                        result = await start_preheat(c, target_temp=temp)
                        print(f"  Preheat: {result.get('status')}")

                    elif cmd.startswith("heat"):
                        parts = cmd.split()
                        pct = int(parts[1]) if len(parts) > 1 else 0
                        result = await set_heat(c, percent=pct)
                        print(f"  Heat set to {pct}%")

                    elif cmd.startswith("fan"):
                        parts = cmd.split()
                        pct = int(parts[1]) if len(parts) > 1 else 0
                        result = await set_fan(c, percent=pct)
                        print(f"  Fan set to {pct}%")

                    elif cmd.startswith("drum"):
                        parts = cmd.split()
                        pct = int(parts[1]) if len(parts) > 1 else 0
                        result = await set_drum(c, percent=pct)
                        print(f"  Drum set to {pct}%")

                    elif cmd == "charge":
                        result = await execute_charge(c, confirmed=True)
                        roast_id = result.get("roast_id")
                        print(f"  CHARGE: {result.get('status')} (roast_id: {roast_id})")

                    elif cmd == "tp":
                        result = await mark_turning_point(c)
                        print(f"  Turning point: {result.get('message', result.get('status'))}")

                    elif cmd == "de":
                        result = await mark_dry_end(c)
                        print(f"  Dry end: {result.get('message', result.get('status'))}")

                    elif cmd == "fc":
                        result = await mark_first_crack(c, confirmed=True)
                        print(f"  First crack: {result.get('message', result.get('status'))}")

                    elif cmd == "drop":
                        result = await execute_drop(c, confirmed=True)
                        print(f"  DROP: {result.get('status')}")
                        if roast_id:
                            print(f"  Roast {roast_id} complete!")

                    elif cmd == "stop":
                        result = await emergency_stop(c)
                        print(f"  EMERGENCY STOP: {result.get('status')}")

                    elif cmd == "quit" or cmd == "exit":
                        print("  Exiting...")
                        break

                    else:
                        print(f"  Unknown command: {cmd}")

                except Exception as e:
                    print(f"  Error: {e}")

                print()

        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

            print()
            print("Disconnecting...")
            await disconnect_roaster(c)
            print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
