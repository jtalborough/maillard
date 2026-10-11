#!/usr/bin/env python3
"""Preview or fill missing phase fields on completed roasts."""

import argparse
import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from maillard.core.config import get_config
from maillard.roasting.events import missing_roast_event_fields


async def backfill(apply: bool = False) -> None:
    config = get_config()
    username, password = config.load_mongodb_credentials()
    options = {}
    if username is not None and password is not None:
        options.update(username=username, password=password, authSource=config.mongodb_auth_source)
    client = AsyncIOMotorClient(
        host=config.mongodb_host, port=config.mongodb_port, **options
    )
    try:
        # Raw reads avoid Beanie initialization creating indexes during dry-run.
        collection = client[config.mongodb_name]["roasts"]
        print("mode\troast_id\tfield\tproposed_value")
        count = 0
        async for roast in collection.find({"status": "completed"}):
            changes = missing_roast_event_fields(roast)
            for field, value in changes.items():
                print(f"{'apply' if apply else 'dry-run'}\t{roast['_id']}\t{field}\t{value}")
                if apply:
                    # Recheck each field atomically: preserve concurrent non-null edits.
                    await collection.update_one(
                        {"_id": roast["_id"], "status": "completed", field: None},
                        {"$set": {field: value}},
                    )
            count += bool(changes)
        print(f"{count} roast(s) with proposed changes.")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    mode.add_argument("--apply", action="store_true", help="Write only missing fields")
    args = parser.parse_args()
    try:
        asyncio.run(backfill(apply=args.apply))
    except Exception as exc:
        # Connection/config exceptions can contain credentials; never print their text.
        parser.exit(1, f"Backfill failed ({type(exc).__name__}); check app database settings.\n")


if __name__ == "__main__":
    main()
