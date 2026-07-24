"""Session context for the accuracy suite.

Builds the run context every accuracy test shares: where the cluster is, where
results are written, the commit under test, and the pass threshold. Optionally
loads the fixture datasets once at the start of the run.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass

import pytest

from .fixtures.loader import load_datasets
from .sdk.runner import ACCURACY_MIN_SCORE
from .sdk.storage import DiskResultStorage


@dataclass
class AccuracyRun:
    cc_base_url: str
    storage: DiskResultStorage
    run_id: str
    commit_sha: str
    min_score: float


def _commit_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


@pytest.fixture(scope="session")
def accuracy_run() -> AccuracyRun:
    cc_base_url = os.environ.get("ACCURACY_CC_BASE_URL", "http://localhost:19002")
    if os.environ.get("ACCURACY_LOAD_DATA") == "1":
        load_datasets(cc_base_url)
    return AccuracyRun(
        cc_base_url=cc_base_url,
        storage=DiskResultStorage(),
        run_id=os.environ.get("ACCURACY_RUN_ID", uuid.uuid4().hex),
        commit_sha=_commit_sha(),
        min_score=ACCURACY_MIN_SCORE,
    )
