"""Unit tests for memory_search."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from asterixdb_mcp.config import Settings
from asterixdb_mcp.errors import ErrorType
from asterixdb_mcp.tools.memory_search import MAX_LIMIT, MAX_QUERY_LEN, run_memory_search
from tests.conftest import make_capturing_cc

pytestmark = pytest.mark.anyio


def _statement_of(req: httpx.Request) -> str:
    return parse_qs(req.content.decode()).get("statement", [""])[0]


def _doc(subject: str, text: str, links: list[str] | None = None) -> dict:
    return {
        "subject": subject,
        "type": "AsterixDB Dataset",
        "title": subject.rsplit(".", 1)[-1],
        "description": f"doc for {subject}",
        "text": text,
        "links": links or [],
        "tags": ["asterixdb"],
        "timestamp": "2026-07-09T00:00:00Z",
    }


def _store_handler(store: dict[str, dict]):
    """Answer the three retrieval passes from an in-memory concept store."""

    def handler(req: httpx.Request) -> httpx.Response:
        stmt = _statement_of(req)
        if 'm.subject = "' in stmt:
            subject = stmt.split('m.subject = "', 1)[1].split('"', 1)[0]
            rows = [store[subject]] if subject in store else []
        elif "ftcontains" in stmt:
            terms = stmt.split("[", 1)[1].split("]", 1)[0]
            tokens = [t.strip().strip('"') for t in terms.split(",")]
            rows = [d for d in store.values() if any(t in d["text"].lower() for t in tokens)]
        elif "m.subject IN [" in stmt:
            wanted = stmt.split("IN [", 1)[1].split("]", 1)[0]
            subjects = [s.strip().strip('"') for s in wanted.split(",")]
            rows = [store[s] for s in subjects if s in store]
        else:  # pragma: no cover - unexpected statement shape
            rows = []
        return httpx.Response(200, json={"status": "success", "results": rows})

    return handler


async def test_empty_query_and_subject_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_memory_search(cap.client, settings, query="   ")
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_overlong_query_rejected(settings: Settings) -> None:
    cap = make_capturing_cc(settings)
    result = await run_memory_search(cap.client, settings, query="x" * (MAX_QUERY_LEN + 1))
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value


@pytest.mark.parametrize("field", ["subject", "dataverse"])
async def test_invalid_identifier_rejected(settings: Settings, field: str) -> None:
    cap = make_capturing_cc(settings)
    result = await run_memory_search(
        cap.client, settings, query="orders", **{field: 'x"; DROP DATAVERSE Dashboard; --'}
    )
    assert result.structured["errorType"] == ErrorType.INVALID_PARAMETER.value
    assert cap.requests == []


async def test_subject_hit_is_first_and_current_only(settings: Settings) -> None:
    store = {"shop.orders": _doc("shop.orders", "orders schema")}
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(
        cap.client, settings, query="orders", subject="shop.orders", follow_links=False
    )
    matches = result.structured["matches"]
    assert matches[0]["subject"] == "shop.orders"
    assert matches[0]["via"] == "subject"
    # the same doc coming back from the full-text pass is deduplicated
    assert [m["subject"] for m in matches].count("shop.orders") == 1
    assert all("valid_to IS UNKNOWN" in _statement_of(r) for r in cap.requests)


async def test_fulltext_ranked_by_token_hits(settings: Settings) -> None:
    store = {
        "a.low": _doc("a.low", "revenue"),
        "a.high": _doc("a.high", "revenue revenue revenue orders"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(cap.client, settings, query="orders revenue")
    subjects = [m["subject"] for m in result.structured["matches"]]
    assert subjects == ["a.high", "a.low"]
    assert all(m["via"] == "fulltext" for m in result.structured["matches"])


async def test_link_expansion_one_hop(settings: Settings) -> None:
    store = {
        "shop.orders": _doc("shop.orders", "orders schema", links=["shop/type/OrderT"]),
        "shop/type/OrderT": _doc("shop/type/OrderT", "the datatype"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(cap.client, settings, query="orders")
    by_subject = {m["subject"]: m for m in result.structured["matches"]}
    assert by_subject["shop/type/OrderT"]["via"] == "link"


async def test_link_expansion_two_hops(settings: Settings) -> None:
    store = {
        "shop.orders": _doc("shop.orders", "orders schema", links=["shop/type/OrderT"]),
        "shop/type/OrderT": _doc("shop/type/OrderT", "the datatype", links=["shop.customers"]),
        "shop.customers": _doc("shop.customers", "linked dataset"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    one_hop = await run_memory_search(cap.client, settings, query="orders")
    assert "shop.customers" not in {m["subject"] for m in one_hop.structured["matches"]}

    two_hop = await run_memory_search(cap.client, settings, query="orders", link_depth=2)
    by_subject = {m["subject"]: m for m in two_hop.structured["matches"]}
    assert by_subject["shop/type/OrderT"]["via"] == "link"
    assert by_subject["shop.customers"]["via"] == "link-2"


async def test_link_depth_clamped_and_cycles_terminate(settings: Settings) -> None:
    store = {
        "shop.orders": _doc("shop.orders", "orders schema", links=["shop/type/OrderT"]),
        "shop/type/OrderT": _doc("shop/type/OrderT", "the datatype", links=["shop.orders"]),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(cap.client, settings, query="orders", link_depth=99)
    subjects = [m["subject"] for m in result.structured["matches"]]
    assert subjects == ["shop.orders", "shop/type/OrderT"]


async def test_link_expansion_skips_docs_already_seen(settings: Settings) -> None:
    orders = _doc("shop.orders", "orders schema", links=["shop/type/OrderT"])
    order_type = _doc("shop/type/OrderT", "the datatype")

    def handler(req: httpx.Request) -> httpx.Response:
        stmt = _statement_of(req)
        if "m.subject IN [" in stmt:  # the link pass echoes an already-seen doc back
            rows = [order_type, orders]
        elif "ftcontains" in stmt:
            rows = [orders]
        else:
            rows = []
        return httpx.Response(200, json={"status": "success", "results": rows})

    cap = make_capturing_cc(settings, handler=handler)
    result = await run_memory_search(cap.client, settings, query="orders")
    subjects = [m["subject"] for m in result.structured["matches"]]
    assert subjects == ["shop.orders", "shop/type/OrderT"]


async def test_follow_links_false_skips_expansion(settings: Settings) -> None:
    store = {
        "shop.orders": _doc("shop.orders", "orders schema", links=["shop/type/OrderT"]),
        "shop/type/OrderT": _doc("shop/type/OrderT", "the datatype"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(cap.client, settings, query="orders", follow_links=False)
    subjects = [m["subject"] for m in result.structured["matches"]]
    assert subjects == ["shop.orders"]


async def test_dataverse_scope_filters_matches(settings: Settings) -> None:
    store = {
        "shop.orders": _doc("shop.orders", "orders here"),
        "other.orders": _doc("other.orders", "orders there"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(
        cap.client, settings, query="orders", dataverse="shop", follow_links=False
    )
    subjects = [m["subject"] for m in result.structured["matches"]]
    assert subjects == ["shop.orders"]


async def test_cc_failure_degrades_to_empty(settings: Settings) -> None:
    cap = make_capturing_cc(settings, status_code=500, response_json={"status": "fatal"})
    result = await run_memory_search(cap.client, settings, query="orders")
    assert result.structured["matches"] == []
    assert not result.is_error
    assert "refresh" in result.text


async def test_limit_clamped_and_applied(settings: Settings) -> None:
    store = {f"dv.d{i}": _doc(f"dv.d{i}", "orders") for i in range(5)}
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(
        cap.client, settings, query="orders", limit=MAX_LIMIT + 100, follow_links=False
    )
    assert result.structured["limit"] == MAX_LIMIT
    result = await run_memory_search(
        cap.client, settings, query="orders", limit=2, follow_links=False
    )
    assert len(result.structured["matches"]) == 2


async def test_subject_only_skips_fulltext_pass(settings: Settings) -> None:
    store = {"shop.orders": _doc("shop.orders", "orders schema")}
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(
        cap.client, settings, query="", subject="shop.orders", follow_links=False
    )
    assert [m["via"] for m in result.structured["matches"]] == ["subject"]
    assert not any("ftcontains" in _statement_of(r) for r in cap.requests)


async def test_malformed_link_targets_not_followed(settings: Settings) -> None:
    store = {
        "shop.orders": _doc(
            "shop.orders", "orders schema", links=['bad"; DROP', "shop.orders", "shop/type/OrderT"]
        ),
        "shop/type/OrderT": _doc("shop/type/OrderT", "the datatype"),
    }
    cap = make_capturing_cc(settings, handler=_store_handler(store))
    result = await run_memory_search(cap.client, settings, query="orders")
    subjects = {m["subject"] for m in result.structured["matches"]}
    assert subjects == {"shop.orders", "shop/type/OrderT"}
    links_stmts = [s for s in map(_statement_of, cap.requests) if "IN [" in s]
    assert links_stmts and "DROP" not in links_stmts[0]
