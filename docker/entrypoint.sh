#!/usr/bin/env bash
# Start sshd (the sample cluster talks to its node over ssh), boot the sample
# cluster, then hold the container open on the cluster logs.
set -euo pipefail

service ssh start

START_SCRIPT="$(find /opt/asterixdb -path '*/opt/local/bin/start-sample-cluster.sh' | head -1)"
CLUSTER_ROOT="$(cd "$(dirname "$START_SCRIPT")/../.." && pwd)"

cd "$CLUSTER_ROOT"
bash bin/start-sample-cluster.sh

echo "AsterixDB sample cluster started; query service on :19002."
# Keep the container alive and stream the cluster logs.
exec tail -f "$CLUSTER_ROOT"/logs/*.log 2>/dev/null || exec sleep infinity
