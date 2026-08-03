#!/usr/bin/env bash
#
# Push Cannonball-SE dashboard(s) to Grafana Cloud via the classic import API.
#
# The repo dashboards are portable v1 exports that reference datasources through
# ${DS_LOKI} / ${DS_TEMPO} plus __inputs. This stack's Grafana (v2 schema)
# resolves a dashboard's datasource ref by its DISPLAY NAME, and the
# /api/dashboards/db import copies the v1 `uid` value verbatim into that name
# field without translating uid->name. So before importing we:
#   1. substitute ${DS_*} with the datasource DISPLAY NAME (not the uid), and
#   2. drop the now-unused datasource template variables + empty __inputs.
# (Passing `inputs` in the import body or pinning the var's `current` do NOT
# work — see dashboards/DASHBOARDS.md and project memory for the history.)
#
# Usage:
#   dashboards/push.sh                       # push recent_games_dashboard.json
#   dashboards/push.sh live_game_dashboard.json recent_games_dashboard.json
#   DS_LOKI_NAME=... DS_TEMPO_NAME=... dashboards/push.sh <file>   # other stack
#
# Requires: gcx logged in to the target stack (verify with `gcx config check`)
# and python3.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Datasource DISPLAY NAMES on the target stack (override via env for another stack).
DS_LOKI_NAME="${DS_LOKI_NAME:-grafanacloud-simonprickett-logs}"
DS_TEMPO_NAME="${DS_TEMPO_NAME:-grafanacloud-simonprickett-traces}"

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

  echo "==> pushing $(basename "$f")"
  DS_LOKI_NAME="$DS_LOKI_NAME" DS_TEMPO_NAME="$DS_TEMPO_NAME" python3 - "$f" <<'PY' | gcx api /api/dashboards/db -d @- --jq '{status, uid, version, url}'
import json, os, sys

raw = open(sys.argv[1]).read()
raw = (raw.replace('${DS_LOKI}',  os.environ['DS_LOKI_NAME'])
          .replace('${DS_TEMPO}', os.environ['DS_TEMPO_NAME']))
d = json.loads(raw)

# Drop the vestigial datasource template variables and import inputs.
d.setdefault('templating', {}).setdefault('list', [])
d['templating']['list'] = [v for v in d['templating']['list'] if v.get('type') != 'datasource']
d['__inputs'] = []

print(json.dumps({'dashboard': d, 'overwrite': True, 'folderUid': ''}))
PY
done
