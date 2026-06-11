"""Unit tests for argument completion ranking and guarded resolution."""

from __future__ import annotations

import httpx
import pytest

from asterixdb_mcp.cc_client import CCClient
from asterixdb_mcp.completions import (
    MAX_COMPLETION_VALUES,
    complete_argument,
    rank_completions,
)
from asterixdb_mcp.config import Settings

pytestmark = pytest.mark.anyio


# --- pure ranking -----------------------------------------------------------


def test_empty_partial_returns_all_sorted() -> None:
    out = rank_completions(["Beta", "alpha", "Gamma"], "")
    assert out.values == ["Beta", "Gamma", "alpha"]
    assert out.total == 3
    assert out.hasMore is False


def test_ranks_exact_over_prefix_over_substring() -> None:
    out = rank_completions(["report", "reporting", "the_report", "unrelated"], "report")
    # exact "report", then prefix "reporting", then substring "the_report".
    assert out.values == ["report", "reporting", "the_report"]


def test_case_insensitive_match() -> None:
    out = rank_completions(["Sales", "SALARY"], "sal")
    assert set(out.values) == {"Sales", "SALARY"}


def test_drops_none_and_empty_candidates() -> None:
    out = rank_completions([None, "", "ok"], "")
    assert out.values == ["ok"]


def test_deduplicates_keeping_best_score() -> None:
    out = rank_completions(["dup", "dup"], "dup")
    assert out.values == ["dup"]
    assert out.total == 1


def test_caps_values_and_flags_has_more() -> None:
    candidates = [f"d{i:04d}" for i in range(MAX_COMPLETION_VALUES + 5)]
    out = rank_completions(candidates, "")
    assert len(out.values) == MAX_COMPLETION_VALUES
    assert out.total == MAX_COMPLETION_VALUES + 5
    assert out.hasMore is True


def test_no_match_returns_empty() -> None:
    out = rank_completions(["alpha", "beta"], "zzz")
    assert out.values == []
    assert out.hasMore is False


# --- guarded async resolution ----------------------------------------------


def _client(handler: object) -> CCClient:
    settings = Settings(cc_base_url="http://test-cc:19002")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.cc_base_url
    )
    return CCClient(settings, http)


def _settings() -> Settings:
    return Settings(cc_base_url="http://test-cc:19002")


async def test_completes_dataverse_names_from_cluster() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"DataverseName": "Sales", "DataFormat": "row"},
                    {"DataverseName": "Shop", "DataFormat": "row"},
                ]
            },
        )

    out = await complete_argument(
        _client(handler), _settings(), argument_name="dataverse", partial="s"
    )
    assert out is not None
    assert set(out.values) == {"Sales", "Shop"}


async def test_completes_dataset_names_scoped_by_context_dataverse() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"DatasetName": "Orders", "DataverseName": "Sales"}]}
        )

    out = await complete_argument(
        _client(handler),
        _settings(),
        argument_name="dataset",
        partial="ord",
        context_arguments={"dataverse": "Sales"},
    )
    assert out is not None
    assert out.values == ["Orders"]


async def test_completes_field_names_from_schema_for_group_by() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        if "Datatype" in body or "Field" in body or "Dataset" in body:
            # Schema lookups hit the metadata query; return field records.
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "DatasetName": "Orders",
                            "DataverseName": "Sales",
                            "Derived": {
                                "Record": {
                                    "Fields": [
                                        {"FieldName": "city", "FieldType": "string"},
                                        {"FieldName": "country", "FieldType": "string"},
                                    ]
                                }
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    out = await complete_argument(
        _client(handler),
        _settings(),
        argument_name="group_by",
        partial="c",
        context_arguments={"dataverse": "Sales", "dataset": "Orders"},
    )
    assert out is not None
    assert set(out.values) <= {"city", "country"}


async def test_field_completion_without_dataset_context_is_empty() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    out = await complete_argument(
        _client(handler), _settings(), argument_name="metric", partial="x"
    )
    assert out is not None
    assert out.values == []


async def test_unknown_argument_returns_none() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    out = await complete_argument(
        _client(handler), _settings(), argument_name="statement", partial="SELECT"
    )
    assert out is None


async def test_cluster_error_degrades_to_empty_completion() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    out = await complete_argument(
        _client(handler), _settings(), argument_name="dataverse", partial="s"
    )
    assert out is not None
    assert out.values == []


async def test_dataset_completion_swallows_cluster_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    out = await complete_argument(
        _client(handler),
        _settings(),
        argument_name="dataset",
        partial="o",
        context_arguments={"dataverse": "Sales"},
    )
    assert out is not None
    assert out.values == []


async def test_field_completion_swallows_schema_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    out = await complete_argument(
        _client(handler),
        _settings(),
        argument_name="group_by",
        partial="c",
        context_arguments={"dataverse": "Sales", "dataset": "Orders"},
    )
    assert out is not None
    assert out.values == []
