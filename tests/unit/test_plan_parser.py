"""Unit tests for the optimized-logical-plan JSON parser."""

from __future__ import annotations

from asterixdb_mcp.plan_parser import parse_optimized_plan

# A representative two-level plan: a select over a data-scan.
_SAMPLE_PLAN = {
    "optimizedLogicalPlan": {
        "operator": "distribute-result",
        "operatorId": "1.1",
        "physical-operator": "DISTRIBUTE_RESULT",
        "inputs": [
            {
                "operator": "select",
                "operatorId": "1.2",
                "condition": {"expressions": ["gt($$x, 3)"]},
                "inputs": [
                    {
                        "operator": "data-scan",
                        "operatorId": "1.3",
                        "data-source": "Yelp.Business",
                        "physical-operator": "DATASOURCE_SCAN",
                        "inputs": [],
                    }
                ],
            }
        ],
    }
}


def test_returns_none_for_non_dict() -> None:
    assert parse_optimized_plan("not a plan") is None
    assert parse_optimized_plan(None) is None


def test_returns_none_when_optimized_plan_absent() -> None:
    assert parse_optimized_plan({"logicalPlan": {}}) is None


def test_returns_none_when_optimized_plan_not_object() -> None:
    assert parse_optimized_plan({"optimizedLogicalPlan": "string-plan"}) is None


def test_parses_root_kind_and_id() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    assert parsed.root.kind == "distribute-result"
    assert parsed.root.operator_id == "1.1"
    assert parsed.root.physical_operator == "DISTRIBUTE_RESULT"


def test_operator_counts_cover_whole_tree() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    assert parsed.operator_counts == {
        "distribute-result": 1,
        "select": 1,
        "data-scan": 1,
    }


def test_data_sources_are_collected() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    assert parsed.data_sources == ("Yelp.Business",)


def test_depth_counts_longest_path() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    assert parsed.depth == 3


def test_predicates_extracted_from_condition() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    select_op = parsed.root.inputs[0]
    assert select_op.predicates == ("gt($$x, 3)",)


def test_condition_as_plain_string() -> None:
    plan = {"optimizedLogicalPlan": {"operator": "select", "condition": "eq($$a, 1)"}}
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.root.predicates == ("eq($$a, 1)",)


def test_top_level_expressions_collected() -> None:
    plan = {
        "optimizedLogicalPlan": {
            "operator": "assign",
            "expressions": ["concat($$a, $$b)"],
        }
    }
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.root.predicates == ("concat($$a, $$b)",)


def test_non_string_expressions_are_dropped() -> None:
    plan = {
        "optimizedLogicalPlan": {
            "operator": "assign",
            "expressions": ["ok", 42, None],
        }
    }
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.root.predicates == ("ok",)


def test_non_list_inputs_are_ignored() -> None:
    plan = {"optimizedLogicalPlan": {"operator": "sink", "inputs": "oops"}}
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.root.inputs == ()
    assert parsed.depth == 1


def test_non_dict_input_entries_are_skipped() -> None:
    plan = {
        "optimizedLogicalPlan": {
            "operator": "sink",
            "inputs": [{"operator": "exchange"}, "garbage", 5],
        }
    }
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert len(parsed.root.inputs) == 1
    assert parsed.root.inputs[0].kind == "exchange"


def test_missing_operator_field_yields_none_kind() -> None:
    plan = {"optimizedLogicalPlan": {"operatorId": "9.9"}}
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.root.kind is None
    assert parsed.operator_counts == {}


def test_duplicate_data_sources_deduped_in_order() -> None:
    plan = {
        "optimizedLogicalPlan": {
            "operator": "join",
            "inputs": [
                {"operator": "data-scan", "data-source": "A"},
                {"operator": "data-scan", "data-source": "B"},
                {"operator": "data-scan", "data-source": "A"},
            ],
        }
    }
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    assert parsed.data_sources == ("A", "B")


def test_to_dict_round_trips_structure() -> None:
    parsed = parse_optimized_plan(_SAMPLE_PLAN)
    assert parsed is not None
    rendered = parsed.to_dict()
    assert rendered["operatorCounts"]["data-scan"] == 1
    assert rendered["dataSources"] == ["Yelp.Business"]
    assert rendered["depth"] == 3
    assert rendered["tree"]["operator"] == "distribute-result"
    scan = rendered["tree"]["inputs"][0]["inputs"][0]
    assert scan["dataSource"] == "Yelp.Business"
    assert scan["physicalOperator"] == "DATASOURCE_SCAN"


def test_to_dict_omits_empty_optionals() -> None:
    plan = {"optimizedLogicalPlan": {"operator": "empty-tuple-source"}}
    parsed = parse_optimized_plan(plan)
    assert parsed is not None
    node = parsed.root.to_dict()
    assert node == {"operator": "empty-tuple-source"}
