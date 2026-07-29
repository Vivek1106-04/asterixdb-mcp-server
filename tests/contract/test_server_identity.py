"""Contract tests: the identity this server reports about itself.

``serverInfo`` and the ``version`` resource are how a client tells one gateway
build from another. Both have failed silently before - FastMCP does not forward a
version to the low-level server, so the SDK's own version leaks out in its place,
and the advertised protocol revision was a hand-written constant that stopped
matching the SDK it runs on.
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
    options = server._mcp_server.create_initialization_options()

    # Assert
    assert options.server_version == __version__


def test_server_info_does_not_leak_the_sdk_version(server) -> None:
    """The regression this guards: FastMCP falling back to ``pkg_version("mcp")``.

    Asserted separately from the positive check so the failure message names the
    actual defect rather than just showing two unequal strings.
    """
    # Arrange
    sdk_version = pkg_version("mcp")

    # Act
    options = server._mcp_server.create_initialization_options()

    # Assert
    assert options.server_version != sdk_version, (
        "serverInfo.version is reporting the mcp SDK version. FastMCP does not "
        "forward a version, so the low-level Server falls back to pkg_version('mcp')."
    )


def test_advertised_protocol_version_matches_the_installed_sdk() -> None:
    """A hand-written protocol constant goes stale the moment the SDK is bumped.

    The gateway does not implement the wire protocol itself - the SDK does - so the
    revision it advertises has to be whatever that SDK actually speaks.
    """
    assert MCP_PROTOCOL_VERSION == types.LATEST_PROTOCOL_VERSION


def test_version_resource_reports_both_versions() -> None:
    """The gateway block of ``asterixdb://version`` is what clients read to
    identify the build, so it carries the same two values as ``serverInfo``."""
    assert _gateway_block() == {
        "version": __version__,
        "protocolVersion": types.LATEST_PROTOCOL_VERSION,
    }
