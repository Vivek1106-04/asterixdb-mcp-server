"""Session context for the live integration suite.

Live tests need a running AsterixDB cluster but no LLM API key, so they are
safe to run on every pull request (including forks). The fixture points at the
cluster and loads the fixture datasets once at the start of the run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from tests.accuracy.fixtures.loader import load_datasets


@dataclass
class LiveCluster:
    cc_base_url: str


@pytest.fixture(scope="session")
def live_cluster() -> LiveCluster:
    cc_base_url = os.environ.get("ACCURACY_CC_BASE_URL", "http://localhost:19002")
    if os.environ.get("ACCURACY_LOAD_DATA") == "1":
        load_datasets(cc_base_url)
    return LiveCluster(cc_base_url=cc_base_url)
