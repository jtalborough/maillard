# AGENTS.md

Maillard is a supervised AI roasting copilot for the Kaleido M10 (Python 3.12, FastAPI, MongoDB).

- Install and test: `uv sync --all-extras --frozen && uv run pytest -q`
- Run locally with the simulator: `uv run python -m maillard web --driver-type simulator`
- Code lives in `src/maillard`, tests in `tests/unit`. Docs in `docs/`.
- Never weaken e-stop, safety monitor, watchdog, manual override, drop, or disconnect fallback.
- Verify hardware-facing changes in the simulator first.
- No code comments. Keep diffs small. Add a test for behavior changes.
- This repo is public: no real hostnames, IPs, tailnet names, keys, customer or inventory data. Use env vars and example values.
- Merging to `main` builds `ghcr.io/jtalborough/maillard` and deploys to the NAS. Do not merge while a roast is active.
