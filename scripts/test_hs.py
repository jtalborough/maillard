#!/usr/bin/env python3
"""Test HS (heating state) behavior."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.kaleido import KaleidoDriver

HOST = os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10")

async def main():
    d = KaleidoDriver()
    await d.connect(HOST, 80)

    print(f"Initial cached HS: {d.get_state_value('HS')}")

    # Read fresh state
    state = await d.read_state()
    print(f"Fresh state: HS={state.get('HS')}, HP={state.get('HP')}, BT={state.get('BT'):.1f}")

    # Force send HS=1 bypassing cache check
    print("\nSending HS=1...")
    await d._send_message("HS", "1")
    await asyncio.sleep(0.5)

    # Read state again
    state = await d.read_state()
    print(f"After HS=1: HS={state.get('HS')}, HP={state.get('HP')}, BT={state.get('BT'):.1f}")

    # Try HP=100 again
    print("\nSending HP=100...")
    await d._send_message("HP", "100")
    await asyncio.sleep(0.5)

    state = await d.read_state()
    print(f"After HP=100: HS={state.get('HS')}, HP={state.get('HP')}, BT={state.get('BT'):.1f}")

    await d.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
