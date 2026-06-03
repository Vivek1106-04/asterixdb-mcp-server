"""clientContextID namespace transform.

Every forwarded query carries a namespaced client_context_id shaped as
{agentSessionId}::{userTag}::{uuid}. The same shape is used for the sync and
async query paths so the audit trail stays consistent end to end.

"::" is the segment delimiter, so it can't appear inside a segment. Segments are
sanitized rather than rejected, keeping the gateway forgiving of whatever userTag
the LLM supplies.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

SEGMENT_DELIMITER = "::"
# Max length of the user-supplied tag segment, mirroring the inputSchema cap.
MAX_USER_TAG_LENGTH = 64
# A short fallback used when a segment sanitizes down to nothing.
EMPTY_SEGMENT_PLACEHOLDER = "_"

# Disallow the delimiter and whitespace inside any segment.
_UNSAFE_SEGMENT_CHARS = re.compile(r"[:\s]+")


def sanitize_segment(value: str, *, max_length: int | None = None) -> str:
    """Collapse delimiter and whitespace runs to a single "-" and trim to length.

    Returns EMPTY_SEGMENT_PLACEHOLDER when the input is empty or sanitizes away,
    so the context id always has three non-empty segments.
    """
    cleaned = _UNSAFE_SEGMENT_CHARS.sub("-", value.strip()).strip("-")
    if max_length is not None:
        cleaned = cleaned[:max_length].strip("-")
    return cleaned or EMPTY_SEGMENT_PLACEHOLDER


def make_client_context_id(
    agent_session_id: str,
    user_tag: str | None = None,
    *,
    uuid_factory: Callable[[], str] | None = None,
) -> str:
    """Build a namespaced ``{agentSessionId}::{userTag}::{uuid}`` client context id.

    Args:
        agent_session_id: Stable per-gateway session identifier.
        user_tag: Optional human-readable label. Defaults to the placeholder when
            absent so the three-segment shape is preserved.
        uuid_factory: Injectable UUID source for deterministic tests. Defaults to
            a fresh uuid.uuid4.
    """
    new_uuid = uuid_factory or (lambda: str(uuid.uuid4()))
    session_segment = sanitize_segment(agent_session_id)
    tag_segment = sanitize_segment(user_tag or "", max_length=MAX_USER_TAG_LENGTH)
    uuid_segment = sanitize_segment(new_uuid())
    return SEGMENT_DELIMITER.join((session_segment, tag_segment, uuid_segment))


def parse_client_context_id(client_context_id: str) -> tuple[str, str, str]:
    """Split a context id back into ``(session, tag, uuid)``.

    Raises:
        ValueError: if the id does not have exactly three ``::``-delimited segments.
    """
    parts = client_context_id.split(SEGMENT_DELIMITER)
    if len(parts) != 3:
        raise ValueError(
            f"clientContextID must have exactly 3 '::'-delimited segments, got {len(parts)}: "
            f"{client_context_id!r}"
        )
    session, tag, tail = parts
    return session, tag, tail
