"""Unit tests for overflow artifacts: serialization, persistence, TTL, id safety."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from asterixdb_mcp.artifacts import (
    artifact_link,
    artifact_uri,
    build_download_url,
    overflow_artifact_payload,
    purge_expired,
    read_artifact,
    resolve_artifact_file,
    resolve_artifacts_dir,
    serialize_rows,
    write_overflow_artifact,
)
from asterixdb_mcp.config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "cc_base_url": "http://test-cc:19002",
        "artifacts_dir": str(tmp_path),
    }
    base.update(overrides)
    return Settings(**base)


def test_serialize_rows_json_is_a_pretty_array() -> None:
    rows = [{"i": 1}, {"i": 2}]
    body = serialize_rows(rows, "json")
    assert json.loads(body) == rows
    assert b"\n" in body  # indent=2 produces newlines


def test_serialize_rows_txt_is_one_json_object_per_line() -> None:
    rows = [{"i": 1}, {"i": 2}]
    lines = serialize_rows(rows, "txt").decode().splitlines()
    assert [json.loads(line) for line in lines] == rows


def test_serialize_rows_txt_empty_is_empty_bytes() -> None:
    assert serialize_rows([], "txt") == b""


def test_write_overflow_artifact_persists_full_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    rows = [{"i": n} for n in range(50)]

    ref = write_overflow_artifact(rows, settings=settings, now=1000.0)

    assert ref is not None
    assert ref.total_rows == 50
    saved = Path(ref.local_path)
    assert saved.is_file()
    assert json.loads(saved.read_bytes()) == rows
    payload = ref.to_payload()
    assert payload["artifactId"] == ref.artifact_id
    assert payload["totalRows"] == 50
    assert payload["bytes"] == ref.byte_size


def test_write_overflow_artifact_disabled_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifacts_enabled=False)
    assert write_overflow_artifact([{"i": 1}], settings=settings) is None


def test_write_overflow_artifact_empty_rows_returns_none(tmp_path: Path) -> None:
    assert write_overflow_artifact([], settings=_settings(tmp_path)) is None


def test_write_overflow_artifact_honors_format_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # default format json
    ref = write_overflow_artifact([{"i": 1}], settings=settings, fmt="txt")
    assert ref is not None
    assert ref.fmt == "txt"
    assert Path(ref.local_path).suffix == ".txt"


def test_download_url_derived_from_http_bind(tmp_path: Path) -> None:
    settings = _settings(tmp_path, transport="http", http_host="127.0.0.1", http_port=19200)
    ref = write_overflow_artifact([{"i": 1}], settings=settings)
    assert ref is not None
    assert ref.download_url == f"http://127.0.0.1:19200/artifacts/{ref.artifact_id}"
    assert ref.to_payload()["downloadUrl"] == ref.download_url


def test_download_url_none_under_stdio(tmp_path: Path) -> None:
    settings = _settings(tmp_path, transport="stdio")
    assert build_download_url(settings, "abc") is None


def test_download_url_explicit_base_overrides(tmp_path: Path) -> None:
    settings = _settings(tmp_path, transport="stdio", artifacts_base_url="https://dl.example.com/")
    assert build_download_url(settings, "abc") == "https://dl.example.com/artifacts/abc"


def test_purge_expired_removes_old_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifacts_ttl_s=100.0)
    old = write_overflow_artifact([{"i": 1}], settings=settings, now=time.time() - 1000)
    assert old is not None
    # Backdate the file so it is older than the TTL.
    stale = Path(old.local_path)
    past = time.time() - 1000
    import os

    os.utime(stale, (past, past))

    removed = purge_expired(resolve_artifacts_dir(settings), settings.artifacts_ttl_s)

    assert removed == 1
    assert not stale.exists()


def test_purge_expired_missing_dir_is_zero(tmp_path: Path) -> None:
    assert purge_expired(tmp_path / "nope", 10.0) == 0


def test_resolve_artifact_file_roundtrips(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ref = write_overflow_artifact([{"i": 1}], settings=settings)
    assert ref is not None
    resolved = resolve_artifact_file(settings, ref.artifact_id)
    assert resolved == Path(ref.local_path)


def test_resolve_artifact_file_rejects_bad_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert resolve_artifact_file(settings, "../etc/passwd") is None
    assert resolve_artifact_file(settings, "not-hex") is None
    assert resolve_artifact_file(settings, "a" * 31) is None  # too short


def test_resolve_artifact_file_unknown_id_is_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert resolve_artifact_file(settings, "0" * 32) is None


def test_resolve_artifact_file_expired_is_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifacts_ttl_s=1.0)
    ref = write_overflow_artifact([{"i": 1}], settings=settings)
    assert ref is not None
    past = time.time() - 1000
    import os

    os.utime(Path(ref.local_path), (past, past))
    assert resolve_artifact_file(settings, ref.artifact_id) is None


def test_resolve_artifacts_dir_defaults_to_tempdir() -> None:
    settings = Settings(cc_base_url="http://test-cc:19002")  # artifacts_dir unset
    assert resolve_artifacts_dir(settings).name == "asterixdb-mcp-artifacts"


def test_artifact_link_describes_the_resource_for_a_json_artifact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = overflow_artifact_payload(
        [{"i": n} for n in range(3)], overflow=True, settings=settings, fmt="json"
    )
    assert payload is not None

    link = artifact_link(payload)

    assert link is not None
    artifact_id = payload["artifactId"]
    assert link["uri"] == artifact_uri(artifact_id)
    assert link["name"] == f"{artifact_id}.json"
    assert link["mimeType"] == "application/json"
    assert link["size"] == payload["bytes"]
    assert "3 row(s)" in link["description"]


def test_artifact_link_uses_json_lines_media_type_for_a_txt_artifact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = overflow_artifact_payload([{"i": 1}], overflow=True, settings=settings, fmt="txt")
    assert payload is not None
    link = artifact_link(payload)
    assert link is not None
    assert link["mimeType"] == "text/plain"
    assert link["name"].endswith(".txt")


def test_artifact_link_is_none_when_no_artifact_was_written() -> None:
    # a result that did not overflow attaches no payload, so there is nothing to link
    assert artifact_link(None) is None
    assert artifact_link({}) is None
    assert artifact_link({"note": "malformed, no id"}) is None


def test_read_artifact_returns_the_full_row_set(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    rows = [{"i": n} for n in range(5)]
    payload = overflow_artifact_payload(rows, overflow=True, settings=settings, fmt="json")
    assert payload is not None

    body = read_artifact(settings, payload["artifactId"])

    # the point of the artifact: all of the rows, not the windowed subset
    assert json.loads(body) == rows


def test_read_artifact_rejects_unknown_expired_and_malformed_ids(tmp_path: Path) -> None:
    """The resource door answers exactly what the download route does: 404-equivalent.

    A traversal attempt is the same case as an unknown id, which is what keeps
    the two doors from disagreeing about what is readable.
    """
    settings = _settings(tmp_path, artifacts_ttl_s=1)
    payload = overflow_artifact_payload([{"i": 1}], overflow=True, settings=settings)
    assert payload is not None

    with pytest.raises(FileNotFoundError):
        read_artifact(settings, "0" * 32)
    with pytest.raises(FileNotFoundError):
        read_artifact(settings, "../../etc/passwd")

    expired = Path(payload["localPath"])
    old = time.time() - 3600
    import os

    os.utime(expired, (old, old))
    with pytest.raises(FileNotFoundError):
        read_artifact(settings, payload["artifactId"])
