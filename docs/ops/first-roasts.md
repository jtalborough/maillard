# First 3–5 roasts: machine checklist

Use this checklist at the Kaleido M10 to validate connection safety and build roast records, working up to 1200 g under manual control.

Stay at the machine throughout preheat and roasting. AI mode stays **off** or
**suggest**; keep `MAILLARD_AI_AUTO_CONTROL_ENABLED=false` for every roast here.

## Pre-flight (each session)

- [ ] Open `/api/version` on your app; record `version` and `git_sha` below and
  confirm they match the build you intended to run.
- [ ] Select AI **off** or **suggest**. Check `/api/ai/status`: `mode` must be
  `off` or `suggest`, and `auto_control_enabled` must be `false`.
- [ ] Keep Kaleido's local controls within reach, with your hand on the
  machine's own heat control during the bench checks and ready during roasting.
- [ ] Have your familiar manual roast strategy and event/weight notes ready.

## Empty-drum bench check (before roast 1; repeat after connection/safety changes)

These are pass criteria, not promises about an older build. The disconnect banner,
heat-resume guard, and disconnected e-stop message require the corresponding fixes.
If a check fails or is unclear, cut heat using Kaleido's controls, record the
failure, and resolve it before proceeding with this checklist.

- [ ] Confirm the drum is **EMPTY**. Connect and start preheat at a modest
  target below your normal charge temperature; record the target in °C.
- [ ] Pull the roaster's network cable (or stop the app container). Watch the
  machine directly: confirm Kaleido cuts heat by itself, **HS → 0**.
  Record seconds from disconnect to HS=0; do not rely on stale dashboard data.
- [ ] Reconnect (or restart the container). Confirm heat **does not return**
  without an explicit dashboard action. The dashboard should show a
  **heat-paused / connection-lost** banner and require explicit heat resume.
  If the app was stopped, inspect the banner once it is running again.
- [ ] With the connection restored, explicitly resume modest preheat and press
  dashboard **e-stop**. Confirm the machine actually cuts heat.
- [ ] Start another modest preheat, pull the network cable, and press dashboard
  **e-stop** while the app remains running. Confirm it says the roaster was
  **unreachable** and instructs you to **use the machine controls**. Use those
  controls to ensure heat is off; a dashboard message is not proof of delivery.
- [ ] Reconnect again and confirm heat remains off until explicitly resumed.

Date: __________  App version / git SHA: __________  Preheat target: ____ °C

| Check | Pass/fail | Observation / time |
|---|---|---|
| Disconnect: machine HS → 0 | | ____ s |
| Reconnect: heat stays off; banner shown | | |
| Connected dashboard e-stop: heat cuts | | |
| Disconnected e-stop: unreachable / use local controls | | |
| Final reconnect: heat stays off | | |

## Roasts 1–2: one familiar coffee, build up gradually

- [ ] Use one well-known coffee, such as Yemen Sharqui Haraz, for both roasts.
- [ ] Roast 1: **900–1000 g**, charge around **205–210 °C** on this probe.
- [ ] Roast 2: work up to **1200 g** using roast 1's response to choose charge
  temperature. Increase heat gradually from your 800 g strategy (**55–90 %**
  heat); watch the curve before each further adjustment.
- [ ] Control heat, fan, and drum manually. Treat suggest-mode advice as commentary.
- [ ] Mark/confirm **TP** (turning point), **dry end** (yellowing),
  **FC start**, and **FC end**; check any automatically detected TP.
- [ ] Press dashboard **DROP the moment beans leave the drum**.
- [ ] Record green weight (g), roasted weight (g), and ambient temperature (°C).

## Roast 3: repeat 1200 g on the same coffee

- [ ] Build the plan from roasts 1–2: charge temperature, FC time/temperature,
  heat changes, and drop. Keep control manual and record the same events/weights.
- [ ] Compare plan delta with the actual curve; use it as information only.

## Roasts 4–5: second coffee, suggest mode

- [ ] Use a second coffee with different density or process, such as a natural
  Ethiopian, at **1200 g**. Adjust the manual plan for that coffee.
- [ ] Select **suggest**, recheck `auto_control_enabled=false`, and use
  **accept/reject** buttons to record your judgment of each suggestion.
  Make any chosen control changes yourself; continue marking events and weights.

## After each roast

- [ ] Spend **5 minutes** reviewing plan versus actual: charge, TP, dry end,
  FC, drop, and response to heat changes. Note one adjustment for next time.
- [ ] Cup within **24–72 h** and enter the cupping against that roast.
- [ ] When the calibration fitter is merged and present, run it to inspect
  updated calibration. Check its output before using it for the next plan:

  ```bash
  docker exec -it <container> python /app/scripts/fit_calibration.py
  ```

## One-time history maintenance

These scripts arrive in separate PRs and may not exist until merged. Once
available, back up MongoDB, run each dry run, inspect its proposed changes, then
apply. Run inside the app container, with the configured database available:

```bash
docker exec -it <container> python /app/scripts/backfill_roast_events.py
docker exec -it <container> python /app/scripts/backfill_roast_events.py --apply
docker exec -it <container> python /app/scripts/cleanup_roast_history.py
docker exec -it <container> python /app/scripts/cleanup_roast_history.py --apply
```

## Known limitations

- Historical data stops at **808 g**. Targets at **1200 g** are low-confidence
  extrapolations; use your observations and cupping to refine them.
- Plan delta is **information only**: do not add heat just to chase the overlay.
- AI suggestions are **commentary**. The roaster decides controls, event marks, and drop.
- Calibration fitting updates explicit calibration data; it does not train the AI.
- Completing five roasts does not authorize AUTO. Keep the flag **false**.
