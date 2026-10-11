#!/usr/bin/env python3
"""Preview physical green counts by default; --apply records ledger adjustments."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, FiniteFloat

from maillard.db import close_db, init_db
from maillard.db.models import Coffee
from maillard.inventory.service import coffee_summary, physical_count


@dataclass(frozen=True)
class Count:
    coffee: str
    grams: float
    supplier: str | None = None
    cost_per_kg: float | None = None
    received_at: str | None = None


class CoffeeSnapshot(BaseModel):
    """Offline API coffee metadata; constructing this never initializes Beanie."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    quantity_grams: FiniteFloat | None = None


def load_coffees_snapshot(path: Path) -> list[CoffeeSnapshot]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("Coffee snapshot must be a GET /api/coffees JSON list")
    coffees = [CoffeeSnapshot.model_validate(row) for row in data]
    if len({coffee.id for coffee in coffees}) != len(coffees):
        raise ValueError("Coffee snapshot contains duplicate ids")
    return coffees


def _tokens(name: str) -> set[str]:
    # Unicode whole words, with punctuation, hyphens and underscores as separators.
    return set(re.findall(r"[^\W_]+", name.casefold()))


def parse_counts(
    path: Path, *, format: str | None = None, input_bytes: bytes | None = None
) -> list[Count]:
    if input_bytes is None:
        input_bytes = sys.stdin.buffer.read() if path == Path("-") else path.read_bytes()
    raw = input_bytes.decode("utf-8-sig")
    format = format or ("txt" if path == Path("-") else path.suffix.lower().lstrip("."))
    if format == "json":
        data = json.loads(raw)
        if isinstance(data, dict):
            rows = [
                {"coffee": name, **(value if isinstance(value, dict) else {"grams": value})}
                for name, value in data.items()
            ]
        elif isinstance(data, list):
            rows = data
        else:
            raise ValueError("JSON must map coffee names to grams or contain count rows")
    elif format == "txt":
        rows = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, grams = line.rpartition(":")
            if not separator:
                raise ValueError(f"Text count line {line_number}: expected Name: grams")
            try:
                value = float(grams)
            except ValueError as exc:
                raise ValueError(f"Text count line {line_number}: invalid grams {grams!r}") from exc
            rows.append({"coffee": name, "grams": value})
    else:
        reader = csv.DictReader(io.StringIO(raw))
        if not {"coffee", "grams"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV header must include coffee,grams")
        rows = list(reader)
    counts, names = [], set()
    for row in rows:
        name = str(row["coffee"]).strip()
        grams = float(row["grams"])
        cost = float(row["cost_per_kg"]) if row.get("cost_per_kg") not in (None, "") else None
        if not name or name.casefold() in names:
            raise ValueError(f"Empty or duplicate coffee name: {name!r}")
        if not math.isfinite(grams) or grams < 0:
            raise ValueError(f"Count for {name} must be finite and nonnegative")
        if cost is not None and (not math.isfinite(cost) or cost < 0):
            raise ValueError(f"Cost for {name} must be finite and nonnegative")
        from maillard.inventory.service import normalize_date

        received = normalize_date(row["received_at"]) if row.get("received_at") else None
        counts.append(Count(name, grams, row.get("supplier") or None, cost, received))
        names.add(name.casefold())
    return counts


def match_coffee[T: (Coffee, CoffeeSnapshot)](name: str, coffees: list[T]) -> T:
    tokens = _tokens(name)
    for predicate in (
        lambda c: c.name == name,
        lambda c: c.name.casefold() == name.casefold(),
        lambda c: name.casefold() in c.name.casefold(),
        lambda c: bool(tokens) and tokens.issubset(_tokens(c.name)),
    ):
        matches = [coffee for coffee in coffees if predicate(coffee)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f"Ambiguous coffee {name!r}: " + ", ".join(c.name for c in matches))
    raise ValueError(f"Unmatched coffee: {name!r}")


def compute_plan(
    count: Count, coffee_id: str, current: float, has_lots: bool, already_applied: bool = False
) -> dict:
    return {
        "coffee_id": coffee_id,
        "coffee": count.coffee,
        "current": current,
        "counted": count.grams,
        "delta": 0 if already_applied else count.grams - current,
        "action": "no-op (count already applied)"
        if already_applied
        else "adjust newest lots"
        if has_lots
        else "create zero receipt + adjustment",
    }


async def run(
    path: Path,
    *,
    apply: bool = False,
    count_id: str | None = None,
    skip_unmatched: bool = False,
    coffees_json: Path | None = None,
    format: str | None = None,
) -> int:
    if coffees_json is not None and apply:
        print("--apply is refused with --coffees-json: snapshots are offline and read-only")
        return 1
    # Parse before connecting; snapshot mode never opens MongoDB or an HTTP client.
    input_bytes = sys.stdin.buffer.read() if path == Path("-") else path.read_bytes()
    counts = parse_counts(path, format=format, input_bytes=input_bytes)
    count_id = count_id or hashlib.sha256(input_bytes).hexdigest()
    if coffees_json is not None:
        coffees = load_coffees_snapshot(coffees_json)
    else:
        await init_db()
    try:
        if coffees_json is None:
            coffees = await Coffee.find_all().to_list()
        from maillard.db.models import InventoryOperation

        plans, errors, seen = [], [], set()
        for count in counts:
            try:
                coffee = match_coffee(count.coffee, coffees)
                if coffee.id in seen:
                    raise ValueError(f"Multiple input names resolve to {coffee.name!r}")
                seen.add(coffee.id)
                if coffees_json is not None:
                    balance = {"remaining": 0, "lots": []}
                    existing = None
                else:
                    balance = await coffee_summary(coffee.id)
                    existing = await InventoryOperation.get(f"count:{count_id}:{coffee.id}")
                plan = compute_plan(
                    count,
                    coffee.id,
                    balance["remaining"],
                    bool(balance["lots"]),
                    existing is not None,
                )
                plan.update(matched_coffee=coffee.name, legacy=coffee.quantity_grams)
                plans.append((count, plan))
            except ValueError as exc:
                errors.append(str(exc))
        if coffees_json is not None:
            print(
                "OFFLINE SNAPSHOT: current lot balances are assumed 0; legacy quantities are informational."
            )
        print("coffee\tmatched coffee\tlegacy (g)\tcurrent\tcounted\tdelta\taction")
        for _, plan in plans:
            legacy = f"{plan['legacy']:g}" if plan["legacy"] is not None else "unknown"
            print(
                f"{plan['coffee']}\t{plan['matched_coffee']}\t{legacy}\t"
                f"{plan['current']:g}\t{plan['counted']:g}\t{plan['delta']:+g}\t{plan['action']}"
            )
        for coffee in coffees:
            if coffee.id not in seen:
                print(f"WARNING: Coffee not in count file; unchanged: {coffee.name} ({coffee.id})")
        for error in errors:
            print(error)
        if errors and not skip_unmatched:
            print("Nothing written; resolve names or use --skip-unmatched")
            return 1
        if not apply:
            print("DRY RUN: nothing written. Use --apply to record this count.")
            return 0
        date = datetime.now(UTC).date().isoformat()
        for count, plan in plans:
            await physical_count(
                plan["coffee_id"],
                count.grams,
                count_id=count_id,
                date=date,
                supplier=count.supplier,
                cost_per_kg=count.cost_per_kg,
                received_at=count.received_at,
            )
        print(f"Applied count {count_id} ({len(plans)} coffees; retries are no-ops)")
        return 0
    finally:
        if coffees_json is None:
            await close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Count file, or - to read stdin (text by default)")
    parser.add_argument("--format", choices=("txt", "csv", "json"), help="Override input format")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--count-id", help="Default: SHA256 of exact input bytes (file or stdin)")
    parser.add_argument("--skip-unmatched", action="store_true")
    parser.add_argument(
        "--coffees-json", type=Path, help="Offline coffee API snapshot; --apply refused"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(
            run(
                args.file,
                apply=args.apply,
                count_id=args.count_id,
                skip_unmatched=args.skip_unmatched,
                coffees_json=args.coffees_json,
                format=args.format,
            )
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Invalid count: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
