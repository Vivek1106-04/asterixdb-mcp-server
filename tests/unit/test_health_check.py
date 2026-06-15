"""Unit tests for database_health_check."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.health_check import (
    CHECK_DUPLICATE_INDEX,
    CHECK_REDUNDANT_INDEX,
    assemble_findings,
    find_columnar_candidates,
    find_duplicate_indexes,
    find_redundant_indexes,
    run_database_health_check,
)
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _idx(name: str, dataset: str, keys: list, *, structure: str = "BTREE",
         primary: bool = False, dataverse: str = "S") -> dict:
    return {
        "IndexName": name,
        "DatasetName": dataset,
        "DataverseName": dataverse,
        "IndexStructure": structure,
        "SearchKey": keys,
        "IsPrimary": primary,
    }


def _ds(name: str, *, fmt: str = "row", kind: str = "INTERNAL", dataverse: str = "S") -> dict:
    record = {"DatasetName": name, "DataverseName": dataverse, "DatasetType": kind}
    if fmt:
        record["DatasetFormat"] = {"Format": fmt}
    return record


# pure analyzers


def test_find_duplicate_indexes_flags_same_structure_and_keys() -> None:
    indexes = [
        _idx("byCityA", "Orders", [["city"]]),
        _idx("byCityB", "Orders", [["city"]]),
    ]
    findings = find_duplicate_indexes(indexes)
    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == CHECK_DUPLICATE_INDEX
    assert f["severity"] == "high"
    assert f["indexes"] == ["byCityA", "byCityB"]
    assert f["keyFields"] == ["city"]


def test_duplicate_handles_bare_string_search_keys() -> None:
    # Some metadata rows carry SearchKey as bare strings rather than nested lists.
    indexes = [
        _idx("a", "Orders", ["city"]),
        _idx("b", "Orders", ["city"]),
    ]
    findings = find_duplicate_indexes(indexes)
    assert findings[0]["keyFields"] == ["city"]


def test_indexes_with_no_usable_keys_are_skipped() -> None:
    # An empty SearchKey (list) and a non-list SearchKey both yield no keys and
    # must not produce findings or crash.
    indexes = [
        _idx("empty", "Orders", []),
        _idx("malformed", "Orders", None),
    ]
    assert find_duplicate_indexes(indexes) == []
    assert find_redundant_indexes(indexes) == []


def test_duplicate_ignores_primary_and_different_structure() -> None:
    indexes = [
        _idx("pk", "Orders", [["id"]], primary=True),
        _idx("alsoPk", "Orders", [["id"]], primary=True),
        _idx("btree", "Orders", [["city"]], structure="BTREE"),
        _idx("rtree", "Orders", [["city"]], structure="RTREE"),
    ]
    assert find_duplicate_indexes(indexes) == []


def test_find_redundant_indexes_flags_prefix_cover() -> None:
    indexes = [
        _idx("short", "Orders", [["city"]]),
        _idx("long", "Orders", [["city"], ["zip"]]),
    ]
    findings = find_redundant_indexes(indexes)
    assert len(findings) == 1
    assert findings[0]["check"] == CHECK_REDUNDANT_INDEX
    assert findings[0]["index"] == "short"
    assert findings[0]["coveredBy"] == "long"


def test_redundant_not_flagged_when_not_a_prefix() -> None:
    indexes = [
        _idx("a", "Orders", [["zip"]]),
        _idx("b", "Orders", [["city"], ["zip"]]),
    ]
    assert find_redundant_indexes(indexes) == []


def test_redundant_requires_same_structure() -> None:
    indexes = [
        _idx("short", "Orders", [["city"]], structure="BTREE"),
        _idx("long", "Orders", [["city"], ["zip"]], structure="RTREE"),
    ]
    assert find_redundant_indexes(indexes) == []


def test_columnar_candidate_flags_internal_row_only() -> None:
    datasets = [
        _ds("RowOne"),
        _ds("ColOne", fmt="column"),
        _ds("ExternalRow", kind="EXTERNAL"),
        _ds("LegacyNoFormat", fmt=""),  # missing block defaults to ROW
    ]
    flagged = {f["dataset"] for f in find_columnar_candidates(datasets)}
    assert flagged == {"RowOne", "LegacyNoFormat"}


def test_assemble_orders_by_severity() -> None:
    datasets = [_ds("RowOne")]
    indexes = [
        _idx("dupA", "Orders", [["city"]]),
        _idx("dupB", "Orders", [["city"]]),
        _idx("short", "Items", [["sku"]]),
        _idx("long", "Items", [["sku"], ["lot"]]),
    ]
    findings = assemble_findings(datasets, indexes)
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert severities[0] == "high"
    assert severities[-1] == "low"


# run_ integration


def _catalog_handler(datasets: list[dict], indexes: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        statement = parse_qs(req.content.decode()).get("statement", [""])[0]
        if "Metadata.`Index`" in statement:
            return httpx.Response(200, json={"status": "success", "results": indexes})
        return httpx.Response(200, json={"status": "success", "results": datasets})

    return handler


async def test_run_reports_findings(settings: Settings) -> None:
    handler = _catalog_handler(
        [_ds("RowOne")],
        [_idx("dupA", "Orders", [["city"]]), _idx("dupB", "Orders", [["city"]])],
    )
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_database_health_check(cap.client, settings)
    assert result.structured["findingsCount"] == 2  # one duplicate + one columnar
    assert result.structured["datasetsScanned"] == 1
    assert "finding(s)" in result.text


async def test_run_clean_catalog_reports_no_issues(settings: Settings) -> None:
    handler = _catalog_handler([_ds("ColOne", fmt="column")], [])
    cap = make_capturing_cc(settings, handler=handler)
    result = await run_database_health_check(cap.client, settings)
    assert result.structured["findingsCount"] == 0
    assert "no schema-level issues" in result.text


async def test_run_scopes_to_dataverse_and_skips_metadata(settings: Settings) -> None:
    datasets = [
        _ds("Keep", dataverse="S"),
        _ds("Other", dataverse="T"),
        _ds("Dataset", dataverse="Metadata"),
    ]
    cap = make_capturing_cc(settings, handler=_catalog_handler(datasets, []))
    result = await run_database_health_check(cap.client, settings, dataverse="S")
    assert result.structured["dataverseFilter"] == "S"
    assert result.structured["datasetsScanned"] == 1
    assert result.structured["findings"][0]["dataset"] == "Keep"


async def test_run_surfaces_cc_error(settings: Settings) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"msg": "boom"}]})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_database_health_check(cap.client, settings)
    assert result.is_error
    assert result.structured["errorType"] == ErrorType.INTERNAL.value
