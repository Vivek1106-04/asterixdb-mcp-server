"""Unit tests for the sample_dataset tool."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.sample_dataset import MAX_SIZE, run_sample_dataset
from tests.conftest import json_response, make_capturing_cc

pytestmark = pytest.mark.anyio

_INVENTORY = [{"DataverseName": "Yelp", "DatasetName": "Business"}]


def _handler(sample_rows: Any) -> object:
    """Serve the inventory resolution query, then the sample query."""

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = _INVENTORY if "Metadata" in stmt else sample_rows
        return json_response({"status": "success", "results": rows})

    return handler


async def test_overflowing_sample_writes_artifact(settings: Settings, tmp_path: object) -> None:
    settings = settings.model_copy(
        update={"artifacts_dir": str(tmp_path), "max_rows_to_llm": 2}
    )
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(settings, handler=_handler(rows))

    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Business")

    artifact = result.structured["egress"]["artifact"]
    assert artifact["totalRows"] == 10  # full sample saved, window was 2
    assert result.structured["rowsReturned"] == 2


async def test_small_sample_writes_no_artifact(settings: Settings, tmp_path: object) -> None:
    settings = settings.model_copy(update={"artifacts_dir": str(tmp_path)})
    cap = make_capturing_cc(settings, handler=_handler([{"i": 1}]))

    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Business")

    assert "artifact" not in result.structured["egress"]


async def test_samples_real_rows(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([{"business_id": "x", "state": "NV"}]))
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Business")
    assert result.is_error is False
    assert result.structured["dataverse"] == "Yelp"
    assert result.structured["dataset"] == "Business"
    assert result.structured["rowsReturned"] == 1
    assert result.structured["results"][0]["state"] == "NV"
    # Backtick-quoted, parameter-free templated statement reached the CC.
    assert "`Yelp`.`Business`" in cap.last_query_form()["statement"]


async def test_case_insensitive_resolution(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([{"a": 1}]))
    result = await run_sample_dataset(cap.client, settings, dataverse="yelp", dataset="business")
    assert result.structured["dataverse"] == "Yelp"
    assert result.structured["dataset"] == "Business"


async def test_size_is_clamped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([]))
    result = await run_sample_dataset(
        cap.client, settings, dataverse="Yelp", dataset="Business", size=10_000
    )
    assert result.structured["sampleSize"] == MAX_SIZE
    assert f"LIMIT {MAX_SIZE};" in cap.last_query_form()["statement"]


async def test_size_floor(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([]))
    result = await run_sample_dataset(
        cap.client, settings, dataverse="Yelp", dataset="Business", size=0
    )
    assert result.structured["sampleSize"] == 1


async def test_non_list_results_wrapped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler({"v": 1}))
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Business")
    assert result.structured["results"] == [{"v": 1}]


async def test_unknown_dataverse_suggests(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([]))
    result = await run_sample_dataset(cap.client, settings, dataverse="Ylp", dataset="Business")
    assert result.is_error is True
    assert "Did you mean: Yelp?" in result.structured["errorMessage"]


async def test_unknown_dataset_suggests(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([]))
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Businesss")
    assert result.is_error is True
    assert "Did you mean: Business?" in result.structured["errorMessage"]


async def test_unknown_dataset_no_suggestion(settings: Settings) -> None:
    cap = make_capturing_cc(settings, handler=_handler([]))
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="zzzzzz")
    assert result.is_error is True
    assert result.structured["errorMessage"].endswith("was not found.")


async def test_cc_error_becomes_error_result(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX9999", "msg": "boom"}]}
    cap = make_capturing_cc(settings, response_json=body)
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Business")
    assert result.is_error is True
    assert result.structured["errorType"] == "INTERNAL"


async def test_sample_clamps_and_bounds_egress(settings: Settings) -> None:
    from urllib.parse import parse_qs

    import httpx

    big = "r" * (settings.max_field_chars + 200)

    def handler(req: httpx.Request) -> httpx.Response:
        stmt = parse_qs(req.content.decode())["statement"][0]
        if "Metadata" in stmt:
            rows: list = [{"DataverseName": "Yelp", "DatasetName": "Review"}]
        else:
            rows = [{"review_id": "x", "text": big}]
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_sample_dataset(cap.client, settings, dataverse="Yelp", dataset="Review")
    assert "[clamped," in result.structured["results"][0]["text"]
    assert result.structured["egress"]["truncated"] is False
