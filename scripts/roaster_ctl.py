#!/usr/bin/env python3
"""Simple roaster control CLI.

Usage:
    roaster_ctl.py status          # Show current state
    roaster_ctl.py fan <0-100>     # Set fan speed
    roaster_ctl.py heat <0-100>    # Set heat (enables HS automatically)
    roaster_ctl.py drum <0-100>    # Set drum speed
    roaster_ctl.py off             # Turn everything off
    roaster_ctl.py stop            # Emergency stop (heat=0, fan=100)
    roaster_ctl.py cool            # Cooling mode (heat=0, fan=50)
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.kaleido import KaleidoDriver

HOST = os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10")


async def run_command(args):
    if len(args) < 1:
        print(__doc__)
        return

    cmd = args[0].lower()
    driver = KaleidoDriver()

    try:
        await driver.connect(HOST, 80)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    try:
        state = await driver.read_state()

        if cmd == "status":
            print(f"BT: {state.get('BT', 0):.1f}°C")
            print(f"ET: {state.get('ET', 0):.1f}°C")
            print(f"Heat: {state.get('HP', 0)}%")
            print(f"Heating: {'ON' if state.get('HS') == 1 else 'OFF'}")
            print(f"Fan: {state.get('FC', 0)}%")
            print(f"Drum: {state.get('RC', 0)}%")

        elif cmd == "fan":
            pct = int(args[1]) if len(args) > 1 else 0
            await driver.set_fan(pct)
            print(f"Fan set to {pct}%")

        elif cmd == "heat":
            pct = int(args[1]) if len(args) > 1 else 0
            await driver.set_heating_state(pct > 0)
            await driver.set_heat(pct)
            print(f"Heat set to {pct}% (HS={'ON' if pct > 0 else 'OFF'})")

        elif cmd == "drum":
            pct = int(args[1]) if len(args) > 1 else 0
            await driver.set_drum(pct)
            print(f"Drum set to {pct}%")

        elif cmd == "off":
            await driver.set_heat(0)
            await driver.set_heating_state(False)
            await driver.set_fan(0)
            await driver.set_drum(0)
            print("All off.")

        elif cmd == "stop":
            await driver.emergency_stop()
            await driver.set_heating_state(False)
            print("EMERGENCY STOP - Heat=0%, Fan=100%")

        elif cmd == "cool":
            await driver.set_heat(0)
            await driver.set_heating_state(False)
            await driver.set_fan(50)
            await driver.set_drum(0)
            print("Cooling mode - Heat=0%, Fan=50%")

        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)

    finally:
        await driver.disconnect()


if __name__ == "__main__":
    asyncio.run(run_command(sys.argv[1:]))
