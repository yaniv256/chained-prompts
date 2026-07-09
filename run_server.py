#!/usr/bin/env python
"""Wrapper script for chained-prompts MCP server."""
from server import mcp
mcp.run(transport="stdio")
