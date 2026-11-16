"""Compatibility launcher for the packaged BSE MCP server."""

from bse_data_mcp.server import build_server, main

# Built at import for local stdio use and the MCP Inspector, which both expect
# a module-level server object.
mcp = build_server()

__all__ = ["build_server", "main", "mcp"]


if __name__ == "__main__":
    main()
