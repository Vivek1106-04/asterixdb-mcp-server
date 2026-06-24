"""Unit tests for the execute_query tool core (windowing, metrics, error mapping)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.execute_query import MAX_LIMIT, run_execute_query
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


async def test_overflow_writes_downloadable_artifact(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"artifacts_dir": str(tmp_path)})
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(
        cap.client, settings, statement="SELECT i FROM x LIMIT 10;", limit=3
    )

    artifact = result.structured["egress"]["artifact"]
    assert artifact["totalRows"] == 10  # the full set, not just the 3-row window
    assert json.loads(Path(artifact["localPath"]).read_bytes()) == rows


async def test_no_overflow_attaches_no_artifact(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"artifacts_dir": str(tmp_path)})
    rows = [{"i": n} for n in range(3)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(cap.client, settings, statement="SELECT 1;", limit=20)

    assert "artifact" not in result.structured["egress"]
    assert list(tmp_path.iterdir()) == []  # nothing written


async def test_overflow_with_artifacts_disabled_attaches_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(
        update={"artifacts_dir": str(tmp_path), "artifacts_enabled": False}
    )
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(
        cap.client, settings, statement="SELECT i FROM x LIMIT 10;", limit=3
    )

    assert result.structured["moreAvailable"] is True  # overflow signalled
    assert "artifact" not in result.structured["egress"]  # but no file referenced
    assert list(tmp_path.iterdir()) == []


async def test_download_format_override_is_honored(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"artifacts_dir": str(tmp_path)})
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(
        cap.client, settings, statement="SELECT i FROM x LIMIT 10;", limit=3, download_format="txt"
    )

    artifact = result.structured["egress"]["artifact"]
    assert artifact["format"] == "txt"
    assert Path(artifact["localPath"]).suffix == ".txt"


async def test_windows_rows_by_offset_and_limit(settings: Settings) -> None:
    rows = [{"i": n} for n in range(10)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(
        cap.client, settings, statement="SELECT i FROM x LIMIT 10;", offset=2, limit=3
    )

    assert result.is_error is False
    assert result.structured["rowsReturned"] == 3
    assert result.structured["results"] == rows[2:5]
    assert result.structured["moreAvailable"] is True
    assert result.structured["clientContextID"].startswith("sess-test::")


async def test_limit_and_offset_are_clamped(settings: Settings) -> None:
    rows = [{"i": n} for n in range(3)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})

    result = await run_execute_query(
        cap.client, settings, statement="SELECT 1;", offset=-5, limit=10_000
    )

    assert result.structured["offset"] == 0
    assert result.structured["limit"] == MAX_LIMIT
    assert result.structured["moreAvailable"] is False


async def test_metrics_and_signature_surfaced_when_present(settings: Settings) -> None:
    body = {
        "status": "success",
        "results": [{"a": 1}],
        "metrics": {"resultCount": 1, "elapsedTime": "5ms"},
        "signature": {"name": ["a"], "type": ["int64"]},
    }
    cap = make_capturing_cc(settings, response_json=body)

    result = await run_execute_query(
        cap.client, settings, statement="SELECT 1;", signature=True, profile=True
    )

    assert result.structured["metrics"] == body["metrics"]
    assert result.structured["signature"] == body["signature"]


async def test_non_list_results_are_wrapped(settings: Settings) -> None:
    # A scalar/object result (not a list) must still produce a one-row window.
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": {"v": 1}})
    result = await run_execute_query(cap.client, settings, statement="SELECT VALUE 1;")
    assert result.structured["rowsReturned"] == 1
    assert result.structured["results"] == [{"v": 1}]


async def test_summary_mentions_offset_when_nonzero(settings: Settings) -> None:
    rows = [{"i": n} for n in range(5)]
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": rows})
    result = await run_execute_query(cap.client, settings, statement="SELECT 1;", offset=1, limit=2)
    assert "offset 1" in result.text


async def test_dataverse_bool_param_and_warnings_are_handled(settings: Settings) -> None:
    body = {
        "status": "success",
        "results": [{"a": 1}],
        "warnings": [{"code": "ASX0001", "msg": "implicit cast"}],
    }
    cap = make_capturing_cc(settings, response_json=body)

    result = await run_execute_query(
        cap.client,
        settings,
        statement="SELECT 1;",
        dataverse="Analytics",
        compiler_parameters={"compiler.column.filter": True},
    )

    form = cap.last_query_form()
    assert form["dataverse"] == "Analytics"
    assert form["compiler.column.filter"] == "true"  # bool rendered to form string
    assert result.structured["warnings"] == body["warnings"]


async def test_cc_error_becomes_error_result(settings: Settings) -> None:
    body = {
        "status": "fatal",
        "errors": [{"code": "ASX1063", "msg": "A readonly query cannot contain ..."}],
    }
    cap = make_capturing_cc(settings, response_json=body)

    result = await run_execute_query(cap.client, settings, statement="DELETE FROM x;")

    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.READONLY_VIOLATION.value
    assert result.structured["retryable"] is False


async def test_auto_limit_appended_to_unbounded_select(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_execute_query(
        cap.client, settings, statement="SELECT a FROM Yelp.Review", limit=20
    )
    forwarded = cap.last_query_form()["statement"]
    assert forwarded == "SELECT a FROM Yelp.Review LIMIT 20;"
    assert result.structured["effectiveStatement"] == "SELECT a FROM Yelp.Review LIMIT 20;"


async def test_existing_limit_is_left_alone(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_execute_query(cap.client, settings, statement="SELECT a FROM x LIMIT 5;")
    assert cap.last_query_form()["statement"] == "SELECT a FROM x LIMIT 5;"


async def test_leading_set_clause_is_stripped(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_execute_query(
        cap.client,
        settings,
        statement="SET `compiler.parallelism` '8'; SELECT a FROM x LIMIT 3;",
    )
    assert cap.last_query_form()["statement"] == "SELECT a FROM x LIMIT 3;"


async def test_select_without_from_is_not_limited(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_execute_query(cap.client, settings, statement="SELECT 1;")
    form = cap.last_query_form()
    assert form["statement"] == "SELECT 1;"


async def test_blank_statement_is_passed_through(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    await run_execute_query(cap.client, settings, statement="   ")
    assert cap.last_query_form()["statement"] == "   "


async def test_unsupported_function_is_caught_before_cc(settings: Settings) -> None:
    cap = make_capturing_cc(settings, response_json={"status": "success", "results": []})
    result = await run_execute_query(
        cap.client, settings, statement="SELECT STDEV(x) FROM y LIMIT 5;"
    )
    assert result.is_error is True
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    # Guard short-circuits: no request reached the CC.
    assert cap.requests == []


async def test_columnar_full_scan_flagged_run_and_minimized(settings: Settings) -> None:
    from urllib.parse import parse_qs

    import httpx

    from asterixdb_mcp.plan_guard import ADVISORY_TYPE
    from asterixdb_mcp.tools.execute_query import COLUMNAR_FLAGGED_MAX_ROWS

    plan = {
        "status": "success",
        "plans": {
            "optimizedLogicalPlan": {
                "operator": "data-scan",
                "data-source": "DV.Cols",
                "inputs": [],
            }
        },
    }
    record = {"DataverseName": "DV", "DatasetName": "Cols", "DatasetFormat": {"Format": "column"}}
    data_rows = [{"i": n} for n in range(50)]

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("compile-only") == "true":
            return httpx.Response(200, json=plan)
        if "Metadata.`Dataset`" in form.get("statement", ""):
            return httpx.Response(200, json={"status": "success", "results": [record]})
        return httpx.Response(200, json={"status": "success", "results": data_rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_execute_query(
        cap.client, settings, statement="SELECT * FROM DV.Cols", limit=50
    )

    # Not blocked: the query ran and returned rows.
    assert result.is_error is False
    assert result.structured["status"] == "success"
    # Flagged with a non-fatal advisory naming the dataset.
    advisories = result.structured["advisories"]
    assert advisories[0]["type"] == ADVISORY_TYPE
    assert advisories[0]["datasets"] == ["DV.Cols"]
    # Output minimized: row window clamped to the flagged ceiling, truncation signalled.
    assert result.structured["rowsReturned"] == COLUMNAR_FLAGGED_MAX_ROWS
    assert result.structured["moreAvailable"] is True
    assert "output minimized" in result.text
