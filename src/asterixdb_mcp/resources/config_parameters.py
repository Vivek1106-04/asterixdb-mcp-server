"""asterixdb://config-parameters: the gateway's configurable surface.

A static, CC-free resource that tells an LLM exactly what it is allowed to tune
and within what bounds:

- the ``compilerParameters`` allowlist (keys, kinds, ranges), straight from the
  same table the execute/submit tools validate against, so advertised and
  enforced limits can never drift;
- the egress ceilings and async wait bounds the gateway enforces.

Because it is derived from settings and the allowlist, it needs no call to the
cluster and reflects this gateway instance's actual configuration.
"""

from __future__ import annotations

from typing import Any

from ..compiler_params import describe_allowlist
from ..config import Settings
from ..tools.execute_query import DEFAULT_LIMIT, MAX_LIMIT


def read_config_parameters(settings: Settings) -> dict[str, Any]:
    """Return the gateway's tunable parameters and enforced limits."""
    return {
        "compilerParameters": describe_allowlist(),
        "limits": {
            "maxTimeMs": settings.max_time_ms,
            "maxBytesPerQuery": settings.max_bytes_per_query,
            "maxWaitMs": settings.max_wait_ms,
            "waitPollIntervalMs": settings.wait_poll_interval_ms,
            "resultDefaultLimit": DEFAULT_LIMIT,
            "resultMaxLimit": MAX_LIMIT,
        },
        "concurrency": {
            "syncPermits": settings.sync_permits,
            "asyncPermits": settings.async_permits,
            "maxConcurrentWaits": settings.max_concurrent_waits,
        },
    }
