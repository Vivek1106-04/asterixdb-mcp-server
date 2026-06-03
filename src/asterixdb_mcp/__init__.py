"""MCP gateway for Apache AsterixDB.

A standalone sidecar that exposes Apache AsterixDB to LLM agents over the Model
Context Protocol. The gateway never parses SQL++ and never holds CC state; the
AsterixDB Cluster Controller remains the single authority on parsing, planning,
and ``readonly=true`` enforcement.
"""

__version__ = "0.1.0"

# MCP protocol revision this gateway targets.
MCP_PROTOCOL_VERSION = "2025-03-26"
