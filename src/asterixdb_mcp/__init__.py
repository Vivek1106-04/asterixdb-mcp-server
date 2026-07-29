"""MCP gateway for Apache AsterixDB.

A standalone sidecar that exposes Apache AsterixDB to LLM agents over the Model
Context Protocol. The gateway never parses SQL++ and never holds CC state; the
AsterixDB Cluster Controller remains the single authority on parsing, planning,
and ``readonly=true`` enforcement.
"""

from mcp.types import LATEST_PROTOCOL_VERSION

__version__ = "0.1.0"

# Highest MCP protocol revision this gateway can speak. Read from the SDK rather
# than hand-written: the SDK implements the wire protocol, so a literal here goes
# stale the moment it is bumped and starts advertising a revision the server does
# not actually negotiate.
#
# Read this as a ceiling, not as what every client gets. The 2.x SDK serves two
# eras: a classic ``initialize`` handshake that tops out at the newest handshake
# revision, and a modern era at this one. A client connecting over ``initialize``
# is answered with the older number, and that is correct behaviour, not drift.
MCP_PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION
