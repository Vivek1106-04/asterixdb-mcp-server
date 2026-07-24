"""Disk-based accuracy result storage.

Each run's results are appended to ``.accuracy/results/<commit>/<runId>.json``,
grouped by prompt with one entry per model. A file lock keeps concurrent model
shards from clobbering each other's writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .types import AgentResult, ExpectedToolCall

RESULTS_DIR = Path(os.environ.get("ACCURACY_RESULTS_DIR", ".accuracy/results"))


class DiskResultStorage:
    def __init__(self, results_dir: Path = RESULTS_DIR) -> None:
        self._dir = results_dir

    def _path(self, commit_sha: str, run_id: str) -> Path:
        return self._dir / commit_sha / f"{run_id}.json"

    def save_model_response(
        self,
        *,
        commit_sha: str,
        run_id: str,
        prompt: str,
        expected_tool_calls: list[ExpectedToolCall],
        provider: str,
        requested_model: str,
        accuracy: float,
        agent_result: AgentResult,
    ) -> None:
        path = self._path(commit_sha, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        model_response = {
            "provider": provider,
            "requestedModel": requested_model,
            "respondingModel": agent_result.responding_model,
            "toolCallingAccuracy": accuracy,
            "llmResponseTimeMs": agent_result.response_time_ms,
            "tokensUsed": {
                "promptTokens": agent_result.prompt_tokens,
                "completionTokens": agent_result.completion_tokens,
                "totalTokens": agent_result.total_tokens,
            },
            "llmToolCalls": [
                {"toolName": c.tool_name, "parameters": c.parameters}
                for c in agent_result.tool_calls
            ],
            "text": agent_result.text,
        }

        from filelock import FileLock

        with FileLock(str(path) + ".lock"):
            data = self._read(path) or {
                "runId": run_id,
                "commitSHA": commit_sha,
                "promptResults": [],
            }
            existing = next((p for p in data["promptResults"] if p["prompt"] == prompt), None)
            if existing is None:
                data["promptResults"].append(
                    {
                        "prompt": prompt,
                        "expectedToolCalls": [
                            {
                                "toolName": e.tool_name,
                                "parameters": _jsonable(e.parameters),
                                "optional": e.optional,
                            }
                            for e in expected_tool_calls
                        ],
                        "modelResponses": [model_response],
                    }
                )
            else:
                existing["modelResponses"].append(model_response)
            path.write_text(json.dumps(data, indent=2, default=str))

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.exists():
            return None
        return json.loads(path.read_text())


def _jsonable(params: dict) -> dict:
    """Render matcher objects in expected params as their repr for the record."""
    return {k: (v if _is_plain(v) else repr(v)) for k, v in params.items()}


def _is_plain(value: object) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))
