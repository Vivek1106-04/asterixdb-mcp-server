"""Contract tests: the identity this server reports about itself.

``serverInfo`` and the ``version`` resource are how a client tells one gateway
build from another. Both have failed silently before: the 1.x FastMCP class took
no version argument and never forwarded one, so the low-level server fell back to
the SDK's own version and reported it as ours, and the advertised protocol
revision was a hand-written constant that stopped matching the SDK it ran on.

The 2.x ``MCPServer`` accepts ``version`` directly, so the first failure mode is
now structurally impossible rather than merely fixed. The assertions stay: they
cost nothing and they are what would catch the next SDK that decides serverInfo
should default to something of its own choosing.
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import pytest
from mcp import types

from asterixdb_mcp import MCP_PROTOCOL_VERSION, __version__
from asterixdb_mcp.config import Settings
from asterixdb_mcp.resources.version import _gateway_block
from asterixdb_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def server() -> object:
    return build_server(Settings(cc_base_url="http://test-cc:19002"))


def test_server_info_reports_this_package_version(server) -> None:
    # Arrange / Act
    options = server._lowlevel_server.create_initialization_options()

    # Assert
    assert options.server_version == __version__


def test_server_info_does_not_leak_the_sdk_version(server) -> None:
    """The regression this guards: serverInfo falling back to ``pkg_version("mcp")``.

    Asserted separately from the positive check so the failure message names the
    actual defect rather than just showing two unequal strings.
    """
    # Arrange
    sdk_version = pkg_version("mcp")

    # Act
    options = server._lowlevel_server.create_initialization_options()

    # Assert
    assert options.server_version != sdk_version, (
        "serverInfo.version is reporting the mcp SDK version rather than the "
        "gateway's own. Check that build_server still passes version= to MCPServer."
    )


def test_advertised_protocol_version_matches_the_installed_sdk() -> None:
    """A hand-written protocol constant goes stale the moment the SDK is bumped.

    The gateway does not implement the wire protocol itself - the SDK does - so the
    revision it advertises has to be whatever that SDK actually speaks.
    """
    assert MCP_PROTOCOL_VERSION == types.LATEST_PROTOCOL_VERSION


def test_advertised_protocol_version_is_the_modern_era_ceiling() -> None:
    """What we advertise is the highest revision, not what every client is given.

    The SDK serves two eras: a classic ``initialize`` handshake and a modern one.
    A client on the handshake path is answered with the older revision, so probing
    with ``initialize`` and seeing something other than this constant is correct
    behaviour. Pinning the relationship here keeps the docs honest about which of
    the two numbers this is.
    """
    from mcp.server.runner import (
        HANDSHAKE_PROTOCOL_VERSIONS,
        LATEST_MODERN_VERSION,
    )

    assert MCP_PROTOCOL_VERSION == LATEST_MODERN_VERSION
    assert MCP_PROTOCOL_VERSION not in HANDSHAKE_PROTOCOL_VERSIONS, (
        "the advertised revision is now also a handshake revision - the two-era "
        "distinction this documents has collapsed, so revisit the README wording."
    )


def test_version_resource_reports_both_versions() -> None:
    """The gateway block of ``asterixdb://version`` is what clients read to
    identify the build, so it carries the same two values as ``serverInfo``."""
    assert _gateway_block() == {
        "version": __version__,
        "protocolVersion": types.LATEST_PROTOCOL_VERSION,
    }
