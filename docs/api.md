# API Reference

Maillard exposes two interfaces: MCP (for AI assistants) and REST (for programmatic access).

## MCP Tools

### Coffee Management

| Tool | Description |
|------|-------------|
| `add_coffee` | Register a new green coffee |
| `list_coffees` | List all coffees with optional filters |
| `get_coffee` | Get details of a specific coffee |
| `update_coffee` | Update coffee information |
| `delete_coffee` | Remove a coffee |

#### add_coffee

```json
{
  "name": "Ethiopia Yirgacheffe",
  "origin": "Ethiopia",
  "processing": "Washed",
  "varietal": ["Heirloom"],
  "altitude": 1900,
  "moisture": 11.5,
  "density": "High",
  "notes": "Floral, citrus notes"
}
```

### Profile Management

| Tool | Description |
|------|-------------|
| `generate_profile` | AI-generate a roast profile |
| `list_profiles` | List saved profiles |
| `get_profile` | Get full profile with phases |
| `save_profile` | Save a new profile |
| `update_profile` | Update existing profile |
| `delete_profile` | Remove a profile |

#### generate_profile

```json
{
  "coffee_id": "uuid",
  "roast_level": "Light",
  "flavor_goal": "Highlight floral and citrus notes",
  "brew_method": "Filter",
  "batch_size_grams": 200
}
```

### Roaster Control

| Tool | Description | Confirmation |
|------|-------------|--------------|
| `get_roaster_status` | Current state and temps | No |
| `connect_roaster` | Connect to Kaleido | No |
| `disconnect_roaster` | Safely disconnect | No |
| `start_preheat` | Begin preheating | No |
| `execute_charge` | Start roast (CHARGE) | **Yes** |
| `mark_turning_point` | Mark TP event | No |
| `mark_dry_end` | Mark dry end | No |
| `mark_first_crack` | Mark FC start | **Yes** |
| `mark_first_crack_end` | Mark FC end | No |
| `mark_second_crack` | Mark SC start | No |
| `execute_drop` | End roast (DROP) | **Yes** |
| `set_heat` | Set heating power (0-100%) | No |
| `set_fan` | Set fan speed (0-100%) | No |
| `set_drum` | Set drum speed (0-100%) | No |
| `set_target_temp` | Set PID target | No |
| `toggle_pid` | Enable/disable PID | No |
| `emergency_stop` | Immediate shutdown | No |

#### Human-in-the-Loop Confirmation

Critical actions require two calls:

1. First call (confirmed=false):
```json
{
  "requires_confirmation": true,
  "action": "charge",
  "message": "Ready to charge at 195°C. Confirm?"
}
```

2. Second call (confirmed=true): Executes action

### Monitoring

| Tool | Description |
|------|-------------|
| `get_realtime_data` | Current temps, RoR, settings |
| `get_roast_progress` | Phase, progress, predictions |
| `compare_to_profile` | Actual vs planned deviation |
| `get_roast_curve` | Full curve data |

### Analysis

| Tool | Description |
|------|-------------|
| `analyze_roast` | Post-roast analysis |
| `add_cupping_notes` | Record tasting feedback |
| `get_improvement_suggestions` | AI recommendations |
| `compare_roasts` | Compare multiple roasts |
| `get_roast_history` | List past roasts |

---

## REST API

Base URL: `http://localhost:8080/api`

### Coffee Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/coffees` | Create coffee |
| GET | `/coffees` | List coffees |
| GET | `/coffees/{id}` | Get coffee |
| PUT | `/coffees/{id}` | Update coffee |
| DELETE | `/coffees/{id}` | Delete coffee |

### Profile Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/profiles/generate` | Generate profile |
| POST | `/profiles` | Save profile |
| GET | `/profiles` | List profiles |
| GET | `/profiles/{id}` | Get profile |
| PUT | `/profiles/{id}` | Update profile |
| DELETE | `/profiles/{id}` | Delete profile |

### Roaster Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/roaster/status` | Current state |
| POST | `/roaster/connect` | Connect |
| POST | `/roaster/disconnect` | Disconnect |
| POST | `/roaster/preheat` | Start preheat |
| POST | `/roaster/charge` | Execute charge |
| POST | `/roaster/drop` | Execute drop |
| POST | `/roaster/control` | Set heat/fan/drum |
| POST | `/roaster/event` | Mark event |
| POST | `/roaster/emergency-stop` | Emergency stop |

### Monitoring Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/roaster/data` | Current readings |
| WS | `/roaster/stream` | Real-time stream |

### Roast History Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/roasts` | List roasts |
| GET | `/roasts/{id}` | Get roast |
| GET | `/roasts/{id}/curve` | Get curve data |
| POST | `/roasts/{id}/cupping` | Add cupping |
| GET | `/roasts/{id}/analysis` | Get analysis |

---

## Data Schemas

### Coffee

```json
{
  "id": "uuid",
  "name": "string",
  "origin": "string",
  "processing": "Washed | Natural | Honey | Anaerobic",
  "varietal": ["string"],
  "altitude": 1900,
  "altitude_category": "Low | Medium | High",
  "moisture": 11.5,
  "density": "Low | Medium | High",
  "notes": "string",
  "created_at": "ISO timestamp",
  "updated_at": "ISO timestamp"
}
```

### Profile

```json
{
  "id": "uuid",
  "coffee_id": "uuid | null",
  "name": "string",
  "roast_level": "Light | Medium-Light | Medium | Medium-Dark | Dark",
  "flavor_goal": "string",
  "brew_method": "Filter | Espresso | Both",
  "batch_size_grams": 200,
  "phases": [
    {
      "name": "charge | turning_point | dry_end | maillard | fc_start | development | drop",
      "target_time_seconds": 0,
      "target_temp": 195.0,
      "heat_percent": 100,
      "fan_percent": 40,
      "drum_percent": 60,
      "target_ror": 15.0,
      "notes": "string"
    }
  ],
  "target_dtr": 18.0,
  "charge_temp": 195.0,
  "ai_rationale": "string"
}
```

### Roast

```json
{
  "id": "uuid",
  "profile_id": "uuid | null",
  "coffee_id": "uuid | null",
  "batch_size_grams": 200,
  "start_time": "ISO timestamp",
  "end_time": "ISO timestamp",
  "total_time_seconds": 620,
  "charge_temp": 195.0,
  "turning_point_time": 72,
  "turning_point_temp": 97.0,
  "dry_end_time": 270,
  "dry_end_temp": 152.0,
  "fc_start_time": 540,
  "fc_start_temp": 196.0,
  "fc_end_time": 580,
  "drop_time": 620,
  "drop_temp": 204.0,
  "dtr": 18.1,
  "status": "in_progress | completed | aborted",
  "curve_data": { /* CurveData */ }
}
```

### CurveData

```json
{
  "time_seconds": [0, 1.5, 3, ...],
  "bt": [195.0, 180.0, 165.0, ...],
  "et": [220.0, 215.0, 210.0, ...],
  "bt_ror": [null, -20.0, -15.0, ...],
  "heat": [100, 100, 100, ...],
  "fan": [40, 40, 45, ...],
  "drum": [60, 60, 60, ...],
  "events": [
    {"time": 0, "type": "charge", "temp": 195.0}
  ]
}
```
