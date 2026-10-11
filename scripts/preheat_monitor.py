#!/usr/bin/env python3
"""Preheat with persistent HS monitoring."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maillard.driver.kaleido import KaleidoDriver

HOST = os.environ.get("MAILLARD_KALEIDO_HOST", "192.0.2.10")
TARGET = 200  # Target preheat temp

async def main():
    d = KaleidoDriver()
    await d.connect(HOST, 80)

    print(f"Connected. Target preheat: {TARGET}°C")
    print("Setting up: Drum=50%, Fan=30%, Heat=100%, HS=ON")

    # Initial setup
    await d.set_drum(50)
    await d.set_fan(30)

    # Force HS=1 and HP=100
    await d._send_message("HS", "1")
    await asyncio.sleep(0.2)
    await d._send_message("HP", "100")

    print("\nMonitoring (Ctrl+C to stop)...")
    print("-" * 50)

    try:
        while True:
            # Re-send HS=1 to keep it active
            await d._send_message("HS", "1")

            state = await d.read_state()
            bt = state.get("BT", 0)
            et = state.get("ET", 0)
            hp = state.get("HP", 0)
            hs = d.get_state_value("HS")  # Use cached value

            status = "HEATING" if hs == 1 else "OFF"
            delta = TARGET - bt

            print(f"\rBT: {bt:5.1f}°C | ET: {et:5.1f}°C | HP: {hp:3}% | HS: {status:7} | Δ{delta:+6.1f}°C  ", end="", flush=True)

            if bt >= TARGET - 5:
                print(f"\n\n*** Approaching target! BT={bt:.1f}°C ***")
                # Reduce heat to avoid overshoot
                await d._send_message("HP", "50")
                print("Reduced heat to 50%")

            if bt >= TARGET:
                print(f"\n\n*** TARGET REACHED: {bt:.1f}°C ***")
                print("Ready for CHARGE!")
                break

            await asyncio.sleep(2)

    except KeyboardInterrupt:
        print("\n\nInterrupted")
    finally:
        print("Disconnecting...")
        await d.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
