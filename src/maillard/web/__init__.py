"""Web dashboard for Maillard.

Provides a real-time monitoring dashboard for roast sessions.
The web server can run embedded inside the MCP process (sharing
ApplicationState) or standalone via `maillard web`.

Usage:
    # Embedded in MCP (automatic when web_enabled=True)
    maillard mcp

    # Standalone
    maillard web --driver-type simulator
"""

from maillard.web.app import create_app

__all__ = ["create_app"]
