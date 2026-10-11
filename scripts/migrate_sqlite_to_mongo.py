#!/usr/bin/env python3
"""Migrate Maillard data from SQLite to MongoDB.

One-time migration script that reads all data from the SQLite database
and inserts it into MongoDB, converting JSON string fields to native types.

Usage:
    python scripts/migrate_sqlite_to_mongo.py \
        --sqlite data/maillard.db \
        --mongo mongodb://localhost:27017/maillard

    # Dry run (no writes):
    python scripts/migrate_sqlite_to_mongo.py \
        --sqlite data/maillard.db \
        --mongo mongodb://localhost:27017/maillard \
        --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path


def _parse_json(value: str | None) -> list | dict | None:
    """Parse a JSON string, returning None for null/empty values."""
    if value is None or value == "" or value == "null":
        return None
    try:
        parsed = json.loads(value)
        # Treat empty lists stored as "[]" as None for optional fields
        if parsed == []:
            return None
        return parsed
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_json_list(value: str | None) -> list[str] | None:
    """Parse a JSON string expected to be a list of strings."""
    result = _parse_json(value)
    if isinstance(result, list):
        return result
    return None


def _parse_json_dict(value: str | None) -> dict | None:
    """Parse a JSON string expected to be a dict."""
    result = _parse_json(value)
    if isinstance(result, dict):
        return result
    return None


def _parse_json_list_of_dicts(value: str | None) -> list[dict]:
    """Parse a JSON string expected to be a list of dicts (phases)."""
    result = _parse_json(value)
    if isinstance(result, list):
        return result
    return []


def read_sqlite(db_path: str) -> dict:
    """Read all data from SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    data = {}

    # Coffees
    cursor = conn.execute("SELECT * FROM coffees")
    data["coffees"] = [dict(row) for row in cursor.fetchall()]

    # Profiles
    cursor = conn.execute("SELECT * FROM profiles")
    data["profiles"] = [dict(row) for row in cursor.fetchall()]

    # Roasts
    cursor = conn.execute("SELECT * FROM roasts")
    data["roasts"] = [dict(row) for row in cursor.fetchall()]

    # Cuppings
    cursor = conn.execute("SELECT * FROM cuppings")
    data["cuppings"] = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return data


def transform_coffee(row: dict) -> dict:
    """Transform a SQLite coffee row to MongoDB document."""
    doc = {
        "_id": row["id"],
        "name": row["name"],
        "origin": row["origin"],
        "processing": row.get("processing"),
        "varietal": _parse_json_list(row.get("varietal")),
        "altitude": row.get("altitude"),
        "altitude_category": row.get("altitude_category"),
        "moisture": row.get("moisture"),
        "density": row.get("density"),
        "harvest_date": row.get("harvest_date"),
        "purchase_date": row.get("purchase_date"),
        "quantity_grams": row.get("quantity_grams"),
        "notes": row.get("notes"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    return doc


def transform_profile(row: dict) -> dict:
    """Transform a SQLite profile row to MongoDB document."""
    doc = {
        "_id": row["id"],
        "coffee_id": row.get("coffee_id"),
        "name": row["name"],
        "roast_level": row.get("roast_level"),
        "flavor_goal": row.get("flavor_goal"),
        "brew_method": row.get("brew_method"),
        "batch_size_grams": row.get("batch_size_grams"),
        "phases": _parse_json_list_of_dicts(row.get("phases")),
        "target_dtr": row.get("target_dtr"),
        "target_weight_loss": row.get("target_weight_loss"),
        "charge_temp": row.get("charge_temp"),
        "ai_rationale": row.get("ai_rationale"),
        "version": row.get("version", 1),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    return doc


def transform_roast(row: dict) -> dict:
    """Transform a SQLite roast row to MongoDB document."""
    doc = {
        "_id": row["id"],
        "profile_id": row.get("profile_id"),
        "coffee_id": row.get("coffee_id"),
        "batch_size_grams": row.get("batch_size_grams"),
        "start_time": row.get("start_time"),
        "end_time": row.get("end_time"),
        "total_time_seconds": row.get("total_time_seconds"),
        "charge_temp": row.get("charge_temp"),
        "turning_point_time": row.get("turning_point_time"),
        "turning_point_temp": row.get("turning_point_temp"),
        "dry_end_time": row.get("dry_end_time"),
        "dry_end_temp": row.get("dry_end_temp"),
        "fc_start_time": row.get("fc_start_time"),
        "fc_start_temp": row.get("fc_start_temp"),
        "fc_end_time": row.get("fc_end_time"),
        "fc_end_temp": row.get("fc_end_temp"),
        "sc_start_time": row.get("sc_start_time"),
        "sc_start_temp": row.get("sc_start_temp"),
        "drop_time": row.get("drop_time"),
        "drop_temp": row.get("drop_temp"),
        "dtr": row.get("dtr"),
        "weight_loss": row.get("weight_loss"),
        "curve_data": _parse_json_dict(row.get("curve_data")),
        "ambient_temp": row.get("ambient_temp"),
        "ambient_humidity": row.get("ambient_humidity"),
        "status": row.get("status", "in_progress"),
        "notes": row.get("notes"),
        "created_at": row.get("created_at"),
    }
    return doc


def transform_cupping(row: dict) -> dict:
    """Transform a SQLite cupping row to MongoDB document."""
    doc = {
        "_id": row["id"],
        "roast_id": row["roast_id"],
        "rest_days": row.get("rest_days"),
        "aroma": row.get("aroma"),
        "acidity": row.get("acidity"),
        "sweetness": row.get("sweetness"),
        "body": row.get("body"),
        "aftertaste": row.get("aftertaste"),
        "overall": row.get("overall"),
        "flavor_notes": _parse_json_list(row.get("flavor_notes")),
        "defects": _parse_json_list(row.get("defects")),
        "brew_method": row.get("brew_method"),
        "cupper_name": row.get("cupper_name"),
        "notes": row.get("notes"),
        "cupped_at": row.get("cupped_at"),
    }
    return doc


async def migrate(sqlite_path: str, mongo_uri: str, dry_run: bool = False) -> None:
    """Run the migration from SQLite to MongoDB."""
    from motor.motor_asyncio import AsyncIOMotorClient

    # Read SQLite data
    print(f"Reading from SQLite: {sqlite_path}")
    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite database not found: {sqlite_path}")
        sys.exit(1)

    data = read_sqlite(sqlite_path)
    print(
        f"  Found: {len(data['coffees'])} coffees, "
        f"{len(data['profiles'])} profiles, "
        f"{len(data['roasts'])} roasts, "
        f"{len(data['cuppings'])} cuppings"
    )

    # Transform data
    coffees = [transform_coffee(r) for r in data["coffees"]]
    profiles = [transform_profile(r) for r in data["profiles"]]
    roasts = [transform_roast(r) for r in data["roasts"]]
    cuppings = [transform_cupping(r) for r in data["cuppings"]]

    if dry_run:
        print("\n[DRY RUN] Would insert:")
        print(f"  {len(coffees)} coffees")
        print(f"  {len(profiles)} profiles")
        print(f"  {len(roasts)} roasts")
        print(f"  {len(cuppings)} cuppings")

        # Show sample transformations
        if coffees:
            print(f"\nSample coffee: {json.dumps(coffees[0], indent=2, default=str)}")
        if profiles:
            print(f"\nSample profile: {json.dumps(profiles[0], indent=2, default=str)}")
        return

    # Connect to MongoDB
    print(f"\nConnecting to MongoDB: {mongo_uri}")
    client = AsyncIOMotorClient(mongo_uri)

    # Parse database name from URI or use default
    from urllib.parse import urlparse

    parsed = urlparse(mongo_uri)
    db_name = parsed.path.lstrip("/") or "maillard"
    database = client[db_name]
    print(f"  Database: {db_name}")

    # Check for existing data
    for collection_name in ["coffees", "profiles", "roasts", "cuppings"]:
        count = await database[collection_name].count_documents({})
        if count > 0:
            print(f"\nWARNING: Collection '{collection_name}' already has {count} documents.")
            response = input("Continue and add to existing data? [y/N]: ")
            if response.lower() != "y":
                print("Aborted.")
                client.close()
                return

    # Insert data
    if coffees:
        result = await database["coffees"].insert_many(coffees)
        print(f"  Inserted {len(result.inserted_ids)} coffees")

    if profiles:
        result = await database["profiles"].insert_many(profiles)
        print(f"  Inserted {len(result.inserted_ids)} profiles")

    if roasts:
        result = await database["roasts"].insert_many(roasts)
        print(f"  Inserted {len(result.inserted_ids)} roasts")

    if cuppings:
        result = await database["cuppings"].insert_many(cuppings)
        print(f"  Inserted {len(result.inserted_ids)} cuppings")

    # Verify counts
    print("\nVerification:")
    for collection_name, source_data in [
        ("coffees", data["coffees"]),
        ("profiles", data["profiles"]),
        ("roasts", data["roasts"]),
        ("cuppings", data["cuppings"]),
    ]:
        mongo_count = await database[collection_name].count_documents({})
        sqlite_count = len(source_data)
        status = "OK" if mongo_count >= sqlite_count else "MISMATCH"
        print(f"  {collection_name}: SQLite={sqlite_count}, MongoDB={mongo_count} [{status}]")

    client.close()
    print("\nMigration complete!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Maillard data from SQLite to MongoDB")
    parser.add_argument(
        "--sqlite",
        default="data/maillard.db",
        help="Path to SQLite database (default: data/maillard.db)",
    )
    parser.add_argument(
        "--mongo",
        default="mongodb://localhost:27017/maillard",
        help="MongoDB connection URI (default: mongodb://localhost:27017/maillard)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without writing to MongoDB",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.sqlite, args.mongo, args.dry_run))


if __name__ == "__main__":
    main()
