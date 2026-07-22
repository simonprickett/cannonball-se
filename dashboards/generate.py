#!/usr/bin/env python3
"""
Single source of truth for the Cannonball-SE game dashboards.

The LIVE board (live_game_dashboard.json) is authored/refined by hand (or exported
from Grafana). This script derives the PICKER board (last_game_dashboard.json) from
it so the two never drift: same panels, layout, viz, DOT, screenshots — only the
query scoping and variables differ.

Usage:  python3 dashboards/generate.py
"""
import json, pathlib, sys

HERE = pathlib.Path(__file__).parent
LIVE = HERE / "live_game_dashboard.json"
PICKER = HERE / "recent_games_dashboard.json"

SEL = '{service_name="cannonball-se"}'
SCOPED = f'{SEL} | session_label="$session"'

# Per-panel picker overrides. "expr" = the session-scoped query (live board uses
# "latest"/epoch tricks that don't apply once a specific game is chosen); everything
# else auto-gets the session_label filter. "title"/"description"/"mappings" re-label
# panels whose live wording ("latest", "follows it") is wrong on a pick-a-game board.
SPECIAL = {
    1: {"title": "Selected game",
        "description": "The game chosen in the Game picker above (defaults to the most recent)."},
    16: {"description": "player_initials of the selected game."},
    2: {  # Session state (3-state). Session-scoped: the picked session's completion_code,
          # or null while it's still in progress (no session.end yet).
        "expr": f'max(max_over_time({SEL} | event="game.session.end" | session_label="$session" | unwrap completion_code [$__range]))',
        "description": ("completion_code of the selected session: 1 = COMPLETED, 2 = TIMED OUT, "
                        "null = IN PROGRESS (no end yet)."),
        "mappings": [
            {"type": "value", "options": {
                "1": {"text": "COMPLETED", "color": "green", "index": 0},
                "2": {"text": "TIMED OUT", "color": "red", "index": 1},
            }},
            {"type": "special", "options": {"match": "null", "result": {"text": "IN PROGRESS", "color": "yellow", "index": 2}}},
        ],
    },
    14: {  # Stage reached
        "expr": f'max(max_over_time({SEL} | session_label="$session" | unwrap stage_number [$__range]))',
        "description": "Highest stage_number reached in the selected session.",
    },
    7: {  # Final score
        "expr": f'max(max_over_time({SEL} | event="game.session.end" | session_label="$session" | unwrap final_score [$__range]))',
        "description": "final_score from the selected session's game.session.end (blank while in progress).",
    },
}

def scope_expr(pid, expr):
    if pid in SPECIAL and "expr" in SPECIAL[pid]:
        return SPECIAL[pid]["expr"]
    if SEL not in expr:
        print(f"  WARN: panel {pid} query has no '{SEL}' to scope — left as-is:\n    {expr}", file=sys.stderr)
        return expr
    # Insert the session filter right after the stream selector (valid anywhere in the pipeline).
    return expr.replace(SEL, SCOPED)

def picker_variables():
    return [
        {"name": "DS_LOKI", "label": "Loki data source", "type": "datasource",
         "query": "loki", "current": {}, "hide": 2, "refresh": 1, "regex": ""},
        {"name": "DS_TEMPO", "label": "Tempo data source", "type": "datasource",
         "query": "tempo", "current": {}, "hide": 2, "refresh": 1, "regex": ""},
        {"name": "session", "label": "Game", "type": "query",
         "datasource": {"type": "tempo", "uid": "${DS_TEMPO}"},
         "query": {"label": "session_label", "refId": "TempoDatasourceVariableQueryEditor-VariableQuery", "type": 1},
         "refresh": 2, "sort": 2, "regex": "", "regexApplyTo": "value",
         "hide": 0, "includeAll": False, "multi": False, "allowCustomValue": True,
         "current": {}, "options": [], "skipUrlSync": False},
    ]

def build_picker(live):
    d = json.loads(json.dumps(live))  # deep copy

    for p in d["panels"]:
        pid = p.get("id")
        for t in p.get("targets", []):
            if "expr" in t:
                t["expr"] = scope_expr(pid, t["expr"])
        if pid in SPECIAL and "title" in SPECIAL[pid]:
            p["title"] = SPECIAL[pid]["title"]
        if pid in SPECIAL and "description" in SPECIAL[pid]:
            p["description"] = SPECIAL[pid]["description"]
        if pid in SPECIAL and "mappings" in SPECIAL[pid]:
            p["fieldConfig"]["defaults"]["mappings"] = SPECIAL[pid]["mappings"]

    # Variables + import inputs + plugin requirements
    d["templating"] = {"list": picker_variables()}
    inputs = [i for i in d.get("__inputs", []) if i.get("name") != "DS_TEMPO"]
    if not any(i.get("name") == "DS_TEMPO" for i in inputs):
        inputs.append({"name": "DS_TEMPO", "label": "Tempo",
                       "description": "Tempo data source holding cannonball-se game traces (drives the Session picker)",
                       "type": "datasource", "pluginId": "tempo", "pluginName": "Tempo"})
    d["__inputs"] = inputs
    reqs = d.get("__requires", [])
    if not any(r.get("id") == "tempo" for r in reqs):
        reqs.append({"type": "datasource", "id": "tempo", "name": "Tempo", "version": "1.0.0"})
    d["__requires"] = reqs

    # Meta specific to the picker board
    d["title"] = "Cannonball-SE — Recent Games"
    d["uid"] = "cannonball-recent-games"
    d["tags"] = ["cannonball-se", "loki", "game", "recent"]
    d["time"] = {"from": "now-6h", "to": "now"}
    d["description"] = ("Browse any recent Cannonball-SE game. Pick one with the Game dropdown (Tempo "
                        "label_values on session_label, sorted newest-first, defaults to the most recent); "
                        "every panel is scoped to that game via session_label, so it's exact at ANY time "
                        "range. Unlike the 'Now Playing' board this does NOT auto-follow — you choose the "
                        "game. Generated from live_game_dashboard.json by generate.py — edit the live board, "
                        "then regenerate. Requires grafana-graphviz-panel and dalvany-image-panel.")
    return d

def main():
    if not LIVE.exists():
        sys.exit(f"missing {LIVE}")
    live = json.loads(LIVE.read_text())
    picker = build_picker(live)
    PICKER.write_text(json.dumps(picker, indent=2) + "\n")
    n_expr = sum(1 for pid in SPECIAL if "expr" in SPECIAL[pid])
    n_scoped = sum(1 for p in picker["panels"] for t in p.get("targets", [])
                   if 'session_label="$session"' in t.get("expr", ""))
    print(f"Generated {PICKER.name} from {LIVE.name}: "
          f"{len(picker['panels'])} panels, {n_expr} session-scoped query rewrites, "
          f"{n_scoped} targets filtered by session_label.")

if __name__ == "__main__":
    main()
