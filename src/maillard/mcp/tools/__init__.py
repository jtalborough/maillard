"""MCP tool implementations.

This package contains MCP tools organized by domain:
- coffee: Coffee inventory management (add, list, get, update, delete)
- profile: Roast profile management (list, get, save, update, delete, generate)
- roaster: Roaster control tools (coming soon)
- monitoring: Real-time monitoring tools (coming soon)
- analysis: Post-roast analysis tools (coming soon)
"""

from maillard.mcp.tools.coffee import (
    add_coffee,
    delete_coffee,
    get_coffee,
    list_coffees,
    update_coffee,
)
from maillard.mcp.tools.profile import (
    delete_profile,
    generate_profile,
    get_profile,
    list_profiles,
    save_profile,
    update_profile,
)

__all__ = [
    # Coffee tools
    "add_coffee",
    "delete_coffee",
    "get_coffee",
    "list_coffees",
    "update_coffee",
    # Profile tools
    "delete_profile",
    "generate_profile",
    "get_profile",
    "list_profiles",
    "save_profile",
    "update_profile",
]
