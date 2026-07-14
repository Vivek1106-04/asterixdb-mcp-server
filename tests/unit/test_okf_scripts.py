"""Unit tests for the OKF pipeline scripts (pure logic, no cluster)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import okf_bundle  # noqa: E402
import memory_eval  # noqa: E402
import okf_consolidate  # noqa: E402
import okf_refresh  # noqa: E402


def _doc(subject: str, text: str) -> dict:
    return {"subject": subject, "type": "AsterixDB Dataset", "text": text}


def test_reconcile_unchanged_is_noop() -> None:
    bundle = {"a.b": _doc("a.b", "same")}
    current = {"a.b": {**_doc("a.b", "same"), "id": "a.b@t0", "valid_from": "t0"}}
    inserts, supersede, unchanged = okf_refresh.reconcile(bundle, current, "t1", None)
    assert (inserts, supersede, unchanged) == ([], [], 1)


def test_reconcile_changed_supersedes_and_inserts() -> None:
    bundle = {"a.b": _doc("a.b", "new")}
    current = {"a.b": {**_doc("a.b", "old"), "id": "a.b@t0", "valid_from": "t0"}}
    inserts, supersede, unchanged = okf_refresh.reconcile(bundle, current, "t1", None)
    assert unchanged == 0
    assert supersede[0]["valid_to"] == "t1" and supersede[0]["id"] == "a.b@t0"
    assert inserts[0]["id"] == "a.b@t1" and inserts[0]["valid_from"] == "t1"


def test_reconcile_vanished_superseded_only_in_scope() -> None:
    current = {
        "a.b": {**_doc("a.b", "x"), "id": "1"},
        "z.k": {**_doc("z.k", "y"), "id": "2"},
    }
    _, supersede, _ = okf_refresh.reconcile({}, current, "t1", "a")
    assert [row["subject"] for row in supersede] == ["a.b"]
    _, supersede_all, _ = okf_refresh.reconcile({}, current, "t1", None)
    assert {row["subject"] for row in supersede_all} == {"a.b", "z.k"}


def test_collect_recommended_parses_real_advise_shape() -> None:
    results = [
        [
            {
                "#operator": "Advise",
                "advice": {
                    "#operator": "IndexAdvice",
                    "adviseinfo": {
                        "current_indexes": [{"index_statement": "CREATE INDEX cur ..."}],
                        "recommended_indexes": {
                            "indexes": [{"index_statement": "CREATE INDEX rec ON `a`.`b`(x);"}]
                        },
                    },
                },
            }
        ]
    ]
    out: set[str] = set()
    okf_refresh._collect_recommended(results, out, under_recommended=False)
    assert out == {"CREATE INDEX rec ON `a`.`b`(x);"}


def test_concept_paths_by_type() -> None:
    cases = {
        ("dv", "AsterixDB Dataverse"): "dv/dv.md",
        ("dv.ds", "AsterixDB Dataset"): "dv/datasets/ds.md",
        ("dv.ds", "AsterixDB View"): "dv/datasets/ds.md",
        ("dv.ds/index/idx", "AsterixDB Index"): "dv/indexes/ds.idx.md",
        ("dv/type/T", "AsterixDB Datatype"): "dv/types/T.md",
    }
    for (subject, concept_type), expected in cases.items():
        path = okf_bundle.concept_path({"subject": subject, "type": concept_type})
        assert path is not None and path.as_posix() == expected


def test_frontmatter_round_trip() -> None:
    doc = {
        "subject": "dv.ds",
        "type": "AsterixDB Dataset",
        "title": "ds",
        "description": 'has "quotes" and: colons',
        "tags": ["a", "b"],
        "links": ["dv/type/T"],
        "timestamp": "2026-07-09T00:00:00Z",
    }
    raw = okf_bundle.render_frontmatter(doc) + "\n\nbody\n"
    meta, body = okf_bundle.parse_frontmatter(raw)
    for key, value in doc.items():
        assert meta[key] == value
    assert body == "body\n"


def test_parse_frontmatter_without_header() -> None:
    meta, body = okf_bundle.parse_frontmatter("just a body\n")
    assert meta == {} and body == "just a body\n"


def test_drop_related_section_keeps_content_after_it() -> None:
    body = "# Schema\n\nx\n\n# Related\n\n- [a](a.md)\n\n# Notes\n\nkeep me\n"
    out = okf_bundle._drop_related_section(body)
    assert "# Related" not in out and "a.md" not in out
    assert "# Schema" in out and "keep me" in out


def test_drop_related_section_at_end() -> None:
    body = "# Schema\n\nx\n\n# Related\n\n- [a](a.md)\n"
    out = okf_bundle._drop_related_section(body)
    assert out == "# Schema\n\nx\n"


def test_render_concept_adds_relative_related_links() -> None:
    doc = {**_doc("dv.ds", "# Schema\n\nx\n"), "links": ["dv/type/T", "missing"]}
    paths = {
        "dv.ds": Path("dv/datasets/ds.md"),
        "dv/type/T": Path("dv/types/T.md"),
    }
    out = okf_bundle.render_concept(doc, paths)
    assert "- [dv/type/T](../../dv/types/T.md)" in out
    assert "missing" not in out.split("# Related", 1)[1]


CORE_V1 = "# Schema\n\n- `price`: int\n- `city`: string\n"
CORE_V2 = "# Schema\n\n- `city`: string\n"  # `price` vanished
OVERLAY = "# Notes\n\n- `price` is in cents\n- grain: one row per listing\n"


def test_merge_layers_is_core_when_overlay_empty() -> None:
    assert okf_refresh.merge_layers(CORE_V1, "") == CORE_V1


def test_merge_layers_appends_overlay_seamlessly() -> None:
    merged = okf_refresh.merge_layers(CORE_V1, OVERLAY)
    assert merged.startswith(CORE_V1.rstrip("\n"))
    assert merged.endswith("one row per listing\n")
    assert "<!--" not in merged  # no markers: the split is invisible


def test_reground_overlay_drops_claims_on_vanished_schema_elements() -> None:
    kept, dropped = okf_refresh.reground_overlay(OVERLAY, CORE_V1, CORE_V2)
    assert "grain: one row per listing" in kept
    assert "`price` is in cents" not in kept
    assert dropped == ["- `price` is in cents"]


def test_reground_overlay_keeps_references_that_never_resolved() -> None:
    overlay = "- `revenue` is derived downstream\n"
    kept, dropped = okf_refresh.reground_overlay(overlay, CORE_V1, CORE_V2)
    assert kept == overlay and dropped == []


def test_reground_overlay_empty_is_noop() -> None:
    assert okf_refresh.reground_overlay("", CORE_V1, CORE_V2) == ("", [])


def _stored(subject: str, core: str, overlay: str) -> dict:
    return {
        "subject": subject,
        "type": "AsterixDB Dataset",
        "id": f"{subject}@t0",
        "valid_from": "t0",
        "core": core,
        "overlay": overlay,
        "text": okf_refresh.merge_layers(core, overlay),
    }


def test_rewalk_preserves_overlay_when_core_unchanged() -> None:
    bundle = {"a.b": _doc("a.b", CORE_V1)}
    current = {"a.b": _stored("a.b", CORE_V1, OVERLAY)}
    inserts, supersede, unchanged = okf_refresh.reconcile(bundle, current, "t1", None)
    assert (inserts, supersede, unchanged) == ([], [], 1)


def test_rewalk_refreshes_core_and_regrounds_overlay() -> None:
    bundle = {"a.b": _doc("a.b", CORE_V2)}
    current = {"a.b": _stored("a.b", CORE_V1, OVERLAY)}
    inserts, supersede, _ = okf_refresh.reconcile(bundle, current, "t1", None)
    assert supersede[0]["overlay"] == OVERLAY  # history keeps the dropped claim
    row = inserts[0]
    assert row["core"] == CORE_V2
    assert "grain: one row per listing" in row["overlay"]
    assert "`price` is in cents" not in row["overlay"]
    assert row["text"] == okf_refresh.merge_layers(CORE_V2, row["overlay"])


def test_import_with_explicit_overlay_supersedes_on_overlay_edit() -> None:
    doc = {**_doc("a.b", CORE_V1), "core": CORE_V1, "overlay": "# Notes\n\n- edited\n"}
    current = {"a.b": _stored("a.b", CORE_V1, OVERLAY)}
    inserts, supersede, unchanged = okf_refresh.reconcile({"a.b": doc}, current, "t1", None)
    assert unchanged == 0 and len(supersede) == 1
    assert inserts[0]["overlay"] == "# Notes\n\n- edited\n"
    assert inserts[0]["core"] == CORE_V1


def test_vanished_guard_spares_non_walk_concepts() -> None:
    current = {
        "a.b": {**_doc("a.b", "x"), "id": "1"},
        "team/glossary": {"subject": "team/glossary", "type": "Note", "text": "y", "id": "2"},
    }
    _, supersede, _ = okf_refresh.reconcile({}, current, "t1", None)
    assert [row["subject"] for row in supersede] == ["a.b"]


def test_split_layers_fast_path_on_exported_shape() -> None:
    body = okf_refresh.merge_layers(CORE_V1, OVERLAY)
    core, overlay = okf_bundle.split_layers(body, CORE_V1)
    assert core == CORE_V1 and overlay == OVERLAY


def test_split_layers_fallback_when_body_restructured() -> None:
    body = "- `city`: string\n\n# Notes\n\n- hand-written claim\n\n# Schema\n\n- `price`: int\n"
    core, overlay = okf_bundle.split_layers(body, CORE_V1)
    assert core == CORE_V1
    assert "- hand-written claim" in overlay
    assert "`price`: int" not in overlay  # literal core lines stay core


def test_split_layers_without_stored_core_is_all_core() -> None:
    assert okf_bundle.split_layers("fresh body\n", "") == ("fresh body\n", "")


def _fake_execute(responses: dict[str, list]) -> tuple[list, object]:
    calls: list[str] = []

    def fake(cc: str, statement: str) -> dict:
        calls.append(statement)
        query = statement.removeprefix(okf_refresh.PIPELINE_MARKER).strip()
        if query not in responses:
            raise RuntimeError("boom")
        return {"results": responses[query]}

    return calls, fake


def test_revalidate_supersedes_on_drift_and_stamps_first_sight(monkeypatch) -> None:
    q_drift, q_new = "SELECT drift;", "SELECT new;"
    _, fake = _fake_execute({q_drift: [{"cnt": 2}], q_new: [{"cnt": 5}]})
    monkeypatch.setattr(okf_refresh, "execute", fake)
    current = {
        "m1": {
            "id": "m1",
            "source_query": q_drift,
            "grounding_digest": okf_refresh._digest([{"cnt": 1}]),
        },
        "m2": {"id": "m2", "source_query": q_new},
        "m3": {"id": "m3", "text": "no query"},
    }
    supersede, stamp, checked = okf_refresh.revalidate("cc", current, "t1")
    assert checked == 2
    assert [row["id"] for row in supersede] == ["m1"]
    assert supersede[0]["valid_to"] == "t1" and supersede[0]["superseded_by"] == "revalidation"
    assert stamp[0]["id"] == "m2"
    assert stamp[0]["grounding_digest"] == okf_refresh._digest([{"cnt": 5}])


def test_revalidate_matching_digest_and_failed_query_leave_row_alone(monkeypatch) -> None:
    q_ok = "SELECT ok;"
    digest = okf_refresh._digest([{"cnt": 7}])
    _, fake = _fake_execute({q_ok: [{"cnt": 7}]})
    monkeypatch.setattr(okf_refresh, "execute", fake)
    current = {
        "m1": {"id": "m1", "source_query": q_ok, "grounding_digest": digest},
        "m2": {"id": "m2", "source_query": "SELECT unreachable;", "grounding_digest": "x"},
    }
    supersede, stamp, checked = okf_refresh.revalidate("cc", current, "t1")
    assert (supersede, stamp, checked) == ([], [], 2)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _learned(subject: str, **extra) -> dict:
    return {"subject": subject, "type": "Note", "id": f"{subject}@t0", "valid_from": "t0", **extra}


def test_dedup_keeps_newest_current_row_per_subject() -> None:
    rows = [
        {"subject": "a", "id": "a@t0", "valid_from": "2026-01-01"},
        {"subject": "a", "id": "a@t1", "valid_from": "2026-06-01"},
        {"subject": "b", "id": "b@t0", "valid_from": "2026-01-01"},
    ]
    kept, supersede = okf_consolidate.dedup(rows, "t2")
    assert {row["id"] for row in kept} == {"a@t1", "b@t0"}
    assert supersede[0]["id"] == "a@t0"
    assert supersede[0]["superseded_by"] == "consolidation-dedup"


def test_decayed_trust_halves_after_one_half_life() -> None:
    row = _learned("a", last_used="2026-06-14T00:00:00+00:00")  # 30 idle days
    assert okf_consolidate.decayed_trust(row, NOW, half_life_days=30.0) == pytest.approx(0.5)


def test_decayed_trust_access_count_stretches_half_life() -> None:
    row = _learned("a", last_used="2026-06-14T00:00:00+00:00", access_count=2)
    assert okf_consolidate.decayed_trust(row, NOW, 30.0) == pytest.approx(0.5 ** (1 / 2))


def test_decayed_trust_falls_back_to_valid_from_then_now() -> None:
    dated = _learned("a")
    dated["valid_from"] = "2026-06-14T00:00:00+00:00"
    assert okf_consolidate.decayed_trust(dated, NOW, 30.0) == pytest.approx(0.5)
    undated = {"subject": "a", "valid_from": "not-a-date"}
    assert okf_consolidate.decayed_trust(undated, NOW, 30.0) == pytest.approx(1.0)


def test_consolidate_updates_prunes_and_exempts() -> None:
    walk_owned = {"subject": "dv.ds", "type": "AsterixDB Dataset", "id": "w", "valid_from": "t0"}
    fresh = _learned("fresh", last_used=NOW.isoformat(), trust=1.0)
    fading = _learned("fading", last_used="2026-06-14T00:00:00+00:00")
    forgotten = _learned("forgotten", last_used="2025-07-14T00:00:00+00:00")
    updates, supersede, exempt = okf_consolidate.consolidate(
        [walk_owned, fresh, fading, forgotten], NOW, half_life_days=30.0, trust_floor=0.2
    )
    assert exempt == 1
    assert [row["subject"] for row in updates] == ["fading"]
    assert updates[0]["trust"] == pytest.approx(0.5)
    assert [row["subject"] for row in supersede] == ["forgotten"]
    assert supersede[0]["superseded_by"] == "consolidation-decay"
    assert supersede[0]["valid_to"] == NOW.isoformat()


def test_eval_rank_orders_by_token_hits() -> None:
    docs = [
        {"subject": "a.low", "text": "revenue"},
        {"subject": "a.high", "text": "revenue revenue orders"},
    ]
    ranked = memory_eval.rank(docs, memory_eval.tokenize("orders revenue!"), k=1)
    assert [d["subject"] for d in ranked] == ["a.high"]


def test_eval_scorers() -> None:
    retrieved = [{"subject": "dv.ds", "text": "SELECT x FROM ds GROUP BY y"}]
    assert memory_eval.score_recall({"expect_subjects": ["dv.ds"]}, retrieved) == {
        "hit": 1.0,
        "mrr": 1.0,
    }
    assert memory_eval.score_recall({"expect_subjects": ["dv.other"]}, retrieved) == {
        "hit": 0.0,
        "mrr": 0.0,
    }
    assert memory_eval.score_forgetting({"forbid_subjects": ["dv.ds"]}, retrieved) == {
        "violations": 1.0
    }
    assert memory_eval.score_reuse({"expect_snippet": "GROUP BY"}, retrieved) == {"reused": 1.0}
    assert memory_eval.score_reuse({}, retrieved) == {"reused": 0.0}
    efficiency = memory_eval.score_efficiency(retrieved, store_chars=100)
    assert efficiency["compression"] == pytest.approx(len(retrieved[0]["text"]) / 100)
    assert memory_eval.score_efficiency([], store_chars=0)["compression"] == 0.0


def test_eval_load_cases_rejects_unknown_axis(tmp_path: Path) -> None:
    good = tmp_path / "cases.jsonl"
    good.write_text('{"axis": "recall", "query": "q"}\n\n{"axis": "reuse", "query": "r"}\n')
    assert [case["axis"] for case in memory_eval.load_cases(good)] == ["recall", "reuse"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"axis": "vibes"}\n')
    with pytest.raises(ValueError, match="unknown axis"):
        memory_eval.load_cases(bad)


def test_eval_evaluate_aggregates_all_axes(monkeypatch) -> None:
    store = {
        "shop.orders": {"subject": "shop.orders", "text": "orders schema GROUP BY day"},
        "shop.stale": {"subject": "shop.stale", "text": "irrelevant"},
    }

    def fake(cc: str, statement: str) -> dict:
        if statement.startswith("SELECT VALUE SUM"):
            return {"results": [1000]}
        if 'm.subject = "' in statement:
            subject = statement.split('m.subject = "', 1)[1].split('"', 1)[0]
            return {"results": [store[subject]] if subject in store else []}
        return {"results": [doc for doc in store.values() if "orders" in doc["text"]]}

    monkeypatch.setattr(memory_eval, "execute", fake)
    cases = [
        {"axis": "recall", "query": "orders", "expect_subjects": ["shop.orders"]},
        {
            "axis": "recall",
            "query": "orders",
            "subject": "shop.orders",
            "expect_subjects": ["shop.orders"],
        },
        {"axis": "forgetting", "query": "orders", "forbid_subjects": ["shop.stale"]},
        {"axis": "reuse", "query": "orders", "expect_snippet": "GROUP BY"},
    ]
    report = memory_eval.evaluate("cc", cases, k=8)
    assert report["recall"] == {"cases": 2, "hit_rate": 1.0, "mrr": 1.0}
    assert report["forgetting"]["violations"] == 0
    assert report["reuse"]["reuse_rate"] == 1.0
    assert report["efficiency"]["mean_compression"] > 0
