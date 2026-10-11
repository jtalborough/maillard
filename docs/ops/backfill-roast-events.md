# Backfill roast events

Recover missing phase times and temperatures from completed roast curves so history and comparison views can use recorded events.

On the NAS, after deploying an image containing this script, find the app container with `docker compose ps`. Preview changes:

```sh
docker exec -it <container> python /app/scripts/backfill_roast_events.py
```

Review the table, then apply:

```sh
docker exec -it <container> python /app/scripts/backfill_roast_events.py --apply
```

Run the preview again to confirm no further recoverable fields remain. Default mode is dry-run; explicit `--dry-run` also works. Take your normal MongoDB backup before applying.

The runtime Dockerfile copies this script to `/app/scripts/` and provides Python with the app dependencies in `/opt/venv`. It does **not** provide uv. In a source checkout with uv, use `uv run python scripts/backfill_roast_events.py` (append `--apply` to write).

The script uses the same `get_config()` settings and credential files as the app: `MAILLARD_MONGODB_HOST`, `MAILLARD_MONGODB_PORT`, `MAILLARD_MONGODB_NAME`, and optional username/password file settings and auth source. The legacy `MAILLARD_MONGODB_URI` is handled by config validation. Compose passes environment settings via `env_file`; no connection string argument is needed. Do not print credentials to troubleshoot.

Only completed roasts are considered. Each absent/null field is filled independently; existing values, including zero, are preserved, even if another process edits them during the run. Events use `type/time/temp` (legacy `bt` is also accepted). TP falls back to the lowest BT sample from charge through 90 seconds only when no TP event exists and the minimum is a real post-charge dip (at ≥5 s and ≥20 °C below the first sample). Fractional sample times truncate to integer seconds to match the Roast model. Second crack and drop are included. No events, samples, DTR, total duration, status, or control settings are changed; unrecoverable fields remain null. Dry-run does not initialize Beanie or create indexes.
