#!/usr/bin/env bash
# Poll the AsterixDB query service until it answers a trivial statement.
# Usage: wait_for_cluster.sh [CC_BASE_URL] [TIMEOUT_SECONDS]
set -euo pipefail

CC_BASE_URL="${1:-http://localhost:19002}"
TIMEOUT="${2:-120}"
DEADLINE=$(( $(date +%s) + TIMEOUT ))

echo "Waiting for AsterixDB at ${CC_BASE_URL} (timeout ${TIMEOUT}s)..."
while true; do
  if curl -sf -X POST "${CC_BASE_URL}/query/service" \
      --data-urlencode 'statement=SELECT 1 AS ready;' 2>/dev/null | grep -q '"status": "success"'; then
    echo "AsterixDB is ready."
    exit 0
  fi
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "Timed out waiting for AsterixDB." >&2
    exit 1
  fi
  sleep 2
done
