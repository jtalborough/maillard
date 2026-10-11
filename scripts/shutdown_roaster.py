#!/usr/bin/env python3
"""Shut down the roaster safely."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.kaleido import KaleidoDriver


async def shutdown():
    driver = KaleidoDriver()
    await driver.connect(os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10"), 80)
    print("Shutting down roaster...")
    await driver.set_heat(0)
    await driver.set_heating_state(False)
    await driver.set_fan(50)  # Fan to help cool
    await driver.set_drum(0)
    state = await driver.read_state()
    bt = state.get("BT", 0)
    hp = state.get("HP", 0)
    hs = state.get("HS", 0)
    fc = state.get("FC", 0)
    print(f"  BT: {bt:.1f}C, HP: {hp}%, HS: {hs}, FC: {fc}%")
    await driver.disconnect()
    print("Roaster shut down. Fan at 50% to cool.")


if __name__ == "__main__":
    asyncio.run(shutdown())
