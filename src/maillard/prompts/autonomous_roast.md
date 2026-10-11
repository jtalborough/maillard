# Autonomous Roast Protocol

## AI Roastmaster Agent

The primary way to run an autonomous roast is the **AI Roastmaster Agent**
(`scripts/roast_agent.py`). Claude controls the roast in real-time via the
Anthropic API, making autonomous decisions about heat, fan, event timing,
and development every ~5 seconds.

### Prerequisites

- `ANTHROPIC_API_KEY` environment variable set
- `pip install anthropic` (or `pip install maillard[agent]`)
- Operator physically present near the roaster

### Running

```bash
# Test with simulator
python scripts/roast_agent.py

# Real hardware (Kaleido M10)
python scripts/roast_agent.py --driver kaleido

# Custom model
python scripts/roast_agent.py --model claude-sonnet-4-5-20250929
```

### How It Works

1. **Preheat**: Mechanical — heats to charge temperature (200°C)
2. **Charge**: Operator drops beans, presses Enter
3. **AI Control**: Claude analyzes BT, ET, RoR every 5 seconds and decides:
   - Heat adjustments (primary control lever)
   - Fan adjustments (airflow/convection)
   - Event markers (turning point, dry end from data analysis)
   - Questions for operator (sensory input like hearing first crack)
   - When to drop (based on target temp, DTR, development)
4. **Drop**: Claude calls `end_roast` when target is reached
5. **Save**: Roast data persisted to database with full curve

### What Claude Controls

Claude receives the full roast curve and makes real decisions — not
following a mechanical profile. It:

- Monitors RoR trends and adjusts heat to maintain declining curve
- Detects turning point from BT data (minimum after charge)
- Marks dry end based on temperature threshold (~150°C)
- Asks operator about first crack (cannot detect from data)
- Decides drop timing based on temperature, DTR, and development
- Explains reasoning in the terminal so operator understands

### Operator Role

The operator provides what the AI cannot sense:

- **Charge**: Drop beans into drum, press Enter
- **Sensory input**: Answer Claude's questions (e.g., "Do you hear cracking?")
- **Safety**: Type `stop` or Ctrl+C for emergency stop at any time
- **Physical presence**: Required near the roaster at all times

### Web Dashboard

The web dashboard at `http://127.0.0.1:8080` runs alongside the agent,
showing the live roast curve, temperatures, and event markers.

---

## MCP-Based Protocol (Alternative)

For use with Claude Desktop or Claude Code via MCP tools:

### 1. Connect to Roaster

```
connect_roaster(driver_type="simulator")
```

### 2. Set Up Coffee & Profile

```
list_coffees()
list_profiles()
add_coffee(name="Ethiopia Yirgacheffe", origin="Ethiopia", processing="Washed")
save_profile(name="Light Filter", roast_level="Light", ...)
```

### 3. Preheat

```
start_preheat(target_temp=200)
get_roaster_status()  # Poll every 15-20 seconds
```

### 4. Start Roast

```
start_roast(coffee_id="...", profile_id="...", confirmed=True)
```

### 5. Monitor Loop

Poll every 15-20 seconds:

```
get_roast_progress()
get_execution_status()
```

Confirm events when suggested:
- `mark_turning_point()`
- `mark_dry_end()`
- `mark_first_crack(confirmed=True)`
- `execute_drop(confirmed=True)`

### 6. Post-Roast

```
get_roaster_status()
add_cupping(roast_id="...", aroma=8.0, ...)
disconnect_roaster()
```

## Safety Notes

- **AI Agent watchdog**: Safety monitor resets every 5 seconds during AI loop
- **MCP watchdog**: Expects tool calls every 30 seconds; emergency-stops at 60s
- **Emergency stop**: `stop` command, Ctrl+C, or dashboard button
- **Never leave unattended**: Operator must be physically present
- **Heat control**: HeatingManager continuously maintains HS=1 on Kaleido hardware

## Simulator

The simulator models realistic temperature dynamics:
- Thermal lag from heat/fan changes
- RoR based on heat input
- Realistic temperature curves
- Useful for testing without hardware
