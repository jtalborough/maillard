#!/usr/bin/env python3
"""Preview or exclude anomalous roast records without deleting them."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from maillard.ai.calibration import roast_quality_issues
from maillard.core.config import get_config
from maillard.roasting.events import derive_roast_event_fields


def cleanup_changes(roast: dict, now: datetime) -> tuple[list[str], dict]:
    """Return reasons and proposed updates; leave recent active roasts alone."""
    issues = roast_quality_issues(roast)
    changes = {}
    if roast.get('status') == 'in_progress':
        start = roast.get('start_time') or roast.get('date')
        try:
            started = start if isinstance(start, datetime) else datetime.fromisoformat(start)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return issues + ['missing or invalid start time; review manually'], changes
        fields = derive_roast_event_fields(roast.get('curve_data') or {'events': roast.get('events', [])})
        if now - started <= timedelta(hours=24) or roast.get('drop_time') is not None or fields.get('drop_time') is not None:
            return issues, changes
        issues.append('in_progress older than 24h without drop')
        changes['status'] = 'abandoned'
    if issues and not roast.get('excluded'):
        changes['excluded'] = True
    return issues, changes


async def cleanup(apply: bool = False) -> None:
    config = get_config()
    username, password = config.load_mongodb_credentials()
    options = {}
    if username is not None and password is not None:
        options.update(username=username, password=password, authSource=config.mongodb_auth_source)
    client = AsyncIOMotorClient(host=config.mongodb_host, port=config.mongodb_port, **options)
    try:
        collection = client[config.mongodb_name]['roasts']
        now = datetime.now(timezone.utc)
        print('mode\troast_id\treasons\tproposed_changes')
        async for roast in collection.find({}):
            issues, changes = cleanup_changes(roast, now)
            if not issues:
                continue
            print(f"{'apply' if apply else 'dry-run'}\t{roast['_id']}\t{'; '.join(issues)}\t{changes}")
            if apply and changes:
                # Preserve concurrent operator updates or a newly recorded drop.
                guard = {'_id': roast['_id']}
                for key in ('status', 'excluded', 'start_time', 'drop_time', 'curve_data'):
                    guard[key] = roast.get(key)
                await collection.update_one(guard, {'$set': changes})
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Preview only (default)')
    mode.add_argument('--apply', action='store_true', help='Exclude bad rows and abandon stale sessions')
    args = parser.parse_args()
    try:
        asyncio.run(cleanup(apply=args.apply))
    except Exception as exc:
        parser.exit(1, f'Cleanup failed ({type(exc).__name__}); check app database settings.\n')


if __name__ == '__main__':
    main()
