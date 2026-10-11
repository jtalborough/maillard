# Clean up roast history

Preview anomalous records on the NAS to keep test runs and broken sessions out of AI history and calibration. Records are preserved.

```sh
docker exec -it <container> python /app/scripts/cleanup_roast_history.py
```

Review the reasons and proposed changes, then apply:

```sh
docker exec -it <container> python /app/scripts/cleanup_roast_history.py --apply
```

The script uses the container's Mongo settings and raw Motor reads. Dry-run writes nothing, including indexes. Apply marks problem rows `excluded=True`; sessions still `in_progress` after 24 hours without a recorded drop become `abandoned`. Recent active sessions and sessions with a drop are left for manual review. Concurrent changes cause the update to be skipped; rerun the preview.

The shared quality check rejects non-completed, test-size (<250 g), cold-charge (<100 °C), explicitly flagged test/discarded, and missing/absurd duration (outside 180–1500 seconds) records. Late drop temperatures alone do not discard useful phase data.

Use the roast detail's discard/restore action to change operator exclusion. Restore does not bypass automatic quality checks. Outside Docker, use `uv run python scripts/cleanup_roast_history.py` or the installed environment's `python3`.
