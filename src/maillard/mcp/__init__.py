"""MCP server for Maillard.

This package provides the Model Context Protocol (MCP) server that enables
MCP-compatible AI assistants to interact with the Maillard coffee roasting
system.

Usage:
    # Run the MCP server via CLI
    maillard mcp

    # Or programmatically
    from maillard.mcp import run_server
    run_server()
"""

from maillard.mcp.server import get_state_from_context, mcp
from maillard.mcp.state import (
    ApplicationState,
    DriverType,
    RoastContext,
    get_app_state,
    lifespan,
    reset_app_state,
)


def run_server() -> None:
    """Run the MCP server.

    This is the main entry point for starting the MCP server.
    It handles stdin/stdout communication using the MCP protocol.
    """
    mcp.run()


__all__ = [
    # Server
    "mcp",
    "run_server",
    "get_state_from_context",
    # State management
    "ApplicationState",
    "RoastContext",
    "DriverType",
    "get_app_state",
    "reset_app_state",
    "lifespan",
]
