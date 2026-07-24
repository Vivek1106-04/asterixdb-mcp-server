#!/usr/bin/env bash
# Boot an AsterixDB sample cluster from a server assembly and wait for it.
#
# Needs no secrets — safe to run on any pull request, including forks. The
# cluster is ephemeral and lives only for the CI job.
#
# Env:
#   ASTERIXDB_SERVER_URL  server assembly (.zip) to boot; override to pin a build
#   CC_BASE_URL           query-service URL to wait on (default localhost:19002)
set -euo pipefail

ASTERIXDB_SERVER_URL="${ASTERIXDB_SERVER_URL:-https://archive.apache.org/dist/asterixdb/asterixdb-0.9.9/apache-asterixdb-0.9.9-server.zip}"
CC_BASE_URL="${CC_BASE_URL:-http://localhost:19002}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Downloading AsterixDB server assembly..."
curl -fsSL "$ASTERIXDB_SERVER_URL" -o asterixdb-server.zip
unzip -q asterixdb-server.zip -d asterixdb-server

START_SCRIPT="$(find asterixdb-server -path '*/opt/local/bin/start-sample-cluster.sh' | head -1)"
if [ -z "$START_SCRIPT" ]; then
  echo "Could not find start-sample-cluster.sh in the assembly." >&2
  exit 1
fi
CLUSTER_ROOT="$(cd "$(dirname "$START_SCRIPT")/../.." && pwd)"

echo "Starting sample cluster from ${CLUSTER_ROOT}..."
(cd "$CLUSTER_ROOT" && bash bin/start-sample-cluster.sh)

bash "$HERE/../accuracy/wait_for_cluster.sh" "$CC_BASE_URL" 180
