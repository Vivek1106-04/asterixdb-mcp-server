"""MCP gateway for Apache AsterixDB.

A standalone sidecar that exposes Apache AsterixDB to LLM agents over the Model
Context Protocol. The gateway never parses SQL++ and never holds CC state; the
AsterixDB Cluster Controller remains the single authority on parsing, planning,
and ``readonly=true`` enforcement.
"""

from mcp.types import LATEST_PROTOCOL_VERSION

__version__ = "0.1.0"

# MCP protocol revision this gateway speaks. Read from the SDK rather than
# hand-written: the SDK implements the wire protocol, so a literal here goes
# stale the moment it is bumped and starts advertising a revision the server
# does not actually negotiate.
MCP_PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION
