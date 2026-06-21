"""Unit tests for the plan-signal optimization hint deriver."""

from __future__ import annotations

from asterixdb_mcp.hint_deriver import derive_hints, hints_payload
from asterixdb_mcp.plan_parser import parse_optimized_plan


def _parse(tree: dict[str, object]):
    parsed = parse_optimized_plan({"optimizedLogicalPlan": tree})
    assert parsed is not None
    return parsed


def test_full_scan_yields_full_scan_hint() -> None:
    parsed = _parse(
        {
            "operator": "distribute-result",
            "inputs": [{"operator": "data-scan", "data-source": "Shop.Business", "inputs": []}],
        }
    )
    codes = {hint.code for hint in derive_hints(parsed)}
    assert codes == {"full-scan"}


def test_broadcast_exchange_yields_broadcast_hint() -> None:
    parsed = _parse(
        {
            "operator": "join",
            "inputs": [
                {
                    "operator": "exchange",
                    "physical-operator": "BROADCAST_EXCHANGE",
                    "inputs": [],
                }
            ],
        }
    )
    codes = {hint.code for hint in derive_hints(parsed)}
    assert codes == {"broadcast-join"}


def test_scan_and_broadcast_yield_both_hints() -> None:
    parsed = _parse(
        {
            "operator": "join",
            "physical-operator": "HYBRID_HASH_JOIN",
            "inputs": [
                {"operator": "data-scan", "data-source": "Shop.Review", "inputs": []},
                {"operator": "exchange", "physical-operator": "broadcast_exchange", "inputs": []},
            ],
        }
    )
    codes = {hint.code for hint in derive_hints(parsed)}
    assert codes == {"full-scan", "broadcast-join"}


def test_clean_plan_yields_no_hints() -> None:
    parsed = _parse({"operator": "distribute-result", "inputs": []})
    assert derive_hints(parsed) == []


def test_hints_payload_serializes_each_hint() -> None:
    parsed = _parse(
        {"operator": "distribute-result", "inputs": [{"operator": "data-scan", "inputs": []}]}
    )
    payload = hints_payload(parsed)
    assert payload == [
        {
            "code": "full-scan",
            "signal": payload[0]["signal"],
            "advice": payload[0]["advice"],
        }
    ]
    assert set(payload[0]) == {"code", "signal", "advice"}
