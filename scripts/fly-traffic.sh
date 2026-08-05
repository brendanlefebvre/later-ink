#!/usr/bin/env bash
#
# fly-traffic.sh - summarize edge HTTP traffic for a Fly app from Fly's
# hosted Prometheus. No app instrumentation needed; Fly scrapes edge/proxy
# metrics automatically.
#
# Usage:
#   scripts/fly-traffic.sh [APP] [WINDOW] [ORG]
#
# Defaults: APP=later-ink  WINDOW=24h  ORG=personal
# Examples:
#   scripts/fly-traffic.sh                 # later-ink, last 24h
#   scripts/fly-traffic.sh later-ink 1h    # last hour
#   scripts/fly-traffic.sh myapp 7d myorg
#
# Requires: flyctl (authenticated), curl, jq.
#
# Gotcha this script exists to encapsulate: the Fly Prometheus API wants the
# macaroon token passed as `Authorization: FlyV1 <token>`, NOT `Bearer <token>`.
# Passing it as Bearer yields `401 / "something went wrong resolving organization"`.
set -euo pipefail

APP="${1:-later-ink}"
WINDOW="${2:-24h}"
ORG="${3:-personal}"

for bin in flyctl curl jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: '$bin' not found on PATH" >&2; exit 1; }
done

# Full token string, including the leading "FlyV1 " prefix, used verbatim as
# the Authorization header value (NOT Bearer).
AUTH="$(flyctl tokens create readonly "$ORG" 2>/dev/null | grep -i 'fm2' | tr -d '\r')"
if [ -z "$AUTH" ]; then
  echo "error: could not mint a readonly token for org '$ORG'" >&2
  exit 1
fi

BASE="https://api.fly.io/prometheus/${ORG}/api/v1/query"
q() { curl -s -G "$BASE" -H "Authorization: ${AUTH}" --data-urlencode "query=$1"; }

echo "== ${APP} edge traffic, last ${WINDOW} (org: ${ORG}) =="
echo

total="$(q "sum(increase(fly_edge_http_responses_count{app=\"${APP}\"}[${WINDOW}]))" \
  | jq -r '.data.result[0].value[1] // "0" | tonumber | floor')"
echo "total responses : ${total}"

bytes="$(q "sum(increase(fly_edge_data_out{app=\"${APP}\"}[${WINDOW}]))" \
  | jq -r '.data.result[0].value[1] // "0" | tonumber')"
mib="$(awk "BEGIN{printf \"%.1f\", ${bytes}/1048576}")"
echo "data out        : ${mib} MiB"

echo
echo "by status:"
q "sum by (status) (increase(fly_edge_http_responses_count{app=\"${APP}\"}[${WINDOW}]))" \
  | jq -r '.data.result | sort_by(.metric.status)[]
           | "  \(.metric.status)  \(.value[1] | tonumber | floor)"'
