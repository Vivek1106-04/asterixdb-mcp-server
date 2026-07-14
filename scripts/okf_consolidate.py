#!/usr/bin/env python3
"""Sleep-time consolidation and forgetting for the agentic-memory store.

A scheduled pass, run while the assistant is idle, that keeps
``Dashboard.Memory`` healthy. Three operations in one job:

1. **Deduplicate** — the store is open, so nothing structurally prevents two
   *current* rows for one subject. Keep the newest (by ``valid_from``) and
   supersede the rest.
2. **Decay and forget** — usage-based salience: each learned row's ``trust``
   decays by half-life over the days since it was last used, with the
   half-life stretched by how often the row has been accessed. Rows that
   decay below the floor are superseded (never deleted — history stays for
   drift analysis). Walk-owned catalog concepts are exempt: the catalog
   refresh is their freshness mechanism, and pruning them would only make
   the next walk re-insert them.
3. **Re-ground** — delegated to ``okf_refresh --revalidate``, which already
   re-runs stored ``source_query`` fingerprints; run both from the same
   scheduler slot.

Usage:
    python scripts/okf_consolidate.py [--cc URL] [--half-life-days N]
                                      [--trust-floor F] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from okf_refresh import KIND, _walk_owned, apply, bootstrap, execute

DEFAULT_TRUST = 1.0
DEFAULT_HALF_LIFE_DAYS = 30.0
DEFAULT_TRUST_FLOOR = 0.2
# each doubling of access_count stretches the half-life by this factor
ACCESS_HALF_LIFE_BONUS = 0.5

ALL_CURRENT_QUERY = (
    'SELECT VALUE m FROM Dashboard.Memory m WHERE m.kind = "{kind}" AND m.valid_to IS UNKNOWN;'
)


def fetch_all_current(cc: str) -> list[dict[str, Any]]:
    """Every current row, including duplicate subjects the keyed fetch would hide."""
    rows = execute(cc, ALL_CURRENT_QUERY.format(kind=KIND)).get("results", [])
    return [row for row in rows if isinstance(row, dict) and "subject" in row]


def dedup(
    rows: list[dict[str, Any]], now: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the newest current row per subject; supersede the rest.

    Returns (kept_rows, rows_to_supersede).
    """
    newest: dict[str, dict[str, Any]] = {}
    supersede: list[dict[str, Any]] = []
    for row in rows:
        subject = str(row["subject"])
        rival = newest.get(subject)
        if rival is None:
            newest[subject] = row
            continue
        older = row if str(row.get("valid_from", "")) <= str(rival.get("valid_from", "")) else rival
        newest[subject] = rival if older is row else row
        supersede.append({**older, "valid_to": now, "superseded_by": "consolidation-dedup"})
    return list(newest.values()), supersede


def decayed_trust(row: dict[str, Any], now: datetime, half_life_days: float) -> float:
    """Trust after usage-modulated exponential decay since the row was last used."""
    last_used = _parse_time(row.get("last_used")) or _parse_time(row.get("valid_from")) or now
    idle_days = max((now - last_used).total_seconds() / 86400.0, 0.0)
    access_count = row.get("access_count")
    accesses = float(access_count) if isinstance(access_count, (int, float)) else 0.0
    effective_half_life = half_life_days * (1.0 + ACCESS_HALF_LIFE_BONUS * accesses)
    trust = row.get("trust")
    base = float(trust) if isinstance(trust, (int, float)) else DEFAULT_TRUST
    return base * 0.5 ** (idle_days / effective_half_life)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def consolidate(
    rows: list[dict[str, Any]],
    now: datetime,
    half_life_days: float,
    trust_floor: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Pure pass over deduplicated current rows.

    Returns (rows_to_update, rows_to_supersede, exempt_count). Walk-owned
    catalog concepts are exempt from decay; learned rows get their decayed
    trust stamped in place, and rows below the floor are superseded.
    """
    now_iso = now.isoformat()
    updates: list[dict[str, Any]] = []
    supersede: list[dict[str, Any]] = []
    exempt = 0
    for row in rows:
        if _walk_owned(row):
            exempt += 1
            continue
        trust = round(decayed_trust(row, now, half_life_days), 4)
        if trust < trust_floor:
            supersede.append({**row, "valid_to": now_iso, "superseded_by": "consolidation-decay"})
        elif trust != row.get("trust"):
            updates.append({**row, "trust": trust})
    return updates, supersede, exempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default="http://localhost:19002", help="CC base URL")
    parser.add_argument("--half-life-days", type=float, default=DEFAULT_HALF_LIFE_DAYS)
    parser.add_argument("--trust-floor", type=float, default=DEFAULT_TRUST_FLOOR)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    options = parser.parse_args()

    bootstrap(options.cc)
    now = datetime.now(timezone.utc)
    rows = fetch_all_current(options.cc)
    kept, dup_supersede = dedup(rows, now.isoformat())
    updates, decay_supersede, exempt = consolidate(
        kept, now, options.half_life_days, options.trust_floor
    )
    if options.dry_run:
        print(
            json.dumps(
                {
                    "rows": len(rows),
                    "duplicates_superseded": len(dup_supersede),
                    "trust_updates": len(updates),
                    "forgotten": len(decay_supersede),
                    "walk_owned_exempt": exempt,
                },
                indent=2,
            )
        )
        return 0
    apply(options.cc, [], dup_supersede + updates + decay_supersede)
    print(
        f"okf_consolidate: {len(rows)} rows | {len(dup_supersede)} deduped | "
        f"{len(updates)} trust updated | {len(decay_supersede)} forgotten | "
        f"{exempt} walk-owned exempt"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
