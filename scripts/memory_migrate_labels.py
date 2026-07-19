"""One-time migration: label legacy ungrounded overlay lines as (unverified).

Overlay notes written before evidence labels existed carry no inline marker,
so auto-recall presents them with the same confidence as grounded knowledge —
live testing showed a stale wrong note being trusted precisely because it
looked clean. This script rewrites every current walk-owned concept whose
overlay contains unmarked blocks, prefixing them with "(unverified) ".

Blocks that are deterministically grounded stay unmarked:
- capture notes (they embed "| working form:" with the proving statement),
- distilled proven-query notes (they start with "Proven query,").

Rewrites go through the same bi-temporal path as every other write: the
current row is superseded (valid_to stamped) and re-inserted, so the
pre-migration text survives as history.

Usage:
    python scripts/memory_migrate_labels.py [--cc http://localhost:19002] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from okf_refresh import execute

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from asterixdb_mcp.tools.memory_write import _INSERT, _UPSERT  # noqa: E402

UNVERIFIED_PREFIX = "(unverified) "
# Markers of blocks that are deterministically grounded and must stay unmarked.
CAPTURE_MARKER = "| working form:"
DISTILL_MARKER = "Proven query,"

CURRENT_ROWS_QUERY = (
    "SELECT VALUE m FROM AgentMemory.Memory m "
    'WHERE m.valid_to IS UNKNOWN AND m.`type` LIKE "AsterixDB %";'
)


def migrate_overlay(overlay: str) -> tuple[str, int]:
    """Prefix unmarked overlay blocks; return (new_overlay, blocks_changed)."""
    blocks = [block.strip() for block in overlay.split("\n\n") if block.strip()]
    changed = 0
    migrated: list[str] = []
    for block in blocks:
        if (
            block.startswith(UNVERIFIED_PREFIX.strip())
            or CAPTURE_MARKER in block
            or block.startswith(DISTILL_MARKER)
        ):
            migrated.append(block)
        else:
            migrated.append(UNVERIFIED_PREFIX + block)
            changed += 1
    if not migrated:
        return "", 0
    return "\n\n".join(migrated) + "\n", changed


def rewrite_row(cc: str, row: dict[str, Any], new_overlay: str) -> None:
    """Supersede the current row and insert the labeled version bi-temporally."""
    now = datetime.now(timezone.utc).isoformat()
    subject = row["subject"]
    core = str(row.get("core") or row.get("text", ""))
    replacement = {
        **row,
        "id": f"{subject}@{now}",
        "valid_from": now,
        "core": core,
        "overlay": new_overlay,
        "text": core.rstrip("\n") + "\n\n" + new_overlay,
        "last_used": now,
    }
    execute(cc, _UPSERT.replace("$row", json.dumps({**row, "valid_to": now})))
    execute(cc, _INSERT.replace("$row", json.dumps(replacement)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cc", default="http://localhost:19002", help="Cluster controller URL")
    parser.add_argument("--dry-run", action="store_true", help="Report, write nothing")
    args = parser.parse_args()

    envelope = execute(args.cc, CURRENT_ROWS_QUERY)
    rows = [r for r in envelope.get("results", []) if isinstance(r, dict)]
    concepts_changed = 0
    blocks_changed = 0
    for row in rows:
        overlay = str(row.get("overlay") or "")
        if not overlay.strip():
            continue
        new_overlay, changed = migrate_overlay(overlay)
        if changed == 0:
            continue
        concepts_changed += 1
        blocks_changed += changed
        if not args.dry_run:
            rewrite_row(args.cc, row, new_overlay)
    mode = "dry-run" if args.dry_run else "written"
    print(
        f"memory_migrate_labels: {len(rows)} walk-owned concepts | "
        f"{concepts_changed} concepts with {blocks_changed} unmarked block(s) | {mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
