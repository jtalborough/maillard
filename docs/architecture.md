# Architecture

## Overview

Maillard is built as a headless service that exposes roaster control through two interfaces:

1. **MCP Server** - Primary interface for AI assistants (Claude Desktop)
2. **REST API** - Secondary interface for programmatic access

```
┌─────────────────┐     ┌─────────────────┐
│  Claude Desktop │     │   Custom App    │
│   (MCP Client)  │     │  (REST Client)  │
└────────┬────────┘     └────────┬────────┘
         │ MCP                   │ HTTP/WS
         ▼                       ▼
┌─────────────────────────────────────────┐
│            Maillard Service            │
├─────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────────┐ │
│  │ MCP Server  │    │    REST API     │ │
│  │  (FastMCP)  │    │    (FastAPI)    │ │
│  └──────┬──────┘    └────────┬────────┘ │
│         └──────────┬─────────┘          │
│                    ▼                    │
│  ┌─────────────────────────────────────┐│
│  │         Application Core            ││
│  │  • Roast State Machine              ││
│  │  • Curve Processing                 ││
│  │  • Safety Monitor                   ││
│  └──────────────────┬──────────────────┘│
│                     ▼                   │
│  ┌──────────────┐  ┌──────────────────┐ │
│  │   Database   │  │  Kaleido Driver  │ │
│  │   (SQLite)   │  │   (WebSocket)    │ │
│  └──────────────┘  └────────┬─────────┘ │
└─────────────────────────────┼───────────┘
                              ▼
                    ┌─────────────────┐
                    │  Kaleido M10    │
                    │    Roaster      │
                    └─────────────────┘
```

## Project Structure

```
maillard/
├── src/maillard/
│   ├── __main__.py      # CLI entry point
│   ├── core/            # Business logic
│   │   ├── config.py    # Configuration (Pydantic Settings)
│   │   ├── events.py    # Internal event bus
│   │   └── safety.py    # Safety monitoring
│   ├── driver/          # Hardware abstraction
│   │   ├── base.py      # RoasterDriver protocol
│   │   ├── kaleido.py   # Kaleido M10 implementation
│   │   └── simulator.py # Test simulator
│   ├── roasting/        # Roast execution
│   │   ├── state.py     # State machine
│   │   └── curve.py     # Data processing
│   ├── db/              # Persistence
│   │   ├── models.py    # SQLAlchemy models
│   │   ├── schemas.py   # Pydantic schemas
│   │   └── repository.py # Data access
│   ├── mcp/             # MCP interface
│   │   ├── server.py    # FastMCP server
│   │   ├── state.py     # Shared state
│   │   └── tools/       # Tool implementations
│   └── api/             # REST interface
│       ├── app.py       # FastAPI app
│       └── routers/     # Endpoints
├── tests/
└── docs/
```

## Components

### Driver Layer

The driver layer abstracts roaster communication:

```python
class RoasterDriver(Protocol):
    async def connect(self, host: str, port: int) -> None: ...
    async def disconnect(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...
    @property
    def state(self) -> RoasterState: ...

    async def set_heat(self, power: int) -> None: ...
    async def set_fan(self, speed: int) -> None: ...
    async def set_drum(self, speed: int) -> None: ...
    async def trigger_event(self, event: RoastEvent) -> None: ...
```

Two implementations:
- **KaleidoDriver**: Real hardware communication via WebSocket
- **SimulatorDriver**: Thermal model simulation for testing

### Roast State Machine

Manages roast lifecycle:

```
IDLE → PREHEATING → READY_TO_CHARGE → ROASTING → COOLING → COMPLETED
  ↓         ↓              ↓              ↓          ↓
  └─────────┴──────────────┴──────────────┴──────────┴──→ ABORTED
```

Sub-phases within ROASTING:
- `drying` - Charge to ~150°C
- `maillard` - ~150°C to first crack
- `development` - First crack to drop

### Database Layer

SQLite with async SQLAlchemy:

- **Coffee**: Green coffee inventory
- **Profile**: Roast profiles with phase targets
- **Roast**: Individual roast sessions with curve data
- **Cupping**: Tasting notes and scores

### MCP Server

FastMCP-based server exposing tools:

- **Coffee tools**: add, list, get, update, delete
- **Profile tools**: generate, list, get, save, update, delete
- **Roaster tools**: connect, status, control, events
- **Monitoring tools**: realtime data, progress, curves
- **Analysis tools**: analyze, cupping, suggestions

### Safety System

Continuous monitoring for:
- Temperature limits (BT max 230°C, ET max 280°C)
- RoR anomalies (stall < 3°C/min, crash > 20°C/min)
- Duration limits (warning 15min, cutoff 20min)
- Network failures (auto-reconnect, emergency timeout)

## Configuration

Environment variables with `MAILLARD_` prefix:

```bash
MAILLARD_KALEIDO_HOST=192.0.2.10
MAILLARD_KALEIDO_PORT=80
MAILLARD_DATABASE_URL=sqlite+aiosqlite:///data/maillard.db
MAILLARD_API_PORT=8080
MAILLARD_MAX_BT_TEMP=230.0
```

## Development

### Running Tests

```bash
pytest tests/ -v
```

### Type Checking

```bash
mypy src/maillard/
ruff check src/maillard/
```

### Running Services

```bash
# MCP Server (for Claude Desktop)
python -m maillard mcp

# REST API
python -m maillard api
```
