"""Unit tests for the OKF pipeline scripts (pure logic, no cluster)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import okf_bundle  # noqa: E402
import okf_refresh  # noqa: E402


# ---------------------------------------------------------------- reconcile


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


# ---------------------------------------------------------------- grounding


def test_collect_recommended_parses_real_advise_shape() -> None:
    results = [[{
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
    }]]
    out: set[str] = set()
    okf_refresh._collect_recommended(results, out, under_recommended=False)
    assert out == {"CREATE INDEX rec ON `a`.`b`(x);"}


# ---------------------------------------------------------------- bundle paths


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


# ---------------------------------------------------------------- frontmatter


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


# ---------------------------------------------------------------- related strip


def test_drop_related_section_keeps_content_after_it() -> None:
    body = "# Schema\n\nx\n\n# Related\n\n- [a](a.md)\n\n# Notes\n\nkeep me\n"
    out = okf_bundle._drop_related_section(body)
    assert "# Related" not in out and "a.md" not in out
    assert "# Schema" in out and "keep me" in out


def test_drop_related_section_at_end() -> None:
    body = "# Schema\n\nx\n\n# Related\n\n- [a](a.md)\n"
    out = okf_bundle._drop_related_section(body)
    assert out == "# Schema\n\nx\n"


# ---------------------------------------------------------------- export render


def test_render_concept_adds_relative_related_links() -> None:
    doc = {**_doc("dv.ds", "# Schema\n\nx\n"), "links": ["dv/type/T", "missing"]}
    paths = {
        "dv.ds": Path("dv/datasets/ds.md"),
        "dv/type/T": Path("dv/types/T.md"),
    }
    out = okf_bundle.render_concept(doc, paths)
    assert "- [dv/type/T](../../dv/types/T.md)" in out
    assert "missing" not in out.split("# Related", 1)[1]
