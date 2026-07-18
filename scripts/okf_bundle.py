"""Export/import the agentic-memory store as an Open Knowledge Format bundle.

**Export** turns the current rows of ``AgentMemory.Memory`` into an OKF v0.1
bundle — a directory tree of markdown files with YAML frontmatter that any
OKF-aware agent (including Google's Knowledge Catalog) consumes with zero
translation, and a human browses on GitHub:

    <out>/
      index.md                      # bundle root: one entry per dataverse
      <dataverse>/
        index.md                    # progressive disclosure (OKF section 6)
        log.md                      # update history from superseded rows (OKF section 7)
        datasets/<name>.md          # dataset / view / external-dataset concepts
        types/<name>.md             # datatype concepts
        indexes/<dataset>.<idx>.md  # secondary-index concepts

Concept links are exported twice: machine-readable in frontmatter (``links``,
an extension key) and human-readable as a ``# Related`` section of relative
markdown links. ``log.md`` is synthesized from the store's bi-temporal chain —
superseded rows ARE the update history, so no separate changelog exists.

**Import** loads a bundle directory back into the store through the same
bi-temporal reconcile as the catalog refresh: unchanged concepts are skipped,
changed ones superseded, new ones inserted. Round-tripping an unchanged store
is a no-op. Each imported body is split against the store's deterministic
core first — the catalog walk owns the core, so bundle edits land in the
learned overlay, which the next re-walk carries forward instead of
clobbering.

Usage:
    python scripts/okf_bundle.py export <dir> [--cc URL] [--dataverse DV]
    python scripts/okf_bundle.py import <dir> [--cc URL]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okf_refresh import KIND, apply, execute, fetch_current, reconcile

# frontmatter keys exported in order; subject/source_query/links are OKF
# extension keys (producers MAY add keys; consumers MUST tolerate them)
FRONTMATTER_KEYS = (
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "timestamp",
    "subject",
    "source_query",
    "links",
)

DATAVERSE_TYPE = "AsterixDB Dataverse"
INDEX_TYPE = "AsterixDB Index"
TYPE_TYPE = "AsterixDB Datatype"

CURRENT_QUERY = (
    'SELECT VALUE m FROM AgentMemory.Memory m WHERE m.kind = "{kind}" AND m.valid_to IS UNKNOWN;'
)
HISTORY_QUERY = (
    "SELECT VALUE m FROM AgentMemory.Memory m "
    'WHERE m.kind = "{kind}" AND m.valid_to IS NOT UNKNOWN;'
)


def dataverse_of(subject: str) -> str:
    """The dataverse a concept belongs to, from its subject shape."""
    return subject.split("/", 1)[0].split(".", 1)[0]


def concept_path(doc: dict[str, Any]) -> Path | None:
    """Map a concept row to its bundle-relative file path (None for dataverse rows,
    which become index.md files rather than concept documents)."""
    subject = str(doc["subject"])
    concept_type = str(doc.get("type", ""))
    dataverse = dataverse_of(subject)
    if concept_type == DATAVERSE_TYPE:
        # the dataverse concept is a real document (like Google's datasets/<ds>.md);
        # index.md is generated separately and stays frontmatter-free per the spec
        return Path(dataverse) / f"{dataverse}.md"
    if concept_type == INDEX_TYPE:  # dv.ds/index/IDX
        dataset_part, _, index_name = subject.partition("/index/")
        dataset = dataset_part.split(".", 1)[1]
        return Path(dataverse) / "indexes" / f"{dataset}.{index_name}.md"
    if concept_type == TYPE_TYPE:  # dv/type/T
        return Path(dataverse) / "types" / f"{subject.rsplit('/', 1)[1]}.md"
    return Path(dataverse) / "datasets" / f"{subject.split('.', 1)[1]}.md"


# Emitted values are JSON-encoded scalars/lists, which are valid YAML — no
# YAML library needed for a faithful round-trip of our own bundles.


def render_frontmatter(doc: dict[str, Any]) -> str:
    lines = ["---"]
    for key in FRONTMATTER_KEYS:
        if key in doc and doc[key] is not None:
            lines.append(f"{key}: {json.dumps(doc[key])}")
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a concept file into (frontmatter dict, body)."""
    if not raw.startswith("---\n"):
        return {}, raw
    header, _, body = raw[4:].partition("\n---\n")
    meta: dict[str, Any] = {}
    for line in header.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            meta[key.strip()] = value
    return meta, body.lstrip("\n")


def render_concept(doc: dict[str, Any], paths: dict[str, Path]) -> str:
    """One concept row -> one OKF markdown document."""
    own = paths[str(doc["subject"])]
    body = str(doc.get("text", "")).rstrip() + "\n"
    related = [link for link in doc.get("links", []) or [] if str(link) in paths]
    if related:
        body += "\n# Related\n\n"
        for link in related:
            target = paths[str(link)]
            rel = Path(*([".."] * (len(own.parts) - 1))) / target
            body += f"- [{link}]({rel.as_posix()})\n"
    return render_frontmatter(doc) + "\n\n" + body


def render_index(dataverse_doc: dict[str, Any], members: list[dict[str, Any]]) -> str:
    """The dataverse's index.md (no frontmatter, per OKF section 6)."""
    lines = [f"# {dataverse_doc['subject']}", ""]
    for member in sorted(members, key=lambda m: str(m["subject"])):
        path = concept_path(member)
        rel = Path(*path.parts[1:]).as_posix()
        lines.append(
            f"* [{member.get('title', member['subject'])}]({rel}) - {member.get('description', '')}"
        )
    return "\n".join(lines) + "\n"


def render_log(history: list[dict[str, Any]]) -> str:
    """log.md from the bi-temporal chain: one entry per superseded fact."""
    lines = ["# Update history", ""]
    for row in sorted(history, key=lambda r: str(r.get("valid_to", ""))):
        lines.append(
            f"* {row.get('valid_to', '?')} — `{row.get('subject')}` superseded "
            f"(was current since {row.get('valid_from', '?')})"
        )
    return "\n".join(lines) + "\n"


def export_bundle(cc: str, out_dir: Path, dataverse: str | None) -> int:
    current = [
        row
        for row in execute(cc, CURRENT_QUERY.format(kind=KIND)).get("results", [])
        if isinstance(row, dict)
        and "subject" in row
        and (dataverse is None or dataverse_of(str(row["subject"])) == dataverse)
    ]
    history = [
        row
        for row in execute(cc, HISTORY_QUERY.format(kind=KIND)).get("results", [])
        if isinstance(row, dict)
        and "subject" in row
        and (dataverse is None or dataverse_of(str(row["subject"])) == dataverse)
    ]

    paths = {
        str(doc["subject"]): path for doc in current if (path := concept_path(doc)) is not None
    }
    by_dataverse: dict[str, list[dict[str, Any]]] = {}
    dataverse_docs: dict[str, dict[str, Any]] = {}
    for doc in current:
        if str(doc.get("type", "")) == DATAVERSE_TYPE:
            dataverse_docs[str(doc["subject"])] = doc
        else:
            by_dataverse.setdefault(dataverse_of(str(doc["subject"])), []).append(doc)

    for doc in current:
        path = concept_path(doc)
        if path is None:
            continue
        target = out_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_concept(doc, paths))

    for dataverse_name, members in by_dataverse.items():
        dv_doc = dataverse_docs.get(dataverse_name, {"subject": dataverse_name})
        (out_dir / dataverse_name).mkdir(parents=True, exist_ok=True)
        (out_dir / dataverse_name / "index.md").write_text(render_index(dv_doc, members))
        dv_history = [h for h in history if dataverse_of(str(h["subject"])) == dataverse_name]
        if dv_history:
            (out_dir / dataverse_name / "log.md").write_text(render_log(dv_history))

    root_lines = ["# Knowledge bundle", ""]
    for dataverse_name in sorted(by_dataverse):
        description = str(dataverse_docs.get(dataverse_name, {}).get("description", ""))
        root_lines.append(f"* [{dataverse_name}]({dataverse_name}/index.md) - {description}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.md").write_text("\n".join(root_lines) + "\n")
    return len(current)


def load_bundle(bundle_dir: Path) -> dict[str, dict[str, Any]]:
    """Parse a bundle directory back into concept docs keyed by subject.

    ``index.md``/``log.md`` are reserved files (OKF section 3.1), not concepts.
    Concepts without a ``subject`` extension key derive one from their path.
    """
    docs: dict[str, dict[str, Any]] = {}
    for path in sorted(bundle_dir.rglob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        meta, body = parse_frontmatter(path.read_text())
        body = _drop_related_section(body)
        subject = str(meta.get("subject") or path.relative_to(bundle_dir).with_suffix(""))
        doc = {key: meta[key] for key in FRONTMATTER_KEYS if key in meta}
        doc["subject"] = subject
        doc["text"] = body
        docs[subject] = doc
    return docs


def _drop_related_section(body: str) -> str:
    """Remove the export-generated ``# Related`` block (and only it)."""
    lines = body.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == "# Related":
            skipping = True
            continue
        if skipping and line.startswith("# "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def split_layers(body: str, stored_core: str) -> tuple[str, str]:
    """Split an imported concept body into (core, overlay).

    The catalog walk owns the deterministic core, so an import can only
    contribute overlay. Exported documents are ``core + overlay``, so a body
    that still starts with the stored core splits at that boundary. If the
    body was restructured by hand, any line that literally appears in the
    stored core stays core and everything else becomes overlay. Without a
    stored core (a concept this store has never seen) the body is its own
    core.
    """
    if not stored_core:
        return body, ""
    trimmed = stored_core.rstrip("\n")
    if body.startswith(trimmed):
        remainder = body[len(trimmed) :].strip("\n")
        return stored_core, remainder + "\n" if remainder else ""
    core_lines = set(trimmed.splitlines())
    overlay_lines = [line for line in body.splitlines() if line.strip() and line not in core_lines]
    overlay = "\n".join(overlay_lines)
    return stored_core, overlay + "\n" if overlay else ""


def import_bundle(cc: str, bundle_dir: Path) -> tuple[int, int, int]:
    docs = load_bundle(bundle_dir)
    current = fetch_current(cc)
    for subject, doc in docs.items():
        stored_core = str((current.get(subject) or {}).get("core") or "")
        core, overlay = split_layers(str(doc["text"]), stored_core)
        doc["core"] = core
        doc["overlay"] = overlay
    now = datetime.now(timezone.utc).isoformat()
    inserts, supersede, unchanged = reconcile(docs, current, now, scope="__import__")
    apply(cc, inserts, supersede)
    return len(inserts), len(supersede), unchanged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--cc", default="http://localhost:19002", help="CC base URL")
    parser.add_argument("--dataverse", default=None, help="Export only this dataverse")
    options = parser.parse_args()

    if options.action == "export":
        count = export_bundle(options.cc, options.directory, options.dataverse)
        print(f"okf_bundle: exported {count} concepts -> {options.directory}")
    else:
        inserted, superseded, unchanged = import_bundle(options.cc, options.directory)
        print(
            f"okf_bundle: imported | {inserted} inserted | "
            f"{superseded} superseded | {unchanged} unchanged"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
