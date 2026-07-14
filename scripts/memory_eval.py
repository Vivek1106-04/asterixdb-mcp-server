#!/usr/bin/env python3
"""Offline evaluation harness for the agentic-memory layer.

Measures the memory store on four axes instead of asserting quality:

- **recall** — does retrieval surface the expected concepts for a question
  (hit@k and MRR over the blended subject/full-text ranking)?
- **forgetting** — does retrieval avoid superseded knowledge (stale-recall
  rate: any forbidden subject surfacing is a violation)?
- **efficiency** — context compression: characters retrieved for the case
  versus the whole current store (the cost of answering cold).
- **reuse** — active use of procedural memory: the retrieved docs must
  contain an expected reusable snippet (e.g. a proven query fragment), not
  merely mention the topic.

Cases are JSONL, one object per line::

    {"axis": "recall",     "query": "...", "expect_subjects": ["dv.ds"], "k": 8}
    {"axis": "forgetting", "query": "...", "forbid_subjects": ["dv.old"]}
    {"axis": "reuse",      "query": "...", "expect_snippet": "GROUP BY"}

The harness replays each case against the store through the same retrieval
shape the gateway's memory_search tool uses (subject key + full-text rank),
so scores move when the blend weights or the store change — which is the
point: tune against this, then re-run.

Usage:
    python scripts/memory_eval.py cases.jsonl [--cc URL] [--k 8] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from okf_refresh import execute

DEFAULT_K = 8
FT_FETCH_WINDOW = 100

SUBJECT_QUERY = (
    'SELECT VALUE m FROM Dashboard.Memory m WHERE m.subject = "{subject}" '
    "AND m.valid_to IS UNKNOWN;"
)
FULLTEXT_QUERY = (
    "SELECT VALUE m FROM Dashboard.Memory m "
    'WHERE ftcontains(m.`text`, [{tokens}], {{"mode": "any"}}) AND m.valid_to IS UNKNOWN '
    f"LIMIT {FT_FETCH_WINDOW};"
)
STORE_SIZE_QUERY = (
    "SELECT VALUE SUM(LENGTH(m.`text`)) FROM Dashboard.Memory m WHERE m.valid_to IS UNKNOWN;"
)


def tokenize(query: str) -> list[str]:
    return [
        token.lower()
        for token in "".join(ch if ch.isalnum() or ch == "_" else " " for ch in query).split()
    ]


def rank(docs: list[dict[str, Any]], tokens: list[str], k: int) -> list[dict[str, Any]]:
    """The gateway's client-side full-text ranking: token hit count, top k."""

    def score(doc: dict[str, Any]) -> int:
        haystack = (str(doc.get("text", "")) + " " + str(doc.get("subject", ""))).lower()
        return sum(haystack.count(token) for token in tokens)

    return sorted(docs, key=lambda d: -score(d))[:k]


def retrieve(cc: str, query: str, subject: str | None, k: int) -> list[dict[str, Any]]:
    """Subject key first, then ranked full text — the memory_search blend."""
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    if subject:
        for doc in execute(cc, SUBJECT_QUERY.format(subject=subject)).get("results", []):
            if isinstance(doc, dict) and str(doc.get("subject")) not in seen:
                seen.add(str(doc.get("subject")))
                docs.append(doc)
    tokens = tokenize(query)
    if tokens:
        token_list = ", ".join(f'"{t}"' for t in tokens)
        fetched = [
            doc
            for doc in execute(cc, FULLTEXT_QUERY.format(tokens=token_list)).get("results", [])
            if isinstance(doc, dict) and str(doc.get("subject")) not in seen
        ]
        docs.extend(rank(fetched, tokens, k))
    return docs[:k]


def score_recall(case: dict[str, Any], retrieved: list[dict[str, Any]]) -> dict[str, float]:
    """hit@k plus MRR of the first expected subject."""
    expected = set(case.get("expect_subjects", []))
    subjects = [str(doc.get("subject")) for doc in retrieved]
    hit = float(bool(expected & set(subjects)))
    reciprocal = 0.0
    for position, subj in enumerate(subjects, start=1):
        if subj in expected:
            reciprocal = 1.0 / position
            break
    return {"hit": hit, "mrr": reciprocal}


def score_forgetting(case: dict[str, Any], retrieved: list[dict[str, Any]]) -> dict[str, float]:
    forbidden = set(case.get("forbid_subjects", []))
    surfaced = forbidden & {str(doc.get("subject")) for doc in retrieved}
    return {"violations": float(len(surfaced))}


def score_reuse(case: dict[str, Any], retrieved: list[dict[str, Any]]) -> dict[str, float]:
    snippet = str(case.get("expect_snippet", ""))
    found = any(snippet in str(doc.get("text", "")) for doc in retrieved) if snippet else False
    return {"reused": float(found)}


def score_efficiency(retrieved: list[dict[str, Any]], store_chars: int) -> dict[str, float]:
    retrieved_chars = sum(len(str(doc.get("text", ""))) for doc in retrieved)
    ratio = retrieved_chars / store_chars if store_chars else 0.0
    return {"retrieved_chars": float(retrieved_chars), "compression": ratio}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        if case.get("axis") not in ("recall", "forgetting", "reuse"):
            raise ValueError(f"line {line_number}: unknown axis {case.get('axis')!r}")
        cases.append(case)
    return cases


def evaluate(cc: str, cases: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """Run every case and aggregate per-axis metrics."""
    store_rows = execute(cc, STORE_SIZE_QUERY).get("results", [])
    store_chars = store_rows[0] if store_rows and isinstance(store_rows[0], int) else 0

    per_axis: dict[str, list[dict[str, float]]] = {"recall": [], "forgetting": [], "reuse": []}
    efficiencies: list[dict[str, float]] = []
    for case in cases:
        retrieved = retrieve(cc, str(case.get("query", "")), case.get("subject"), k)
        efficiencies.append(score_efficiency(retrieved, store_chars))
        axis = case["axis"]
        scorer = {"recall": score_recall, "forgetting": score_forgetting, "reuse": score_reuse}[
            axis
        ]
        per_axis[axis].append(scorer(case, retrieved))

    report: dict[str, Any] = {"cases": len(cases), "k": k, "store_chars": store_chars}
    if per_axis["recall"]:
        rows = per_axis["recall"]
        report["recall"] = {
            "cases": len(rows),
            "hit_rate": sum(r["hit"] for r in rows) / len(rows),
            "mrr": sum(r["mrr"] for r in rows) / len(rows),
        }
    if per_axis["forgetting"]:
        rows = per_axis["forgetting"]
        violations = sum(r["violations"] for r in rows)
        report["forgetting"] = {
            "cases": len(rows),
            "violations": int(violations),
            "stale_recall_rate": violations / len(rows),
        }
    if per_axis["reuse"]:
        rows = per_axis["reuse"]
        report["reuse"] = {
            "cases": len(rows),
            "reuse_rate": sum(r["reused"] for r in rows) / len(rows),
        }
    if efficiencies:
        report["efficiency"] = {
            "mean_retrieved_chars": sum(e["retrieved_chars"] for e in efficiencies)
            / len(efficiencies),
            "mean_compression": sum(e["compression"] for e in efficiencies) / len(efficiencies),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="JSONL case file")
    parser.add_argument("--cc", default="http://localhost:19002", help="CC base URL")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="Retrieval depth")
    parser.add_argument("--json", action="store_true", help="Machine-readable report")
    options = parser.parse_args()

    report = evaluate(options.cc, load_cases(options.cases), options.k)
    if options.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"memory_eval: {report['cases']} cases @ k={report['k']}")
    for axis in ("recall", "forgetting", "reuse", "efficiency"):
        if axis in report:
            metrics = ", ".join(
                f"{key}={value:.3f}" if isinstance(value, float) else f"{key}={value}"
                for key, value in report[axis].items()
            )
            print(f"  {axis}: {metrics}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
