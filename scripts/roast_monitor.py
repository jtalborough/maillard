#!/usr/bin/env python3
"""Simple terminal monitor for watching a simulated roast.

Run with: python scripts/roast_monitor.py

This connects to the simulator and shows real-time roast progress.
You can interact via Claude Desktop or run commands in another terminal.
"""

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.mcp import lifespan
from maillard.mcp.tools.roaster import connect_roaster, get_roaster_status
from maillard.mcp.tools.monitoring import get_roast_progress


def _make_ctx(state):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"state": state}
    return ctx


def clear_screen():
    os.system("clear" if os.name != "nt" else "cls")


def format_temp(t):
    return f"{t:6.1f}°C" if t is not None else "  --  "


def format_ror(r):
    return f"{r:+5.1f}°/min" if r is not None else "   --   "


def format_time(secs):
    if secs is None:
        return "--:--"
    mins = int(secs // 60)
    secs = int(secs % 60)
    return f"{mins:02d}:{secs:02d}"


async def display_status(ctx, state):
    """Display current roaster/roast status."""
    # Get roaster status
    roaster = await get_roaster_status(ctx)

    # Get roast progress if available
    progress = await get_roast_progress(ctx)

    clear_screen()
    print("=" * 60)
    print("  MAILLARD ROAST MONITOR")
    print("=" * 60)
    print()

    # Connection status
    connected = roaster.get("connected", False)
    print(f"  Roaster: {'🟢 Connected' if connected else '🔴 Disconnected'}")

    if connected:
        print()
        print("  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  Bean Temp (BT):  {format_temp(roaster.get('bt'))}                    │")
        print(f"  │  Env Temp (ET):   {format_temp(roaster.get('et'))}                    │")
        print("  │                                                     │")
        print(f"  │  Heat:  {roaster.get('heat_percent', 0):3d}%    Fan: {roaster.get('fan_percent', 0):3d}%    Drum: {roaster.get('drum_percent', 0):3d}%  │")
        print("  └─────────────────────────────────────────────────────┘")

    # Roast progress if active
    if progress.get("status") != "no_active_roast":
        print()
        print("  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  ROAST IN PROGRESS                                  │")
        print("  │                                                     │")

        elapsed = progress.get("elapsed_seconds")
        phase = progress.get("current_phase", "unknown")
        ror = progress.get("ror_smoothed")
        samples = progress.get("sample_count", 0)

        print(f"  │  Time:    {format_time(elapsed)}                                │")
        print(f"  │  Phase:   {phase:<20}                    │")
        print(f"  │  RoR:     {format_ror(ror)}                          │")
        print(f"  │  Samples: {samples:4d}                                  │")
        print("  │                                                     │")

        # Events
        events = progress.get("events", [])
        if events:
            print("  │  Events:                                            │")
            for evt in events[-5:]:  # Show last 5 events
                name = evt.get("event", "?")
                time = format_time(evt.get("elapsed_seconds"))
                temp = evt.get("bt", 0)
                print(f"  │    {time}  {name:<12} @ {temp:.1f}°C              │")

        print("  └─────────────────────────────────────────────────────┘")
    else:
        print()
        print("  No active roast. Use execute_charge to start.")

    print()
    print("  Press Ctrl+C to exit")
    print("=" * 60)


async def monitor_loop(state, ctx, interval=1.0):
    """Main monitoring loop."""
    while True:
        try:
            await display_status(ctx, state)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(interval)


async def main():
    print("Starting Maillard Roast Monitor...")
    print("Initializing...")

    async with lifespan() as state:
        ctx = _make_ctx(state)

        # Auto-connect to simulator
        print("Connecting to simulator...")
        result = await connect_roaster(ctx, driver_type="simulator")
        print(f"Connection: {result.get('status')}")

        # Start monitoring
        try:
            await monitor_loop(state, ctx)
        except KeyboardInterrupt:
            print("\n\nShutting down...")


if __name__ == "__main__":
    asyncio.run(main())
