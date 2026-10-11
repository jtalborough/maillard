# Green inventory

Record receipts, reserve presale green, and reconcile physical counts in Maillard. Lots and the append-only ledger own green balances; the store owns sellable bags.

## Operator workflow

1. Seed inventory from a physical count using the preview below, then explicitly apply it. Until seeding, coffees without lots show **0 / unknown**, never the old purchase quantity.
2. Receive new purchases in the Inventory drawer (grams and supplier); the API also accepts receipt date, cost/kg and notes.
3. Open a drop with capacity, bag size (340 g default) and expected yield (0.85 default). Green reserved per bag is bag size / yield, normally 400 g. Sync the drop to publish its capacity to the store.
4. Sync sales before closing. Close stops sales and releases unsold green; cancel releases all green only with zero sold bags and zero active checkout holds. Store rejection or unavailability retains reservations. Resolve active holds before retrying.
5. Use **Use for next roast** in Inventory, then confirm coffee and charged batch in the roast controls. The selected drop is validated against the coffee. Use Clear drop in the roast controls before charging another coffee. Deduction occurs after completion, while cooling controls remain available.
6. In History, weigh the roasted coffee, confirm/edit bags packed and bag size, and save. Bags default to floor(roasted grams / bag size). Zero bags is valid. Labels require saved roasted weight and bags packed, even when overriding site readiness. Sync the roast to deliver its bags to the store.
7. Sync a roasted drop to refresh sold/fulfilled counts from the store. Maillard never estimates store fulfilment counts locally.

All roasting remains supervised. No emergency-stop, watchdog, manual override, fan or drop controls are weakened.

## Models and ledger

`GreenLot` stores coffee id, normalized UTC receipt time, supplier, nullable cost/kg, grams in, cached grams remaining, notes and creation time. UUID ids break equal-date FIFO ties. The service alone maintains balance caches.

`InventoryMove` stores time, lot and coffee ids, kind, grams, optional roast/drop ids, unique nullable sparse idempotency key, note and actor. The service only inserts entries; it never updates or deletes them. Received, roasted, reserved and released grams are positive magnitudes; adjustments are signed (including zero for a zero starting count).

| Kind | Remaining delta | Reservation delta |
| --- | ---: | ---: |
| received | +grams | 0 |
| roasted | -grams | 0 |
| adjusted | signed grams | 0 |
| reserved | 0 | +grams |
| released | 0 | -grams |

Remaining = received + adjusted - roasted. Reserved = reserved - released, per lot and drop. Free = remaining - reserved. Adjustments require a note and refuse negative remaining unless explicitly forced. A physical count below reservations makes free negative; review/reduce outstanding drops rather than silently releasing sold reservations.

`PresaleDrop` stores coffee, optional store drop number, open/closed/roasted/cancelled status, capacity, bag size, expected yield, computed green grams per bag, last synced sold/held/fulfilled counts, roast ids and creation/close times. Opening/increasing capacity allocates free green FIFO. Decreasing capacity releases newest reservations first to retain the oldest FIFO allocation. Closed capacity equals sold bags. The store rejects shrinking below sold bags plus active holds.

`Roast` adds bags packed, bag size, nullable drop id and a green-deducted flag. Web charge, MCP execute_charge and MCP start_roast persist charged green weight at insertion. Completion uses measured green weight when available, else batch size. Aborted/abandoned roasts are deducted **only if green_weight_grams is set**, our persisted charge evidence. Merely planned batches never consume green.

With a drop, roast deduction first releases up to the consumed green from that drop's reserved lots, then roasts green FIFO across all lots. It does not subtract reservations from remaining twice. Supervisors can consume green reserved for other drops; this can make free negative and requires reconciliation. If stock falls short, deduct available green and append a negative adjustment on a zero-input deficit lot for the shortfall. The warning and negative balance remain visible; the roast/cooling flow still completes.

A measured green correction after an already-accounted roast updates the roast record without reapplying the inventory event. Reconcile the difference with a noted lot adjustment. A roast with no prior drop can be linked on the weights step: a separate idempotent release operation accounts for its drop reservation using the original deducted weight, without another roasted entry. Existing drop associations cannot change after deduction. Choose a drop before the first bag sync; already-applied store stock is immutable by roast id and later changes require store reconciliation.

### Persistence and retry recovery

Per-coffee renewable Mongo leases serialize mutation planning across processes. `InventoryOperation` stores a complete durable plan before effects are applied. Move ids are recorded atomically alongside each lot cache increment. A retry inserts missing moves and applies only missing increments, then completes the journal. Pending plans recover before the next inventory write for that coffee. A crash after deduction cannot subtract again. Multi-lot roast entries use `roast:<id>` for the first entry and stable lot suffixes thereafter; the entire journal is unique by `roast:<id>`.

The journal works with standalone MongoDB, without requiring transactions or a replica set. Readers do not write or recover pending operations; a read during an interrupted write can briefly show an incomplete cache until the next write/retry recovers it. Database failures during completion are logged; retry via weights after restoring the DB. The lease atomically retains the active plan before journal insertion; new owners recover that plan before allocation. Sequence guards prevent older drop plans from overwriting newer changes. Operation/cache markers must be retained with the ledger. Do not manually delete or modify ledger, markers, locks or journals.

## Physical counts

Preview a saved count entirely offline, using the saved coffee API snapshot:

```bash
uv run python scripts/import_green_counts.py tests/fixtures/green_count_2026-10-03.txt --coffees-json tests/fixtures/maillard_coffees_2026-10-03.json
```

`--coffees-json` reads local metadata only. It never connects to MongoDB or fetches the API. It assumes **current lot balances are 0 and no lots exist yet**; the snapshot's legacy `quantity_grams` appears only as information. This is a seeding preview, not a live inventory audit. `--apply` is refused in snapshot mode. The fixture contains 12 counts, including four empty coffees, totaling 9,515 g.

### Dry run and apply on the NAS container

Run these commands **on the NAS host**, with the updated Maillard image installed. The image bundles the importer at `/app/scripts/import_green_counts.py`. Stage the verbatim count at `/volume1/docker/maillard/tests/fixtures/green_count_2026-10-03.txt`, then copy only the count into the container:

```bash
cd /volume1/docker/maillard
sudo /usr/local/bin/docker cp tests/fixtures/green_count_2026-10-03.txt maillard-maillard-1:/tmp/green_count_2026-10-03.txt

# Dry run against the app's configured MongoDB; review every match and delta.
sudo /usr/local/bin/docker exec -it maillard-maillard-1 python /app/scripts/import_green_counts.py /tmp/green_count_2026-10-03.txt

# After reviewing the dry run, explicitly apply the physical count.
sudo /usr/local/bin/docker exec -it maillard-maillard-1 python /app/scripts/import_green_counts.py /tmp/green_count_2026-10-03.txt --apply
```

Alternatively, paste counts directly into stdin. Use `-i` without `-t` for heredocs. This example previews only the supplied coffee; paste the complete count when reconciling all coffees:

```bash
sudo /usr/local/bin/docker exec -i maillard-maillard-1 python /app/scripts/import_green_counts.py - <<'COUNTS'
Flores Turelelo: 791
COUNTS
```

Review the preview, then repeat the exact command and counts with `--apply` after `-`. Stdin defaults to text; use `--format csv` or `--format json` for other input. To save pasted counts for review and application, write them with `sudo /usr/local/bin/docker exec -i maillard-maillard-1 sh -c 'cat > /tmp/counts.txt' <<'COUNTS'`, followed by the count lines and closing `COUNTS`, then use the `docker exec -it` commands above with `/tmp/counts.txt`.

The container's installed Python environment and existing DB configuration supply credentials and connection settings. No `--coffees-json` is used for application. These are operator commands; NAS staging, deployment and application were not performed during development. For a later count, supply a new file or explicit count id:

```bash
uv run python scripts/import_green_counts.py counts.csv
uv run python scripts/import_green_counts.py counts.csv --apply
uv run python scripts/import_green_counts.py counts.json --apply --count-id weekly-2026-10-10
```

Text (`.txt`) input accepts one `Name: grams` entry per line, ignoring blank lines and lines starting with `#` (including indented comments). CSV header: `coffee,grams[,supplier,cost_per_kg,received_at]`. JSON supports `{"Coffee name": 1200}` or metadata values such as `{"Coffee name": {"grams":1200,"supplier":"Importer"}}`. Receipt times accept ISO dates or timestamps, normalized to UTC. Counts must be finite and nonnegative; **zero is a real count, never skipped**.

Matching tries exact name, case-insensitive name, unique substring, then a unique coffee containing every count-name token as a case-insensitive whole word. Token order does not matter; punctuation and hyphens separate words. Thus `Flores Turelelo` matches `Flores Anaerobic Honey Process Turelelo`, and `Guatemala Gesha` matches `Guatemala Acatenango Gesha AA`. An ambiguous match at any stage fails instead of guessing. Unmatched/ambiguous names, or two aliases resolving to one coffee, fail before application unless `--skip-unmatched` is explicit. Coffees omitted from the count warn and stay unchanged.

Output shows the entered name, matched registered name, informational legacy quantity, current lot balance, counted grams, delta and action. **Dry run is the default and writes nothing.** `--apply` records signed adjustments with note `physical count <UTC import date>`.

With no lot, create a zero-input lot and an adjusted starting balance, rather than inventing a receipt. A zero count creates a lot with `grams_in = grams_remaining = 0` and an explicit zero-gram `adjusted` entry, marking that coffee as counted and empty. With lots, additions use the newest lot; reductions use newest lots first, splitting if needed so reductions preserve FIFO and do not drive an individual positive lot below zero. Metadata applies to newly created lots; existing receipt metadata stays intact. Application recalculates delta while holding the coffee mutation lease.

Count keys are `count:<count-id>:<coffee-id>`. Count id defaults to SHA256 of the exact input bytes, including stdin bytes (comments and line endings count). Reapplying identical input remains a no-op even if a roast has changed the balance since the count. Use a new count id for a new physical count. Run against the app's normal `MAILLARD_MONGODB_HOST/PORT/NAME` and credential configuration; no host is hardcoded in the script.

## API and migration

The existing dashboard `/api` router exposes:

- GET/POST `/inventory/lots`; optional GET `coffee_id` filter; POST accepts `grams_in`.
- POST `/inventory/lots/{id}/adjust`: signed `grams`, required `note`, optional `force` and `idempotency_key`.
- GET `/inventory/moves?coffee_id=...` and GET `/inventory/summary` (remaining, reserved, free, known, lots per coffee).
- GET/POST `/drops`, POST `/drops/{id}/resize`, `/close`, `/cancel`, `/sync`. Open is local; sync publishes to the store. For drops with any sync attempt, resize/close/cancel require configured sync and store acceptance, even if a publishing timeout left the store drop number unknown.
- POST `/roasts/{id}/weights`: green/roasted grams, bags packed, bag size and drop id. Completion and weights retries share one inventory operation.
- Read-only MCP `get_inventory_summary` reports the same green balance and lot view.

Beanie creates the new collections/indexes through `DOCUMENT_MODELS`. No destructive migration or automatic historical deduction runs. `Coffee.quantity_grams` is **deprecated storage**: existing data is retained, while dashboard/API/MCP quantities reflect lots. Existing coffee CRUD accepts the legacy field for compatibility, but changing it has no effect on current green inventory. Lot inventory and ledger survive deletion of a roast; do not reseed old purchase quantities or replay pre-count roast history on top of physical counts.

Historical weight nulls: weights were introduced in commit `7502ef8` (2026-10-01), after the audit's latest real roast (May 19). Thus those old records never captured the field. Both MCP charge paths also omitted it until this implementation. Completion reloads the persisted roast; completion/code/weights writes now set only owned fields to preserve concurrent weight edits. No historical actual weights are fabricated.

## Verification and release checks

Local tests cover ledger arithmetic, concurrent allocation/retries, interrupted-write recovery, drop lifecycle, mocked HMAC HTTP, physical-count preview/application, label gates and SimulatorDriver charge-to-completion for web and both MCP paths. Hardware, printer output, actual MongoDB crash/lease behavior and store deployment require separate supervised validation; no live services were contacted during implementation.

## Cross-repo contract (verbatim)

The storefront integration follows this contract.

# Storefront inventory contract v1 (Maillard <-> store)  — 2026-10-03

## Ownership
- Maillard = source of truth for GREEN coffee (lots + append-only ledger).
- Store (Neon) = source of truth for SELLABLE BAGS (coffees.stock_total/stock_reserved + new stock_moves ledger) and drop sales.
- Only Maillard calls the store (store cannot reach Maillard). All Maillard->store calls use the existing HMAC scheme
  (X-Maillard-Signature: sha256=<hex HMAC-SHA256(MAILLARD_INGEST_SECRET, raw body)>, X-Maillard-Timestamp, body.sent_at, 15-min staleness check).

## 1. Roast ingest additions — POST /api/maillard/roasts (existing endpoint, schema_version stays 1, new fields optional)
roast.bags_packed: integer >= 0 | null     // bags produced by this roast (operator-entered, required by Maillard before label print)
roast.bag_size_grams: number | null         // bag size used (default 340)
roast.drop: { "maillard_id": "<Maillard PresaleDrop id>", "drop_number": 4 } | null   // set when this roast fulfils a pre-sale drop
Store behaviour (single transaction with the existing roast upsert, idempotent by roast):
- If status=="completed", coffee matched by maillard_id, and bags_packed is a non-null integer, and no stock_moves row with reason in ('roast','drop_fulfill') exists yet for this roast:
  - No drop: stock_total += bags_packed; insert stock_moves(reason='roast', delta=bags_packed, roast_id).
  - With drop (matched by drops.maillard_id): owed = bags_sold - bags_fulfilled; fulfil = min(owed, bags_packed);
    bags_fulfilled += fulfil; insert stock_moves(reason='drop_fulfill', delta=0, quantity=fulfil, drop_id, roast_id);
    leftover = bags_packed - fulfil -> stock_total += leftover, stock_moves(reason='roast', delta=leftover, drop_id, roast_id).
    Drop pre-sale closes (presale_status='roasted' if fully fulfilled, else stays 'roasted' with shortfall warning 'drop_short_<n>').
    Drop-sold bags were never added to stock_total, so they are not double counted.
- Re-sent roast (same roast id) never applies stock again. Response adds "stock": {"applied": bool, "delta": n, "fulfilled": n} and warnings.

## 2. Pre-sale drop upsert — POST /api/maillard/drops (new, HMAC)
Request:
{ "schema_version": 1, "sent_at": "...Z",
  "drop": { "maillard_id": "uuid", "coffee_maillard_id": "uuid", "drop_number": 4 | null,
            "status": "open" | "closed" | "cancelled",          // Maillard-side state
            "capacity_bags": 20,                                 // max bags sellable before roast (green reserved for exactly this many)
            "bag_size_grams": 340, "expected_yield": 0.85, "green_grams_per_bag": 400.0,
            "green_reserved_grams": 8000.0 } }
Store: upsert drops row by maillard_id (create with next drop_number if null; link coffee by coffees.maillard_id; 404 coffee_not_matched if none).
  presale=true, capacity_bags updated (never below bags_sold + active holds -> 409 {"error":"capacity_below_sold","bags_sold":n}),
  presale_status: open->'open', closed->'closed' (no further sales), cancelled->'cancelled' (only allowed if bags_sold=0).
Response 200: { "drop": { "id","maillard_id","drop_number","presale_status","capacity_bags","bags_sold","bags_held","bags_fulfilled" } }

## 3. Pre-sale drop status — POST /api/maillard/drops/status (new, HMAC, read-only)
Request: { "schema_version":1, "sent_at":"...Z", "maillard_ids": ["uuid", ...] }
Response: { "drops": [ same object as above ] }  (unknown ids omitted)

## Store selling rules for pre-sale drops
- Cart/checkout line may carry dropId. For a drop line: available = capacity_bags - bags_sold - active unexpired holds for that drop; only when presale_status='open'.
  stock_holds.drop_id records it; recordPaidOrder (webhook) increments drops.bags_sold (not coffees.stock_reserved) and stores order_items.drop_id,
  plus stock_moves(reason='drop_sale', delta=0, quantity=qty, drop_id, order_id). Same lock ordering / needsAttention behaviour as coffee stock.
- Regular coffee lines unchanged.

## Maillard green rules
- Ledger kinds: received | roasted | adjusted | reserved | released. grams signed (+received, -roasted, +/-adjusted; reserved/released tracked
  separately per lot). Lot remaining = received + adjusted - roasted. Lot reserved = reserved - released (>= 0). Free = remaining - reserved.
- Opening a drop reserves capacity_bags * green_grams_per_bag against the coffee's lots FIFO; refused if free green insufficient.
- Closing a drop (sales end): Maillard fetches bags_sold via /drops/status, releases (capacity - sold) * gpb, pushes capacity_bags=sold + status closed.
  After close, reserved green == bags_sold * gpb ("reserved per bag sold").
- Roast completion deducts green once, FIFO from oldest lot, idempotent by roast id (unique key). If roast.drop_id set: first release up to
  the drop's remaining reservation on the same lots, then record roasted. Net green deducted once.
