"""Overflow artifacts: persist a full result set to a downloadable file.

Egress layer 4 (``egress.py``) caps how many rows reach the LLM so a large result
cannot blow the context window. The rows that do not fit are not lost: when a
result overflows, the complete set is written to a file the end user can download
(JSON array, or one JSON row per line as ``.txt``). The tool result carries only a
small reference — id, byte size, row count, download URL / local path, expiry —
never the bulk data, so the context window stays bounded while the full result
stays reachable.

The file is written under ``settings.artifacts_dir`` (a per-process temp directory
when unset). Each write opportunistically purges files older than the configured
TTL, so the directory does not grow without bound and no long-lived cleanup task
is required. Artifact ids are random hex; the download route validates the id
against that shape before touching the filesystem, so a request can never escape
the artifacts directory.

Peer precedent: HTTP-mode MCP servers that expose oversized tool output as a
download URL with a server-side TTL (the "artifacts-as-resources" pattern).
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .config import ARTIFACTS_PATH_PREFIX, Settings

logger = logging.getLogger(__name__)

ArtifactFormat = Literal["json", "txt"]

# An artifact id is exactly the hex of a uuid4 (32 lowercase hex chars). The
# download route matches against this so a caller-supplied id can never contain a
# path separator or traversal sequence.
_ARTIFACT_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_EXTENSIONS: dict[str, str] = {"json": "json", "txt": "txt"}
# Stable per-process temp subdirectory used when artifacts_dir is unset.
_DEFAULT_DIR_NAME = "asterixdb-mcp-artifacts"


@dataclass(frozen=True)
class ArtifactRef:
    """A reference to a persisted overflow artifact — metadata only, never the data."""

    artifact_id: str
    fmt: ArtifactFormat
    total_rows: int
    byte_size: int
    local_path: str
    expires_at: datetime
    download_url: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Render the reference for the egress metadata block."""
        payload: dict[str, Any] = {
            "artifactId": self.artifact_id,
            "format": self.fmt,
            "totalRows": self.total_rows,
            "bytes": self.byte_size,
            "localPath": self.local_path,
            "expiresAt": self.expires_at.isoformat(),
            "note": (
                f"Full result ({self.total_rows} row(s)) saved for download; the response "
                "above shows only the windowed rows."
            ),
        }
        if self.download_url is not None:
            payload["downloadUrl"] = self.download_url
        return payload


def resolve_artifacts_dir(settings: Settings) -> Path:
    """Return the artifacts directory, defaulting to a per-process temp subdir."""
    if settings.artifacts_dir:
        return Path(settings.artifacts_dir)
    return Path(tempfile.gettempdir()) / _DEFAULT_DIR_NAME


def serialize_rows(rows: list[Any], fmt: ArtifactFormat) -> bytes:
    """Serialize a full row set to the artifact's on-disk bytes.

    ``json`` writes a pretty-printed array; ``txt`` writes one compact JSON object
    per line (JSON Lines), which stays greppable and streamable for large results.
    """
    if fmt == "txt":
        lines = [json.dumps(row, default=str, ensure_ascii=False) for row in rows]
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return json.dumps(rows, default=str, ensure_ascii=False, indent=2).encode("utf-8")


def build_download_url(settings: Settings, artifact_id: str) -> str | None:
    """Build the HTTP download URL for an artifact, or None when not servable.

    A link is produced only when a base URL is available: an explicit
    ``artifacts_base_url`` override, otherwise the HTTP bind when serving over the
    http transport. Under stdio there is no server to serve the file, so the
    reference carries only the local path.
    """
    base = settings.artifacts_base_url.strip()
    if not base and settings.transport == "http":
        base = f"http://{settings.http_host}:{settings.http_port}"
    if not base:
        return None
    return f"{base.rstrip('/')}{ARTIFACTS_PATH_PREFIX}/{artifact_id}"


def write_overflow_artifact(
    rows: list[Any],
    *,
    settings: Settings,
    fmt: ArtifactFormat | None = None,
    now: float | None = None,
) -> ArtifactRef | None:
    """Persist a full result set and return a reference, or None when disabled.

    Returns None (no file written) when artifacts are disabled or there are no
    rows to save. Expired files are purged before the new one is written.
    """
    if not settings.artifacts_enabled or not rows:
        return None

    chosen: ArtifactFormat = fmt or settings.artifacts_format
    now = time.time() if now is None else now
    directory = resolve_artifacts_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    purge_expired(directory, settings.artifacts_ttl_s, now=now)

    artifact_id = uuid.uuid4().hex
    path = directory / f"{artifact_id}.{_EXTENSIONS[chosen]}"
    body = serialize_rows(rows, chosen)
    path.write_bytes(body)

    expires_at = datetime.fromtimestamp(now + settings.artifacts_ttl_s, tz=timezone.utc)
    return ArtifactRef(
        artifact_id=artifact_id,
        fmt=chosen,
        total_rows=len(rows),
        byte_size=len(body),
        local_path=str(path),
        expires_at=expires_at,
        download_url=build_download_url(settings, artifact_id),
    )


def overflow_artifact_payload(
    rows: list[Any],
    *,
    overflow: bool,
    settings: Settings,
    fmt: ArtifactFormat | None = None,
) -> dict[str, Any] | None:
    """Persist the full row set and return its egress payload, when applicable.

    The single helper every results-returning tool calls so the
    "overflow -> downloadable artifact" behavior stays identical across them.
    Returns None (nothing attached) when the result did not overflow or when
    artifacts are disabled / empty.
    """
    if not overflow:
        return None
    ref = write_overflow_artifact(rows, settings=settings, fmt=fmt)
    return ref.to_payload() if ref is not None else None


def purge_expired(directory: Path, ttl_s: float, *, now: float | None = None) -> int:
    """Delete artifact files older than the TTL. Returns how many were removed.

    Best-effort: a file that vanishes or cannot be stat'd mid-sweep is skipped, so
    a concurrent request never turns cleanup into an error.
    """
    if not directory.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for child in directory.iterdir():
        try:
            if now - child.stat().st_mtime > ttl_s:
                child.unlink()
                removed += 1
        except OSError:  # pragma: no cover - racing cleanup, nothing actionable
            logger.debug("skipping un-purgeable artifact %s", child, exc_info=True)
    return removed


def resolve_artifact_file(settings: Settings, artifact_id: str) -> Path | None:
    """Resolve a download id to an existing, unexpired file, or None.

    The id must match the strict hex shape (no separators, no traversal). The
    resolved path is confirmed to sit inside the artifacts directory before any
    file is returned, and an expired file is treated as absent.
    """
    if not _ARTIFACT_ID_RE.match(artifact_id):
        return None
    directory = resolve_artifacts_dir(settings).resolve()
    for ext in _EXTENSIONS.values():
        candidate = (directory / f"{artifact_id}.{ext}").resolve()
        if candidate.parent != directory or not candidate.is_file():
            continue
        if time.time() - candidate.stat().st_mtime > settings.artifacts_ttl_s:
            return None
        return candidate
    return None
