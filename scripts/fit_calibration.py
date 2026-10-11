#!/usr/bin/env python3
"""Print machine calibration from completed roasts (read-only; no index creation)."""

import argparse
import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from maillard.ai.calibration import fit_calibration
from maillard.core.config import get_config


async def fit() -> None:
    config = get_config()
    username, password = config.load_mongodb_credentials()
    options = {}
    if username is not None and password is not None:
        options.update(username=username, password=password, authSource=config.mongodb_auth_source)
    client = AsyncIOMotorClient(host=config.mongodb_host, port=config.mongodb_port, **options)
    try:
        # Raw reads avoid Beanie initialization creating indexes during dry-run.
        collection = client[config.mongodb_name]["roasts"]
        roasts = await collection.find({"status": "completed"}).to_list(length=None)
        print(fit_calibration(roasts).model_dump_json(indent=2))
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        asyncio.run(fit())
    except Exception as exc:
        # Connection/config exceptions can contain credentials; never print their text.
        parser.exit(
            1, f"Calibration fit failed ({type(exc).__name__}); check app database settings.\n"
        )


if __name__ == "__main__":
    main()
