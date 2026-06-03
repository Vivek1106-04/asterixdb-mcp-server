"""Unit tests for the readonly=true invariant and timeout formatting on the CC hop.

These are the load-bearing safety tests: the gateway's entire mutation-rejection
story rests on ``readonly=true`` reaching the CC on every execute call.
"""

from __future__ import annotations

import pytest

from asterixdb_mcp.config import Settings
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_readonly_true_is_sent_on_every_execute(settings: Settings) -> None:
    # Arrange
    cap = make_capturing_cc(settings)

    # Act
    await cap.client.execute("SELECT 1;", client_context_id="sess::_::u")

    # Assert
    form = cap.last_query_form()
    assert form["readonly"] == "true"
    assert form["statement"] == "SELECT 1;"
    assert form["client_context_id"] == "sess::_::u"


async def test_timeout_is_a_duration_string(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    await cap.client.execute("SELECT 1;", client_context_id="c")
    assert cap.last_query_form()["timeout"] == "30000ms"


async def test_compiler_parameters_cannot_override_readonly(settings: Settings) -> None:
    # A malicious/buggy compilerParameters map must not be able to flip readonly off.
    cap = make_capturing_cc(settings)
    await cap.client.execute(
        "SELECT 1;",
        client_context_id="c",
        compiler_parameters={"readonly": "false", "timeout": "999ms"},
    )
    form = cap.last_query_form()
    assert form["readonly"] == "true"
    assert form["timeout"] == "30000ms"


async def test_shared_secret_header_sent_when_configured() -> None:
    settings = Settings(
        cc_base_url="http://test-cc:19002",
        cc_shared_secret="topsecret",
        agent_session_id="s",
    )
    cap = make_capturing_cc(settings)
    await cap.client.execute("SELECT 1;", client_context_id="c")
    assert cap.requests[-1].headers["X-Gateway-Secret"] == "topsecret"
