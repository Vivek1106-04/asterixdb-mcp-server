"""Unit tests for the Hyracks runtime-profile summarizer."""

from __future__ import annotations

from asterixdb_mcp.profile_parser import MAX_OPERATORS, parse_profile


def _profile(*counters: dict[str, object]) -> dict[str, object]:
    return {
        "job-id": "JID:1",
        "joblets": [{"node-id": "nc1", "tasks": [{"counters": list(counters)}]}],
    }


def test_aggregates_counters_by_runtime_id_across_partitions() -> None:
    profile = {
        "job-id": "JID:1",
        "joblets": [
            {"node-id": "nc1", "tasks": [
                {"counters": [{"name": "scan", "runtime-id": "ODID:1",
                               "run-time": 5.0, "cardinality-out": 50, "pages-read": 2}]}
            ]},
            {"node-id": "nc2", "tasks": [
                {"counters": [{"name": "scan", "runtime-id": "ODID:1",
                               "run-time": 3.0, "cardinality-out": 70, "pages-read": 1}]}
            ]},
        ],
    }
    summary = parse_profile(profile)
    assert summary is not None
    assert summary.job_id == "JID:1"
    assert summary.operators == [
        {
            "operator": "scan",
            "runTimeMs": 8.0,
            "cardinalityOut": 120,
            "partitions": 2,
            "operatorId": "ODID:1",
            "pagesRead": 3,
        }
    ]


def test_operators_sorted_by_run_time_descending() -> None:
    summary = parse_profile(
        _profile(
            {"name": "a", "runtime-id": "1", "run-time": 1.0},
            {"name": "b", "runtime-id": "2", "run-time": 9.0},
        )
    )
    assert summary is not None
    assert [op["operator"] for op in summary.operators] == ["b", "a"]


def test_groups_by_name_when_no_runtime_id() -> None:
    summary = parse_profile(
        _profile(
            {"name": "agg", "run-time": 2.0},
            {"name": "agg", "run-time": 3.0},
        )
    )
    assert summary is not None
    assert len(summary.operators) == 1
    assert summary.operators[0]["runTimeMs"] == 5.0
    # No runtime-id means the operatorId field is omitted.
    assert "operatorId" not in summary.operators[0]


def test_pages_read_omitted_when_zero() -> None:
    summary = parse_profile(_profile({"name": "x", "run-time": 1.0}))
    assert summary is not None
    assert "pagesRead" not in summary.operators[0]


def test_counter_without_name_is_skipped() -> None:
    summary = parse_profile(_profile({"run-time": 1.0}, {"name": "real", "run-time": 2.0}))
    assert summary is not None
    assert [op["operator"] for op in summary.operators] == ["real"]


def test_non_dict_profile_returns_none() -> None:
    assert parse_profile(None) is None
    assert parse_profile("nope") is None


def test_missing_job_id_omits_field() -> None:
    summary = parse_profile({"joblets": []})
    assert summary is not None
    assert summary.to_dict() == {"operatorCount": 0, "operators": []}


def test_boolean_metrics_do_not_count_as_numbers() -> None:
    # A stray boolean must not be coerced to 1; run-time stays 0.
    summary = parse_profile(_profile({"name": "x", "run-time": True, "cardinality-out": True}))
    assert summary is not None
    assert summary.operators[0]["runTimeMs"] == 0.0
    assert summary.operators[0]["cardinalityOut"] == 0


def test_malformed_joblets_and_tasks_are_skipped() -> None:
    profile = {"joblets": ["bad", {"tasks": ["bad", {"counters": ["bad"]}]}]}
    summary = parse_profile(profile)
    assert summary is not None
    assert summary.operators == []


def test_operator_list_is_capped() -> None:
    counters = [
        {"name": f"op{i}", "runtime-id": str(i), "run-time": float(i)}
        for i in range(MAX_OPERATORS + 5)
    ]
    summary = parse_profile(_profile(*counters))
    assert summary is not None
    assert len(summary.operators) == MAX_OPERATORS
