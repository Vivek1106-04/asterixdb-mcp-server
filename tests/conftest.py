"""Shared test fixtures and helpers.

A captured-request fixture lets unit tests assert on the exact CC form parameters
the gateway sends without standing up a real AsterixDB cluster, using
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.cc_client import CCClient
from asterixdb_mcp.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked tests on asyncio only (no trio dependency)."""
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings pointed at a fake CC base URL."""
    return Settings(
        cc_base_url="http://test-cc:19002",
        agent_session_id="sess-test",
        max_time_ms=30_000,
        max_bytes_per_query=1_000_000,
    )


@dataclass
class CapturingCC:
    """A CCClient wired to a MockTransport that records every request it sees."""

    client: CCClient
    requests: list[httpx.Request] = field(default_factory=list)

    def last_query_form(self) -> dict[str, str]:
        """Return the urldecoded form of the most recent /query/service POST."""
        for request in reversed(self.requests):
            if request.url.path == "/query/service":
                parsed = parse_qs(request.content.decode())
                return {k: v[0] for k, v in parsed.items()}
        raise AssertionError("no /query/service request was captured")


def make_capturing_cc(
    settings: Settings,
    *,
    response_json: dict[str, Any] | None = None,
    status_code: int = 200,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> CapturingCC:
    """Build a CapturingCC returning a fixed JSON body (or a custom handler)."""
    captured = CapturingCC(client=None)
    default_body = (
        response_json if response_json is not None else {"status": "success", "results": []}
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.requests.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(status_code, json=default_body)

    transport = httpx.MockTransport(_handler)
    http = httpx.AsyncClient(transport=transport, base_url=settings.cc_base_url)
    captured.client = CCClient(settings, http)
    return captured


def json_response(body: dict[str, Any], status_code: int = 200) -> httpx.Response:
    """Helper to build a JSON httpx response from a custom handler."""
    return httpx.Response(status_code, content=json.dumps(body).encode())
