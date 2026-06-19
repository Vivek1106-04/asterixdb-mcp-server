"""Unit tests for the Hyracks job (physical plan) parser."""

from __future__ import annotations

from asterixdb_mcp.job_parser import parse_job


def test_parse_job_rejects_non_dict_plans_and_job() -> None:
    assert parse_job("nope") is None
    assert parse_job({"job": "not-a-dict"}) is None
    assert parse_job({}) is None


def test_parse_job_summarizes_operators_connectors_and_parallelism() -> None:
    parsed = parse_job(
        {
            "job": {
                "operators": [
                    {
                        "id": "ODID:1",
                        "java-class": "a.b.BTreeSearchOperatorDescriptor",
                        "in-arity": 0,
                        "out-arity": 1,
                        "partition-constraints": {"count": 8},
                    },
                    {
                        "id": "ODID:2",
                        "java-class": "a.b.BTreeSearchOperatorDescriptor",
                        "in-arity": 1,
                        "out-arity": 1,
                    },
                ],
                "connectors": [
                    {
                        "in-operator-id": "ODID:1",
                        "out-operator-id": "ODID:2",
                        "connector": {"java-class": "a.b.OneToOneConnectorDescriptor"},
                    }
                ],
            }
        }
    )
    assert parsed is not None
    out = parsed.to_dict()
    assert out["operatorCount"] == 2
    assert out["operatorCounts"] == {"BTreeSearch": 2}
    assert out["connectorCounts"] == {"OneToOne": 1}
    assert out["maxPartitionCount"] == 8
    # The arity-bearing operator serializes its arities; the count-less operator
    # omits partitionCount.
    assert out["operators"][0]["partitionCount"] == 8
    assert "partitionCount" not in out["operators"][1]


def test_parse_job_skips_non_dict_nodes() -> None:
    parsed = parse_job(
        {
            "job": {
                "operators": ["junk", {"id": "ODID:1", "java-class": "a.SortOperatorDescriptor"}],
                "connectors": "not-a-list",
            }
        }
    )
    assert parsed is not None
    assert parsed.operator_counts == {"Sort": 1}
    assert parsed.connectors == ()
    # No operator carried a partition count.
    assert parsed.max_partition_count is None


def test_parse_job_falls_back_to_display_name_and_handles_bad_types() -> None:
    parsed = parse_job(
        {
            "job": {
                "operators": [
                    {
                        # No java-class -> kind falls back to display-name.
                        "id": "ODID:9",
                        "display-name": "MysteryOp",
                        # bool must not be read as an int arity.
                        "in-arity": True,
                        "partition-constraints": "not-a-dict",
                    }
                ],
                # A connector whose nested connector is missing -> kind None.
                "connectors": [{"in-operator-id": "ODID:9", "out-operator-id": "ODID:9"}],
            }
        }
    )
    assert parsed is not None
    op = parsed.operators[0]
    assert op.kind == "MysteryOp"
    assert op.in_arity is None
    assert op.partition_count is None
    assert parsed.connectors[0].kind is None
    assert parsed.connector_counts == {}


def test_to_dict_omits_absent_arities_partition_and_parallelism() -> None:
    # operators field absent entirely -> empty tuple; the single op carries no
    # arities or partition count, so to_dict omits every optional and the job
    # omits maxPartitionCount.
    parsed = parse_job(
        {"job": {"operators": [{"id": "ODID:1", "java-class": "a.SortOperatorDescriptor"}]}}
    )
    assert parsed is not None
    out = parsed.to_dict()
    op = out["operators"][0]
    assert op == {"operatorId": "ODID:1", "kind": "Sort"}
    assert "inArity" not in op and "outArity" not in op and "partitionCount" not in op
    assert "maxPartitionCount" not in out


def test_parse_job_handles_operators_field_not_a_list() -> None:
    parsed = parse_job({"job": {"operators": "not-a-list"}})
    assert parsed is not None
    assert parsed.operators == ()


def test_kind_keeps_class_without_descriptor_suffix() -> None:
    # A class name that does not end in the noise suffix is kept verbatim.
    parsed = parse_job({"job": {"operators": [{"id": "x", "java-class": "a.b.EmptyTupleSource"}]}})
    assert parsed is not None
    assert parsed.operators[0].kind == "EmptyTupleSource"


def test_parse_job_strips_suffix_only_class_to_none() -> None:
    # A class that is *only* the noise suffix reduces to None, not "".
    parsed = parse_job(
        {"job": {"operators": [{"id": "x", "java-class": "OperatorDescriptor"}]}}
    )
    assert parsed is not None
    assert parsed.operators[0].kind is None
    assert parsed.operator_counts == {}
