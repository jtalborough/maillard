#!/usr/bin/env python3
"""Test script to investigate HS (Heating State) variable.

This script tests whether the HS variable is the missing piece
for getting the heater to actually produce heat.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.kaleido import KaleidoDriver


async def main():
    print("=" * 60)
    print("  HEATING STATE (HS) INVESTIGATION")
    print("=" * 60)
    print()

    driver = KaleidoDriver(logging_enabled=True)

    try:
        print("[1] Connecting to Kaleido M10...")
        await driver.connect(os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10"), 80)
        print("    Connected!")
        print()

        # Read initial state
        print("[2] Reading initial state...")
        state = await driver.read_state()
        print(f"    BT: {state.get('BT')}°C")
        print(f"    ET: {state.get('ET')}°C")
        print(f"    HP: {state.get('HP')}%  (Heat Power)")
        print(f"    HS: {state.get('HS')}   (Heating State - THE KEY?)")
        print(f"    AH: {state.get('AH')}   (Auto Heat/PID mode)")
        print(f"    FC: {state.get('FC')}%  (Fan)")
        print(f"    RC: {state.get('RC')}%  (Drum)")
        print()

        # Enable drum and fan first
        print("[3] Setting drum=50%, fan=30%...")
        await driver.set_drum(50)
        await driver.set_fan(30)
        await asyncio.sleep(0.5)
        state = await driver.read_state()
        print(f"    FC: {state.get('FC')}%, RC: {state.get('RC')}%")
        print()

        # Try enabling heating state
        print("[4] Setting HS=1 (enable heating state)...")
        await driver.set_heating_state(True)
        await asyncio.sleep(0.5)
        state = await driver.read_state()
        print(f"    HS after enable: {state.get('HS')}")
        print()

        # Now set heat power
        print("[5] Setting HP=100% (heat power)...")
        await driver.set_heat(100)
        await asyncio.sleep(0.5)
        state = await driver.read_state()
        print(f"    HP: {state.get('HP')}%")
        print(f"    HS: {state.get('HS')}")
        print()

        # Monitor for temperature rise
        print("[6] Monitoring for 60 seconds...")
        print("    Press Ctrl+C to stop early")
        print()
        print("    Time   BT       ET       HP    HS")
        print("    " + "-" * 40)

        initial_bt = state.get("BT", 0)
        try:
            for i in range(60):
                state = await driver.read_state()
                bt = state.get("BT", 0)
                et = state.get("ET", 0)
                hp = state.get("HP", 0)
                hs = state.get("HS", "?")

                delta = bt - initial_bt if initial_bt else 0
                delta_str = f"({delta:+.1f})" if abs(delta) > 0.1 else ""

                print(f"    {i:3d}s   {bt:6.1f}°C {et:6.1f}°C {hp:3}%   {hs}   {delta_str}")
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print()
            print("    Stopped.")

        # Summary
        print()
        final_state = await driver.read_state()
        final_bt = final_state.get("BT", 0)
        delta = final_bt - initial_bt if initial_bt else 0
        print("=" * 60)
        if delta > 1:
            print(f"  SUCCESS! Temperature rose by {delta:.1f}°C")
            print("  HS (Heating State) was the missing piece!")
        else:
            print(f"  Temperature change: {delta:.1f}°C")
            print("  May need to investigate further.")
        print("=" * 60)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Safety: turn off heat before disconnecting
        print()
        print("[7] Safety shutdown...")
        try:
            await driver.set_heat(0)
            await driver.set_heating_state(False)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        await driver.disconnect()
        print("    Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nAborted.")
