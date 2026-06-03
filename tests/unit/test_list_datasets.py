"""Unit tests for list_datasets (summary shape + pagination)."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.tools.list_datasets import MAX_EMPTY_PROBES, run_list_datasets
from tests.conftest import json_response, make_capturing_cc

pytestmark = pytest.mark.anyio


def _datasets(n: int) -> list[dict]:
    return [
        {
            "DataverseName": "DV",
            "DatasetName": f"ds{i}",
            "DatatypeName": "T",
            "DatasetType": "INTERNAL",
            "DatasetFormat": {"Format": "column" if i % 2 else "row"},
        }
        for i in range(n)
    ]


async def test_summarizes_each_dataset(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": _datasets(2)})
    result = await run_list_datasets(cap.client, settings)
    first = result.structured["datasets"][0]
    assert first == {
        "dataverse": "DV",
        "dataset": "ds0",
        "datatypeName": "T",
        "datasetType": "INTERNAL",
        "format": "ROW",
    }
    assert result.structured["datasets"][1]["format"] == "COLUMNAR"


async def test_pagination_reports_more_available(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": _datasets(5)})
    result = await run_list_datasets(cap.client, settings, offset=0, limit=2)
    assert result.structured["totalDatasets"] == 5
    assert len(result.structured["datasets"]) == 2
    assert result.structured["moreAvailable"] is True


async def test_dataverse_filter_is_parameterized(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_list_datasets(cap.client, settings, dataverse="My DV")
    form = cap.last_query_form()
    # The name is bound as a SQL++ named parameter, never spliced into the text.
    assert "d.DataverseName = $dataverse" in form["statement"]
    assert form["$dataverse"] == '"My DV"'


async def test_cc_error_becomes_error_result(settings: Settings) -> None:
    body = {"status": "fatal", "errors": [{"code": "ASX9999", "msg": "boom"}]}
    cap = make_capturing_cc(settings, response_json=body)
    result = await run_list_datasets(cap.client, settings)
    assert result.is_error is True
    assert result.structured["errorType"] == "INTERNAL"


def _record(dataverse: object, dataset: object) -> dict:
    return {"DataverseName": dataverse, "DatasetName": dataset, "DatasetFormat": {"Format": "row"}}


async def test_unique_names_have_no_collision_flag(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": _datasets(2)})
    result = await run_list_datasets(cap.client, settings)
    assert result.structured["nameCollisions"] == 0
    assert "nameCollision" not in result.structured["datasets"][0]


async def test_collision_flagged_and_emptiness_probed(settings: Settings) -> None:
    inventory = [_record("Default", "Business"), _record("Yelp", "Business")]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "Metadata" in stmt:
            rows: list = inventory
        elif "`Default`.`Business`" in stmt:
            rows = []  # empty
        elif "`Yelp`.`Business`" in stmt:
            rows = [1]  # populated
        else:
            rows = []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_datasets(cap.client, settings)
    by_dv = {d["dataverse"]: d for d in result.structured["datasets"]}
    assert by_dv["Default"]["nameCollision"] is True
    assert by_dv["Default"]["isEmpty"] is True
    assert by_dv["Yelp"]["isEmpty"] is False
    assert result.structured["nameCollisions"] == 2
    assert "span multiple dataverses" in result.text


async def test_collision_with_unprobeable_name_skips_isempty(settings: Settings) -> None:
    inventory = [_record("Default", "Bad-Name"), _record("Yelp", "Bad-Name")]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = inventory if "Metadata" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_datasets(cap.client, settings)
    first = result.structured["datasets"][0]
    assert first["nameCollision"] is True
    assert "isEmpty" not in first


async def test_collision_probe_cc_error_skips_isempty(settings: Settings) -> None:
    inventory = [_record("Default", "Business"), _record("Yelp", "Business")]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        if "Metadata" in stmt:
            return json_response({"status": "success", "results": inventory})
        return json_response({"status": "fatal", "errors": [{"code": "ASX9999", "msg": "x"}]})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_datasets(cap.client, settings)
    first = result.structured["datasets"][0]
    assert first["nameCollision"] is True
    assert "isEmpty" not in first


async def test_non_string_dataverse_and_dataset_in_collision(settings: Settings) -> None:
    inventory = [_record("A", "Dup"), _record(7, "Dup"), _record("A", 999)]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = inventory if "Metadata" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_list_datasets(cap.client, settings)
    datasets = result.structured["datasets"]
    non_str_dv = next(d for d in datasets if d["dataverse"] == 7)
    assert non_str_dv["nameCollision"] is True
    assert "isEmpty" not in non_str_dv
    # The record with a non-string dataset name is never flagged.
    non_str_ds = next(d for d in datasets if d["dataset"] == 999)
    assert "nameCollision" not in non_str_ds


async def test_emptiness_probe_is_capped(settings: Settings) -> None:
    names = [f"ds{i}" for i in range(MAX_EMPTY_PROBES + 2)]
    inventory = [_record(dv, name) for name in names for dv in ("Default", "Yelp")]

    def handler(request: httpx.Request) -> httpx.Response:
        stmt = parse_qs(request.content.decode())["statement"][0]
        rows = inventory if "Metadata" in stmt else []
        return json_response({"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler, status_code=200)
    result = await run_list_datasets(cap.client, settings, limit=MAX_EMPTY_PROBES * 2 + 10)
    probed = sum(1 for d in result.structured["datasets"] if "isEmpty" in d)
    assert probed == MAX_EMPTY_PROBES
