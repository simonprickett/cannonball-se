#!/usr/bin/env bash
#
# Push a v2 dashboard manifest (dashboard.grafana.app/v2) to Grafana Cloud via
# `gcx dashboards update`.
#
# The committed manifest deliberately OMITS metadata.resourceVersion (a volatile
# server token that would churn the file on every edit). `gcx dashboards update`
# needs it for optimistic concurrency, so we fetch the current value from the
# server and inject it right before updating. If the dashboard was changed by
# another writer since we fetched, the update fails with a conflict — just re-run.
#
# (This replaces the old v1 classic /api/dashboards/db import + ${DS_*} display-name
# substitution: v2 references datasources by name inline, so no substitution.)
#
# Usage:
#   dashboards/push.sh                              # push recent_games_dashboard.json
#   dashboards/push.sh recent_games_dashboard.json  # explicit file(s)
#
# Requires: gcx logged in to the target stack (verify with `gcx config check`)
# and python3.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

files=("$@")
if [ "${#files[@]}" -eq 0 ]; then
  files=("$HERE/recent_games_dashboard.json")
fi

for f in "${files[@]}"; do
  # Allow bare names relative to the dashboards/ dir.
  [ -f "$f" ] || f="$HERE/$f"
  if [ ! -f "$f" ]; then
    echo "skip: $f not found" >&2
    continue
  fi

  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["metadata"]["name"])' "$f")"
  echo "==> pushing $(basename "$f") -> $name"

  # Current resourceVersion (optimistic-concurrency token) from the server.
  rv="$(gcx dashboards get "$name" -o json 2>&1 | grep -v '"class":"hint"' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["resourceVersion"])')"

  # Inject the RV into a temp copy and update.
  tmp="$(mktemp -t cannonball_v2.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
d.setdefault("metadata", {})["resourceVersion"] = sys.argv[2]
json.dump(d, open(sys.argv[3], "w"))' "$f" "$rv" "$tmp"

  gcx dashboards update "$name" -f "$tmp" 2>&1 | grep -v '"class":"hint"'
  rm -f "$tmp"; trap - EXIT
done
