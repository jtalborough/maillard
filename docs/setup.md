# Setup Guide

## Requirements

- Python 3.12 or later
- Kaleido M10 roaster with network/WiFi control enabled
- Network connectivity between your computer and the roaster

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/jta/maillard.git
cd maillard
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
# For regular use
pip install -e .

# For development (includes test dependencies)
pip install -e ".[dev]"
```

## Configuration

### Environment Variables

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# Kaleido M10 connection
MAILLARD_KALEIDO_HOST=192.0.2.10
MAILLARD_KALEIDO_PORT=80

# Database location (default: data/maillard.db)
MAILLARD_DATABASE_URL=sqlite+aiosqlite:///data/maillard.db

# REST API settings
MAILLARD_API_HOST=127.0.0.1
MAILLARD_API_PORT=8080

# Safety limits (optional, these are defaults)
MAILLARD_MAX_BT_TEMP=230.0
MAILLARD_MAX_ET_TEMP=280.0
MAILLARD_MAX_ROR=20.0
MAILLARD_MIN_ROR=3.0
MAILLARD_MAX_ROAST_DURATION=1200
```

### Finding Your Kaleido's IP Address

1. Connect your Kaleido M10 to your WiFi network
2. Check your router's connected devices list
3. Or use the Kaleido app to find the IP address
4. Update `MAILLARD_KALEIDO_HOST` in your `.env` file

## Claude Desktop Integration

### 1. Locate Claude Desktop Config

**macOS:**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Windows:**
```
%APPDATA%\Claude\claude_desktop_config.json
```

### 2. Add Maillard Server

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maillard": {
      "command": "python3",
      "args": ["-m", "maillard", "mcp"],
      "cwd": "/path/to/maillard",
      "env": {
        "MAILLARD_KALEIDO_HOST": "192.0.2.10"
      }
    }
  }
}
```

Replace `/path/to/maillard` with the actual path to your cloned repository.

### 3. Restart Claude Desktop

Close and reopen Claude Desktop. You should see "maillard" in the MCP servers list.

## Running Standalone

### MCP Server

```bash
python -m maillard mcp
```

### REST API

```bash
python -m maillard api
```

The API will be available at `http://localhost:8080`. OpenAPI docs at `http://localhost:8080/docs`.

## Testing Your Setup

### 1. Check Installation

```bash
python -m maillard --help
```

### 2. Run Tests

```bash
pytest tests/ -v
```

### 3. Test Roaster Connection

In Claude Desktop (after configuration):

> "Connect to my roaster and show me the current status"

Or via REST API:

```bash
curl http://localhost:8080/api/roaster/status
```

## Troubleshooting

### Connection Issues

**"Cannot connect to roaster"**
- Verify Kaleido is powered on and connected to WiFi
- Check IP address is correct
- Ensure your computer is on the same network
- Try pinging the Kaleido: `ping 192.0.2.10`

**"WebSocket connection failed"**
- Kaleido might be in use by another client
- Restart the Kaleido and try again

### Claude Desktop Issues

**"MCP server not found"**
- Verify the path in `claude_desktop_config.json` is correct
- Ensure Python is available at the specified command
- Check Claude Desktop logs for errors

**"Tools not appearing"**
- Restart Claude Desktop after config changes
- Verify JSON syntax in config file

### Database Issues

**"Database locked"**
- Only run one instance of Maillard at a time
- Check for zombie processes: `ps aux | grep maillard`

## Safety Reminders

1. **Never leave a roast unattended** - Always be present near the roaster
2. **Test with simulator first** - Use `SimulatorDriver` for testing
3. **Know your emergency stop** - `emergency_stop` tool cuts heat immediately
4. **Verify connections** - Always confirm roaster connection before starting
