"""Live integration tests against a running cluster (no LLM).

These spawn the real gateway over stdio and exercise its tools directly — no
model in the loop — so they verify the gateway wiring end to end without any
API spend. They run on every pull request once a cluster is available.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.accuracy.fixtures.loader import DATAVERSE, load_datasets
from tests.accuracy.sdk.mcp_client import McpTestClient

pytestmark = pytest.mark.live

# Tools the gateway must always advertise for the suites in this repo to work.
CORE_TOOLS = {"execute_query", "get_schema", "list_datasets", "sample_dataset", "get_reference"}


def test_gateway_advertises_core_tools(live_cluster) -> None:
    async def _run() -> set[str]:
        client = await McpTestClient.initialize(live_cluster.cc_base_url)
        try:
            return {schema["name"] for schema in client.tool_schemas()}
        finally:
            await client.close()

    advertised = asyncio.run(_run())
    missing = CORE_TOOLS - advertised
    assert not missing, f"gateway is missing core tools: {missing}"


def test_execute_query_returns_expected_rows(live_cluster) -> None:
    async def _run() -> str:
        client = await McpTestClient.initialize(live_cluster.cc_base_url)
        try:
            return await client.call_tool(
                "execute_query",
                {"statement": f"SELECT VALUE COUNT(*) FROM {DATAVERSE}.mflix_movies;"},
            )
        finally:
            await client.close()

    output = asyncio.run(_run())
    assert "40" in output, f"expected 40 movies in query result, got: {output}"


def test_get_schema_describes_the_dataset(live_cluster) -> None:
    async def _run() -> str:
        client = await McpTestClient.initialize(live_cluster.cc_base_url)
        try:
            return await client.call_tool(
                "get_schema", {"dataverse": DATAVERSE, "dataset": "comics_characters"}
            )
        finally:
            await client.close()

    output = asyncio.run(_run()).lower()
    assert "comics_characters" in output and "field" in output, (
        f"schema should describe the dataset, got: {output[:400]}"
    )


def test_sample_dataset_returns_open_fields(live_cluster) -> None:
    async def _run() -> str:
        client = await McpTestClient.initialize(live_cluster.cc_base_url)
        try:
            return await client.call_tool(
                "sample_dataset",
                {"dataverse": DATAVERSE, "dataset": "comics_characters", "size": 3},
            )
        finally:
            await client.close()

    output = asyncio.run(_run()).lower()
    assert "powers" in output or "is_villain" in output, (
        f"sample should surface open record fields, got: {output[:400]}"
    )


def test_loader_is_idempotent(live_cluster) -> None:
    counts = load_datasets(live_cluster.cc_base_url)
    assert counts["mflix_movies"] == 40
    assert set(counts) == {
        "comics_books",
        "comics_characters",
        "mflix_movies",
        "mflix_shows",
        "support_tickets",
    }


def test_tool_error_is_returned_not_raised(live_cluster) -> None:
    """A bad statement should come back as an error payload, not crash the call."""

    async def _run() -> str:
        client = await McpTestClient.initialize(live_cluster.cc_base_url)
        try:
            return await client.call_tool(
                "execute_query", {"statement": "SELECT * FROM accuracy.no_such_dataset;"}
            )
        finally:
            await client.close()

    output = asyncio.run(_run())
    # The gateway surfaces the compile error in the response payload.
    assert output and not _looks_like_rows(output), f"expected an error payload, got: {output}"


def _looks_like_rows(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("status") == "success"
