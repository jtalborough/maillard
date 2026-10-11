#!/usr/bin/env python3
"""Full roast test with real Kaleido M10."""

import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.mcp import lifespan
from maillard.mcp.tools.roaster import (
    connect_roaster, get_roaster_status, start_preheat,
    execute_charge, mark_turning_point, mark_dry_end,
    mark_first_crack, execute_drop, disconnect_roaster,
)
from maillard.mcp.tools.monitoring import get_roast_progress
from maillard.mcp.tools.cupping import add_cupping


def ctx(state):
    m = MagicMock()
    m.request_context.lifespan_context = {"state": state}
    return m


async def show_status(state, c):
    s = await get_roaster_status(c)
    p = await get_roast_progress(c)
    roaster = s.get("roaster", {})
    bt = roaster.get("bt") or 0
    et = roaster.get("et") or 0
    hp = roaster.get("heat_percent") or 0
    fc = roaster.get("fan_percent") or 0

    active = p.get("status") != "no_active_roast"
    if active:
        elapsed = p.get("elapsed_seconds") or 0
        ror = p.get("ror_smoothed")
        phase = p.get("current_phase") or "?"
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        ror_s = f"{ror:+.1f}" if ror else "--"
        print(f"  {mins:02d}:{secs:02d} | BT:{bt:6.1f}°C ET:{et:6.1f}°C | H:{hp}% F:{fc}% | RoR:{ror_s} | {phase}")
    else:
        print(f"  BT:{bt:6.1f}°C ET:{et:6.1f}°C | H:{hp}% F:{fc}%")


async def main():
    print("=" * 60)
    print("  LIVE ROAST TEST - Kaleido M10")
    print("=" * 60)

    async with lifespan() as state:
        c = ctx(state)

        # 1. Connect
        print()
        print("[1] Connecting to roaster...")
        r = await connect_roaster(c, host=os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10"), driver_type="kaleido")
        print(f"    Status: {r.get('status')}")
        await show_status(state, c)

        # 2. Preheat
        print()
        print("[2] Starting preheat to 200°C...")
        r = await start_preheat(c, target_temp=200)
        print(f"    Status: {r.get('status')}")
        print(f"    Heat={r.get('heat_percent')}%, Fan={r.get('fan_percent')}%, Drum={r.get('drum_percent')}%")
        print(f"    Heating enabled: {r.get('heating_enabled')}")

        # Monitor preheat
        print()
        print("[3] Monitoring preheat (waiting for 180°C)...")
        print("    Press Ctrl+C to skip waiting")
        try:
            while True:
                await show_status(state, c)
                s = await get_roaster_status(c)
                bt = s.get("roaster", {}).get("bt") or 0
                if bt >= 180:
                    print(f"    Reached {bt:.1f}°C - ready to charge!")
                    break
                await asyncio.sleep(3)
        except KeyboardInterrupt:
            print()
            print("    Preheat monitoring skipped")

        # 3. Charge
        print()
        print("[4] Ready to CHARGE")
        input("    Press ENTER when beans are in drum...")
        r = await execute_charge(c, batch_size_grams=200, confirmed=True)
        print(f"    CHARGE: {r.get('status')}")
        roast_id = r.get("roast_id")
        print(f"    Roast ID: {roast_id}")

        if r.get("status") == "error":
            print(f"    Error: {r.get('error')}")
            await disconnect_roaster(c)
            return

        # 4. Monitor roast
        print()
        print("[5] Roasting... (auto-marking events)")
        print("    Press Ctrl+C to manually drop")

        tp_done = de_done = fc_done = False
        fc_time = None

        try:
            while True:
                await show_status(state, c)

                s = await get_roaster_status(c)
                bt = s.get("roaster", {}).get("bt") or 0

                p = await get_roast_progress(c)
                elapsed = p.get("elapsed_seconds") or 0
                ror = p.get("ror_smoothed")

                # Auto-mark turning point when RoR goes positive
                if not tp_done and elapsed > 30 and ror and ror > 0:
                    print()
                    print("    >>> Marking TURNING POINT")
                    r = await mark_turning_point(c)
                    print(f"    {r.get('message', r.get('status'))}")
                    tp_done = True

                # Auto-mark dry end around 150°C
                if not de_done and bt >= 150:
                    print()
                    print("    >>> Marking DRY END")
                    r = await mark_dry_end(c)
                    print(f"    {r.get('message', r.get('status'))}")
                    de_done = True

                # Auto-mark first crack around 195°C
                if not fc_done and bt >= 195:
                    print()
                    print("    >>> Marking FIRST CRACK")
                    r = await mark_first_crack(c, confirmed=True)
                    print(f"    {r.get('message', r.get('status'))}")
                    fc_done = True
                    fc_time = elapsed
                    print("    Development phase - will auto-drop at 210°C or 90s dev")

                # Auto-drop: 90s after FC or at 210°C
                if fc_done:
                    dev_time = elapsed - fc_time
                    if dev_time >= 90 or bt >= 210:
                        print()
                        print(f"    >>> Auto-drop (dev={dev_time:.0f}s, BT={bt:.1f}°C)")
                        break

                # Safety: drop if BT > 220
                if bt > 220:
                    print()
                    print("    >>> SAFETY: BT > 220°C - dropping!")
                    break

                await asyncio.sleep(2)

        except KeyboardInterrupt:
            print()
            print("    Manual drop requested")

        # 5. Drop
        print()
        print("[6] Executing DROP...")
        r = await execute_drop(c, confirmed=True)
        print(f"    DROP: {r.get('status')}")

        # 6. Add cupping placeholder
        if roast_id:
            print()
            print("[7] Adding cupping record...")
            cupping = await add_cupping(
                c,
                roast_id=roast_id,
                rest_days=0,
                notes="Live roast test - cupping scores TBD after resting",
            )
            print(f"    Cupping ID: {cupping['id']}")

        # Summary
        print()
        print("=" * 60)
        print("  ROAST COMPLETE!")
        print("=" * 60)
        print(f"  Roast ID: {roast_id}")

        await disconnect_roaster(c)
        print()
        print("Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nAborted.")
