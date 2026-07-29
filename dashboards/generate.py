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

# Height (grid units) of the "Recent games" picker table inserted at the top of
# the picker board; every inherited panel is pushed down by this much.
TABLE_H = 7

# Ordinal single-hue ramp (light -> dark, stage 1 -> 5) for the Time-per-stage bar.
# Warm orange ramp (soft peach -> deep orange), tuned to read on Grafana's dark
# theme without a stark near-white lightest step. Swap the list for another hue
# (e.g. blue #cde2fb/#9ec5f4/#5598e7/#2a78d6/#184f95) to recolour all 5 stages.
STAGE_COLORS = ["#ffcc80", "#ffb74d", "#ffa726", "#fb8c00", "#ef6c00"]

# Per-panel picker overrides. "expr" = the session-scoped query (live board uses
# "latest"/epoch tricks that don't apply once a specific game is chosen); everything
# else auto-gets the session_label filter. "title"/"description"/"mappings" re-label
# panels whose live wording ("latest", "follows it") is wrong on a pick-a-game board.
SPECIAL = {
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
    7: {  # Score (latest, updates through the game)
        "expr": f'max(last_over_time({SEL} | session_label="$session" | unwrap score [$__range]))',
        "description": "Latest score for the selected game — updates through the game via in-game events (crashes, overtakes, route choices).",
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
    # Loki-only picker: `session` is a plain TEXTBOX holding the chosen game's
    # session_label. It is set by clicking a row in the "Recent games" table
    # (a per-row data link rewrites ?var-session=...), and can also be typed.
    # No Tempo variable — Tempo's tag-values picker was recent-biased/capped on
    # Grafana Cloud (github.com/grafana/tempo/issues/6996); the table reads Loki,
    # which honours the full dashboard time range.
    return [
        {"name": "DS_LOKI", "label": "Loki data source", "type": "datasource",
         "query": "loki", "current": {}, "hide": 2, "refresh": 1, "regex": ""},
        {"name": "session", "label": "Game", "type": "textbox",
         "query": "", "current": {"text": "", "value": ""},
         "options": [{"text": "", "value": "", "selected": True}],
         "hide": 0, "skipUrlSync": False,
         "description": "session_label of the game to view. Click a row in 'Recent games' to set it, or type/paste one."},
    ]


def recent_games_panel():
    # Loki-driven picker table: one row per game (session_label) over the
    # dashboard time range, newest first. Each row's data link sets the
    # `session` textbox var and reloads, scoping every panel below to that game.
    return {
        "id": 30,
        "type": "table",
        "title": "Recent games — click a row to view",
        "description": ("Every game seen in the current time range, newest first "
                        "(session_label sorts lexically by its timestamp prefix). Click a "
                        "row to load that game into the panels below. Widen the time range "
                        "to browse further back — Loki keeps full history, with none of the "
                        "Tempo tag-values cap the old picker suffered."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": TABLE_H},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": f'sum by (session_label) (count_over_time({SEL} | session_label!="" [$__range]))',
        }],
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False},
                    "sortBy": [{"displayName": "Game", "desc": True}]},
        "transformations": [
            {"id": "organize", "options": {
                # Keep only the game (session_label); drop Time and the event count.
                "excludeByName": {"Time": True, "Value": True, "Value #A": True},
                "renameByName": {"session_label": "Game"},
            }},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Game"},
                 "properties": [
                     {"id": "custom.width", "value": 340},
                     {"id": "links", "value": [{
                         "title": "View this game",
                         "url": "/d/cannonball-recent-games/?var-session=${__value.raw}&${__url_time_range}",
                         "targetBlank": False,
                     }]},
                 ]},
            ],
        },
    }

def total_events_panel(y):
    # Picker-only stat: how many game events (log lines) were captured for the
    # selected game. Session-scoped, so it needs the $session textbox var (which
    # only exists on the picker board).
    return {
        "id": 31,
        "type": "stat",
        "title": "Total events",
        "description": "Total game events (log lines) captured for the selected game.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": y, "w": 4, "h": 5},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": f'sum(count_over_time({SCOPED} [$__range]))',
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "short", "mappings": [],
                                     "color": {"mode": "fixed", "fixedColor": "blue"}},
                        "overrides": []},
    }


def stat_panel(pid, x, y, title, description, expr, unit="short", color="blue"):
    # Generic picker-only session-scoped stat tile.
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": 4, "h": 5},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": expr,
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": unit, "mappings": [],
                                     "color": {"mode": "fixed", "fixedColor": color}},
                        "overrides": []},
    }


def stage_time_bar_panel(y):
    # Picker-only: a single HORIZONTAL STACKED bar whose total length = game time,
    # split into one coloured segment per stage (seconds). Per-stage durations come
    # from stage_duration_seconds on game.stage.end (now emitted for the final stage
    # too, at game over). Pivot the long (stage_number, value) result into one row of
    # per-stage fields ("Stage 1", "Stage 2", …) so the bar chart can stack them.
    return {
        "id": 37,
        "type": "barchart",
        "title": "Time per stage",
        "description": "Total game time split by stage — each segment is the wall-clock seconds spent on that stage (stage_duration_seconds on game.stage.end, incl. the final stage to game over).",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 6},
        # One query per stage (1..5, OutRun's max) so each becomes its own value field
        # (Value #A..#E) = a separate stackable series. `by (player_initials)` keeps the
        # initials as a label so labelsToFields can surface it as the x-axis category.
        "targets": [
            {"refId": rid, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (player_initials) (max_over_time({SCOPED} | event="game.stage.end" | stage_number="{n}" | unwrap stage_duration_seconds [$__range]))'}
            for n, rid in [(1, "A"), (2, "B"), (3, "C"), (4, "D"), (5, "E")]
        ],
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": {"player_initials": "Player",
                                 "Value #A": "Stage 1", "Value #B": "Stage 2", "Value #C": "Stage 3",
                                 "Value #D": "Stage 4", "Value #E": "Stage 5"}}},
        ],
        "options": {"orientation": "horizontal", "stacking": "normal", "showValue": "auto",
                    "xField": "Player",
                    "groupWidth": 0.7, "barWidth": 0.97, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "s",
                                     "color": {"mode": "fixed", "fixedColor": STAGE_COLORS[0]},
                                     "custom": {"fillOpacity": 85, "gradientMode": "hue",
                                                "lineWidth": 1, "axisPlacement": "hidden",
                                                "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        # Ordinal single-hue ramp (light->dark) per stage — see STAGE_COLORS.
                        "overrides": [
                            {"matcher": {"id": "byName", "options": f"Stage {i}"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                            for i, c in enumerate(STAGE_COLORS, 1)]},
    }


def fastest_crash_panel(y):
    # Picker-only stat: the km/h at which the car hit its fastest crash in the
    # selected game (max speed_kph over game.crash events). Always red background.
    return {
        "id": 10,  # reuse the "Crashes this game" slot it replaces
        "type": "stat",
        "title": "Fastest crash",
        "description": "Speed (km/h) of the fastest crash in the selected game — max speed_kph over game.crash events.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 4},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": f'max(max_over_time({SCOPED} | event="game.crash" | unwrap speed_kph [$__range]))',
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "center",
                    "textMode": "value", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "velocitykmh", "decimals": 0, "mappings": [],
                                     "color": {"mode": "fixed", "fixedColor": "red"}},
                        "overrides": []},
    }


def music_panel(x, y):
    # Picker-only stat: the game's music_selection (from game.session.start).
    # Numeric for now — track-name value mappings can be added later.
    return {
        "id": 33,
        "type": "stat",
        "title": "Music",
        "description": "music_selection for the selected game (numeric for now; track-name mappings TBD).",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": 4, "h": 5},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": f'max(max_over_time({SCOPED} | unwrap music_selection [$__range]))',
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "none", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "value", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "text"},
                                     "mappings": [
                                         # Stock OutRun tracks (music_selected indexes config.sound.music).
                                         {"type": "value", "options": {
                                             "0": {"text": "Magical Sound Shower", "index": 0},
                                             "1": {"text": "Passing Breeze", "index": 1},
                                             "2": {"text": "Splash Wave", "index": 2},
                                         }},
                                         # Anything else (custom tracks in res/) -> "Custom".
                                         {"type": "range", "options": {
                                             "from": 3, "to": 9999999,
                                             "result": {"text": "Custom", "index": 3}}},
                                     ]},
                        "overrides": []},
    }


def longest_clean_panel(x, y, w):
    # Picker-only stat: longest clean-driving streak (seconds) in the selected
    # game, from longest_clean_seconds (emitted on game.session.end). Background
    # colour by threshold: <10 red, 10-20 orange, 21-30 yellow, 31+ green.
    return {
        "id": 32,
        "type": "stat",
        "title": "Longest clean streak",
        "description": "Longest continuous stretch of clean driving (no crash / off-road) in the selected game, in seconds.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "instant",
            "expr": f'max(max_over_time({SCOPED} | unwrap longest_clean_seconds [$__range]))',
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "center",
                    "textMode": "value", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "s", "decimals": 0, "mappings": [],
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "red", "value": None},
                                         {"color": "orange", "value": 10},
                                         {"color": "yellow", "value": 21},
                                         {"color": "green", "value": 31}]}},
                        "overrides": []},
    }


# Route-map stages: columns left->right (stage 1..5); within a column, top->bottom
# is highest id first (= most left-turns), matching the OutRun stage_lookup_off ids.
STAGES = [
    [("0", "Coconut Beach")],
    [("9", "Gateway"), ("8", "Devil's Canyon")],
    [("18", "Desert"), ("17", "Alps"), ("16", "Cloudy Mountain")],
    [("27", "Wilderness"), ("26", "Old Capital"), ("25", "Wheat Field"), ("24", "Seaside Town")],
    [("36", "Vineyard"), ("35", "Death Valley"), ("34", "Desolation Hill"), ("33", "Autobahn"), ("32", "Lakeside")],
]
ROUTE_NODE_IDS = [nid for col in STAGES for nid, _ in col]
ROUTE_EDGES = [
    ("0", "8"), ("0", "9"),
    ("8", "16"), ("8", "17"), ("9", "17"), ("9", "18"),
    ("16", "24"), ("16", "25"), ("17", "25"), ("17", "26"), ("18", "26"), ("18", "27"),
    ("24", "32"), ("24", "33"), ("25", "33"), ("25", "34"), ("26", "34"), ("26", "35"),
    ("27", "35"), ("27", "36"),
]


def route_map_dot():
    # Same layout as before, but every node defaults to grey (unvisited) and carries
    # just its stage name — visited stages are lit red by the nodeOverride/threshold.
    # Node ids are the stage_id values ("0","9",…) so a node override can match each
    # node against the stage_id column (matchPattern "${id}").
    nodes = [f'  "{nid}" [label="{name}"];' for col in STAGES for nid, name in col]
    ranks = ['  { rank=same; ' + ' '.join(f'"{nid}"' for nid, _ in col) + '; }'
             for col in STAGES if len(col) > 1]
    invis = ['  ' + ' -> '.join(f'"{nid}"' for nid, _ in col) + ' [style=invis];'
             for col in STAGES if len(col) > 1]
    edges = [f'  "{a}" -> "{b}";' for a, b in ROUTE_EDGES]
    return (
        'digraph OutRun {\n'
        '  rankdir=LR;\n'
        '  bgcolor="transparent";\n'
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, '
        'fillcolor="#4a4a4a", fontcolor="white", color="#00000000", penwidth=1.5];\n'
        '  edge [arrowsize=0.7, color="#9e9e9e", penwidth=1.2];\n\n'
        + '\n'.join(nodes) + '\n\n'
        + '\n'.join(ranks) + '\n\n'
        + '\n'.join(invis) + '\n\n'
        + '\n'.join(edges) + '\n'
        '}'
    )


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

    # Make room at the top and insert the Loki "Recent games" picker table.
    for p in d["panels"]:
        p["gridPos"]["y"] += TABLE_H
    d["panels"].insert(0, recent_games_panel())

    # --- Recent-Games-only panel edits (do NOT touch the live board) ---
    # Replace "Crashes this game" (id 10, logs) with a full-width "Fastest crash"
    # stat, and drop "Full event timeline" (id 11) below it (standalone, slow) and
    # "Session result (end event)" (id 8). Then pull the panels that sat below the
    # two removed logs panels up to close the freed space.
    p10, p11 = (next(p for p in d["panels"] if p.get("id") == i) for i in (10, 11))
    top = p10["gridPos"]["y"]
    below = top + p10["gridPos"]["h"] + p11["gridPos"]["h"]  # bottom of the stacked pair
    d["panels"] = [p for p in d["panels"] if p.get("id") not in (8, 11)]
    for i, p in enumerate(d["panels"]):
        if p.get("id") == 10:
            d["panels"][i] = fastest_crash_panel(top)
            gap = below - (top + d["panels"][i]["gridPos"]["h"])
            break
    for p in d["panels"]:
        if p["gridPos"]["y"] >= below:
            p["gridPos"]["y"] -= gap
    # Drop the "Selected game" header (id 1) — adds no value; nothing depends on it.
    d["panels"] = [p for p in d["panels"] if p.get("id") != 1]
    d["panels"].append(total_events_panel(TABLE_H))
    d["panels"].append(music_panel(4, TABLE_H))
    # Fill the rest of the header row with session totals.
    d["panels"].append(stat_panel(
        34, 8, TABLE_H, "Total crashes", "Total crashes in the selected game.",
        f'sum(count_over_time({SCOPED} | event="game.crash" [$__range]))', "short", "red"))
    d["panels"].append(stat_panel(
        35, 12, TABLE_H, "Total overtakes", "Total vehicles overtaken in the selected game.",
        f'sum(count_over_time({SCOPED} | event="game.vehicle_overtake" [$__range]))', "short", "green"))
    d["panels"].append(stat_panel(
        36, 16, TABLE_H, "Game duration",
        "Wall-clock duration of the selected game (session.end epoch minus session.start epoch).",
        f'(max(max_over_time({SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max(max_over_time({SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        "s", "blue"))
    # Add "Longest clean streak" to the stat row (Game state / Player / Stage
    # reached / Off-road), rebalancing their widths to fit a fifth tile.
    row_y = next(p["gridPos"]["y"] for p in d["panels"] if p.get("id") == 2)
    row_widths = {2: (0, 4), 16: (4, 4), 14: (8, 6), 4: (14, 5)}  # id: (x, w)
    for p in d["panels"]:
        if p.get("id") in row_widths:
            p["gridPos"]["x"], p["gridPos"]["w"] = row_widths[p["id"]]
    d["panels"].append(longest_clean_panel(19, row_y, 5))
    # Sort "Overtakes by color" (id 17) bars descending (wrap the already-scoped expr).
    for p in d["panels"]:
        if p.get("id") == 17:
            for t in p.get("targets", []):
                if "expr" in t and not t["expr"].startswith("sort_desc("):
                    t["expr"] = f'sort_desc({t["expr"]})'
            break
    # Route map (id 20): recolour from static rainbow to visited/unvisited. Every
    # stage defaults grey (in the DOT); a stage the player entered produces a
    # stage_id series with a value, which the threshold lights red. Unvisited stages
    # have no series, so they stay grey. (Edges left neutral for now — same
    # threshold/override mechanism can drive edgeOverrides later.)
    for p in d["panels"]:
        if p.get("id") == 20:
            p["options"]["dotDiagram"] = route_map_dot()
            # No transforms: the raw query is already the long shape the plugin's data
            # binding wants — a stage_id id-column + a "Value #A" value-column.
            p.pop("transformations", None)
            p["options"]["namedThresholds"] = [
                {"id": "visited", "name": "Visited",
                 "steps": [{"color": "transparent", "value": 0}, {"color": "#e53935", "value": 1}]}]
            # ONE node override for all nodes: matchPattern "${id}" resolves to each
            # node's id, the plugin finds the row where stage_id == that id, reads its
            # "Value #A" count, and the threshold colours the node red. Unvisited stages
            # have no matching row -> node left grey. (Edges parked until nodes confirmed.)
            p["options"]["nodeOverrides"] = [{
                "id": "visited-nodes",
                "targetNodeIds": ROUTE_NODE_IDS,
                "matchFieldName": "stage_id",
                "matchPattern": "${id}",
                "rules": [{"kind": "fillColor", "colorFieldName": "Value #A", "thresholdId": "visited"}],
            }]
            p["options"]["edgeOverrides"] = []
            break
    # Final frame (id 21) + Course map (id 22): these screenshots load a beat after
    # the rest, so show a "loading" placeholder rather than a "no screenshot" message;
    # and make the panels tall enough for the ~640x512 image (width:100%) not to clip.
    for p in d["panels"]:
        if p.get("id") in (21, 22):
            p["options"]["defaultContent"] = "Loading screenshot..."
            p["gridPos"]["h"] = 16
    # Time-per-stage stacked bar — appended at the bottom (below all other panels)
    # so it never overlaps, regardless of the layout above.
    bottom = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(stage_time_bar_panel(bottom))

    # Variables + import inputs (Loki only — Tempo dropped)
    d["templating"] = {"list": picker_variables()}
    d["__inputs"] = [i for i in d.get("__inputs", []) if i.get("name") != "DS_TEMPO"]
    d["__requires"] = [r for r in d.get("__requires", []) if r.get("id") != "tempo"]

    # Meta specific to the picker board
    d["title"] = "Cannonball-SE — Recent Games"
    d["uid"] = "cannonball-recent-games"
    d["tags"] = ["cannonball-se", "loki", "game", "recent"]
    d["time"] = {"from": "now-7d", "to": "now"}
    d["refresh"] = ""  # historical board — no auto-refresh (avoids slow periodic reloads)
    d["preload"] = True  # load all panels on dashboard load, not lazily on scroll
                         # (so the below-the-fold screenshots aren't sluggish)
    d["description"] = ("Browse any recent Cannonball-SE game. Click a row in the 'Recent games' "
                        "table (Loki, one row per game, newest first) to load it — every panel below "
                        "is scoped to that game via session_label, exact at ANY time range. Widen the "
                        "range to browse further back; Loki keeps full history (no Tempo tag-values "
                        "cap). Unlike 'Now Playing' this does NOT auto-follow — you choose the game. "
                        "Loki-only: no Tempo dependency. Generated from live_game_dashboard.json by "
                        "generate.py — edit the live board, then regenerate. Requires "
                        "grafana-graphviz-panel and marcusolsson-dynamictext-panel.")
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
