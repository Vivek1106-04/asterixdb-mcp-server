"""Render a markdown brief from stored accuracy results.

Reads ``.accuracy/results/<commit>/<runId>.json`` and prints a per-model
summary plus a per-prompt score table. Used by CI to comment on the PR:

    python -m tests.accuracy.sdk.report .accuracy/results/<commit>/<runId>.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean


def render(result_path: Path) -> str:
    data = json.loads(result_path.read_text())
    prompt_results = data.get("promptResults", [])

    by_model: dict[str, list[float]] = {}
    for prompt in prompt_results:
        for response in prompt["modelResponses"]:
            model = f"{response['provider']} / {response['respondingModel']}"
            by_model.setdefault(model, []).append(response["toolCallingAccuracy"])

    lines = [
        "## Accuracy test results",
        "",
        f"Run `{data.get('runId', '?')}` · commit `{data.get('commitSHA', '?')[:10]}` · "
        f"{len(prompt_results)} prompts",
        "",
        "| Model | Prompts | Mean accuracy | Perfect (1.0) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for model, scores in sorted(by_model.items()):
        perfect = sum(1 for s in scores if s == 1.0)
        lines.append(f"| {model} | {len(scores)} | {mean(scores):.2f} | {perfect}/{len(scores)} |")

    lines += [
        "",
        "<details><summary>Per-prompt scores</summary>",
        "",
        "| Prompt | Model | Score |",
        "| --- | --- | ---: |",
    ]
    for prompt in prompt_results:
        label = prompt["prompt"].splitlines()[0][:70]
        for response in prompt["modelResponses"]:
            model = response["respondingModel"]
            lines.append(f"| {label} | {model} | {response['toolCallingAccuracy']:.2f} |")
    lines += ["", "</details>"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render accuracy results as markdown")
    parser.add_argument("result_path", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    brief = render(args.result_path)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(brief)
    print(brief)


if __name__ == "__main__":
    main()
