"""Unit tests for the OKF pipeline scripts (pure logic, no cluster)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import memory_eval  # noqa: E402
import okf_bundle  # noqa: E402
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


def _recording_execute(results: list | None = None) -> tuple[list[tuple], object]:
    """Record (statement, parameters) for every call; answer with ``results``."""
    calls: list[tuple] = []

    def fake(cc: str, statement: str, parameters: dict | None = None) -> dict:
        calls.append((statement, parameters))
        return {"results": results or []}

    return calls, fake


def test_execute_binds_named_parameters_instead_of_splicing_them(monkeypatch) -> None:
    sent: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status": "success", "results": []}'

    def fake_urlopen(request):
        sent["body"] = request.data.decode()
        return _Response()

    monkeypatch.setattr(okf_refresh.urllib.request, "urlopen", fake_urlopen)
    okf_refresh.execute("http://cc", "SELECT 1;", {"principal": 'a"b'})
    # the value arrives JSON-encoded in its own field, never in the statement
    assert "%24principal=%22a%5C%22b%22" in sent["body"]
    assert "a%22b" not in sent["body"].split("&")[0]


def test_fetch_current_binds_the_principal_it_reads_as(monkeypatch) -> None:
    calls, fake = _recording_execute([{"subject": "a.b", "principal": "tenant-1"}])
    monkeypatch.setattr(okf_refresh, "execute", fake)
    current = okf_refresh.fetch_current("cc", "tenant-1")
    statement, parameters = calls[0]
    assert "$principal" in statement
    assert parameters == {"principal": "tenant-1"}
    assert set(current) == {"a.b"}


def test_fetch_current_drops_the_global_tier_it_does_not_own(monkeypatch) -> None:
    # the store's read predicate is "mine or global", so shared rows come back too;
    # reconciling them would let one tenant's walk retire a fact everyone reads
    _, fake = _recording_execute(
        [
            {"subject": "mine", "principal": "tenant-1"},
            {"subject": "shared", "principal": "*"},
        ]
    )
    monkeypatch.setattr(okf_refresh, "execute", fake)
    assert set(okf_refresh.fetch_current("cc", "tenant-1")) == {"mine"}


def test_apply_stamps_new_rows_with_their_owner(monkeypatch) -> None:
    calls, fake = _recording_execute()
    monkeypatch.setattr(okf_refresh, "execute", fake)
    okf_refresh.apply("cc", [{"id": "a@t1", "subject": "a"}], [], "tenant-1")
    assert '"principal": "tenant-1"' in calls[0][0]


def test_apply_leaves_a_superseded_rows_owner_alone(monkeypatch) -> None:
    calls, fake = _recording_execute()
    monkeypatch.setattr(okf_refresh, "execute", fake)
    okf_refresh.apply("cc", [], [{"id": "a@t0", "principal": "tenant-2"}], "tenant-1")
    assert '"principal": "tenant-2"' in calls[0][0]


def test_adopt_unowned_claims_legacy_rows_and_is_a_noop_when_there_are_none(monkeypatch) -> None:
    calls, fake = _recording_execute([{"id": "legacy-1"}, {"id": "legacy-2"}, "not-a-row"])
    monkeypatch.setattr(okf_refresh, "execute", fake)
    assert okf_refresh.adopt_unowned("cc", "tenant-1") == 2
    assert calls[1][0].startswith("UPSERT INTO AgentMemory.Memory")
    assert calls[1][0].count('"principal": "tenant-1"') == 2

    empty, fake_empty = _recording_execute([])
    monkeypatch.setattr(okf_refresh, "execute", fake_empty)
    assert okf_refresh.adopt_unowned("cc", "tenant-1") == 0
    assert len(empty) == 1  # read only; nothing to write


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


# memory_distill.py

import memory_distill  # noqa: E402


def test_distill_load_events_skips_malformed(tmp_path):
    log = tmp_path / "s1.jsonl"
    log.write_text('{"outcome": "success"}\nnot json\n[1,2]\n{"outcome": "error"}\n')
    events = memory_distill.load_events(tmp_path)
    assert [e["outcome"] for e in events] == ["success", "error"]


def test_distill_proven_queries_requires_distinct_sessions():
    stmt = "SELECT VALUE c FROM ShopDV.customers c LIMIT 5;"
    events = [
        {"outcome": "success", "statement": stmt, "session": "a"},
        {"outcome": "success", "statement": stmt, "session": "a"},
        {"outcome": "success", "statement": stmt, "session": "b"},
        {"outcome": "error", "statement": stmt, "session": "c"},
    ]
    proven = memory_distill.proven_queries(events, min_sessions=2)
    assert proven == [
        (
            "ShopDV.customers",
            f"Proven query, used successfully in 2 sessions: {stmt}",
            stmt,
        )
    ]
    assert memory_distill.proven_queries(events, min_sessions=3) == []


def test_distill_recurring_failures_excludes_resolved_subjects():
    fail = {"outcome": "error", "statement": "SELECT a FROM DV.a x;", "error": "QUERY_ERROR"}
    ok = {"outcome": "success", "statement": "SELECT a FROM DV.a x;"}
    events = [fail, fail, fail]
    cautions = memory_distill.recurring_failures(events, min_failures=3)
    assert cautions == [
        ("DV.a", "Caution: queries on this dataset failed 3 times with QUERY_ERROR.")
    ]
    assert memory_distill.recurring_failures([*events, ok], min_failures=3) == []


def test_distill_write_note_reconciles(monkeypatch):
    statements = []

    def fake_execute(cc, statement):
        statements.append(statement)
        return {"results": []}

    monkeypatch.setattr(memory_distill, "execute", fake_execute)
    action = memory_distill.write_note("http://cc", "DV.a", "note", "SELECT 1;")
    assert action == "created"
    assert any("INSERT INTO AgentMemory.Memory" in s for s in statements)


def test_distill_write_note_unchanged_writes_nothing(monkeypatch):
    existing = {"subject": "DV.a", "type": "Note", "text": "note"}

    def fake_execute(cc, statement):
        return {"results": [existing]}

    monkeypatch.setattr(memory_distill, "execute", fake_execute)
    assert memory_distill.write_note("http://cc", "DV.a", "note") == "unchanged"


def test_distill_load_cluster_events_degrades_to_empty(monkeypatch):
    def boom(cc, statement):
        raise RuntimeError("cluster down")

    monkeypatch.setattr(memory_distill, "execute", boom)
    assert memory_distill.load_cluster_events("http://cc") == []


def test_distill_load_cluster_events_returns_rows(monkeypatch):
    rows = [{"id": "e1", "outcome": "success"}, "not-a-dict"]

    def fake_execute(cc, statement):
        assert "SessionEvent" in statement
        return {"results": rows}

    monkeypatch.setattr(memory_distill, "execute", fake_execute)
    assert memory_distill.load_cluster_events("http://cc") == [{"id": "e1", "outcome": "success"}]


# memory_migrate_labels.py

import memory_migrate_labels  # noqa: E402


def test_migrate_overlay_prefixes_only_unmarked_blocks():
    overlay = (
        "The categories field is an array of strings.\n\n"
        "A query on this dataset failed (SYNTAX_ERROR): bad | working form: good\n\n"
        "Proven query, used successfully in 2 sessions: SELECT 1;\n\n"
        "(unverified) already labeled\n"
    )
    migrated, changed = memory_migrate_labels.migrate_overlay(overlay)
    assert changed == 1
    assert migrated.startswith("(unverified) The categories field")
    assert "| working form: good" in migrated
    assert "(unverified) A query" not in migrated
    assert "(unverified) Proven query" not in migrated
    assert migrated.count("(unverified)") == 2


def test_migrate_overlay_empty_is_noop():
    assert memory_migrate_labels.migrate_overlay("   \n") == ("", 0)


def test_migrate_rewrite_row_supersedes_bitemporally(monkeypatch):
    statements = []

    def fake_execute(cc, statement):
        statements.append(statement)
        return {"results": []}

    monkeypatch.setattr(memory_migrate_labels, "execute", fake_execute)
    row = {
        "id": "DV.a@t0",
        "subject": "DV.a",
        "type": "AsterixDB Dataset",
        "core": "core",
        "overlay": "claim\n",
        "text": "core\n\nclaim\n",
        "valid_from": "t0",
    }
    memory_migrate_labels.rewrite_row("http://cc", row, "(unverified) claim\n")
    assert statements[0].startswith("UPSERT INTO AgentMemory.Memory")
    assert '"valid_to"' in statements[0]
    assert statements[1].startswith("INSERT INTO AgentMemory.Memory")
    assert "(unverified) claim" in statements[1]


def test_migrate_main_reports_and_respects_dry_run(monkeypatch, capsys):
    row = {
        "id": "DV.a@t0",
        "subject": "DV.a",
        "type": "AsterixDB Dataset",
        "core": "core",
        "overlay": "claim\n",
        "text": "core\n\nclaim\n",
    }
    clean = {**row, "id": "DV.b@t0", "subject": "DV.b", "overlay": ""}
    statements = []

    def fake_execute(cc, statement):
        statements.append(statement)
        return {"results": [row, clean, "junk"]}

    monkeypatch.setattr(memory_migrate_labels, "execute", fake_execute)
    monkeypatch.setattr(sys, "argv", ["memory_migrate_labels.py", "--dry-run"])
    assert memory_migrate_labels.main() == 0
    out = capsys.readouterr().out
    assert "1 concepts with 1 unmarked block(s)" in out
    assert len(statements) == 1  # only the SELECT; dry-run writes nothing

    monkeypatch.setattr(sys, "argv", ["memory_migrate_labels.py"])
    assert memory_migrate_labels.main() == 0
    assert any(s.startswith("INSERT INTO AgentMemory.Memory") for s in statements)
