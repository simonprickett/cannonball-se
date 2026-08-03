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
# Data-driven layout: the v2 spec.layout (RowsLayout) captured from the Grafana UI.
# Arrange in the UI, then `gcx dashboards get cannonball-recent-games` and save its
# spec.layout here (see dashboards/README/push.sh). Panel CONTENT stays in this
# generator; only positions + rows/tabs live in layout.json.
LAYOUT_FILE = HERE / "layout.json"

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
# Deep-purple ordinal ramp (dark, light->dark) for the Score-progression stacked bar.
SCORE_COLORS = ["#7e57c2", "#673ab7", "#5e35b1", "#4527a0", "#311b92"]

# Per-panel picker overrides. "expr" = the session-scoped query (live board uses
# "latest"/epoch tricks that don't apply once a specific game is chosen); everything
# else auto-gets the session_label filter. "title"/"description"/"mappings" re-label
# panels whose live wording ("latest", "follows it") is wrong on a pick-a-game board.
SPECIAL = {
    16: {"description": "player_initials of the selected game."},
    # Title tweaks (+ emoji) on live-board-inherited panels — Recent-Games-only,
    # so the live/Now Playing board keeps its own plain titles.
    20: {"title": "🗺️ Route taken (map)"},
    21: {"title": "📷 Final frame (game over)"},
    22: {"title": "🗺️ Course map"},
    23: {"title": "📷 Key moments"},
    4: {"description": "Number of times the car went off-road (game.off_road events) in the selected game.",
        "thresholds": [{"color": "green", "value": None},   # 0-5
                       {"color": "orange", "value": 6},     # 6-12
                       {"color": "red", "value": 13}]},     # 13+
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
        # Not a metric we compare on / the user controls -> plain blue gradient
        # background rather than value-graded thresholds.
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "short", "mappings": [],
                                     "color": {"mode": "fixed", "fixedColor": "blue"}},
                        "overrides": []},
    }


def stat_panel(pid, x, y, title, description, expr, unit="short", color="blue", thresholds=None):
    # Generic picker-only session-scoped stat tile. Pass `thresholds` (a list of
    # {color,value} steps) to colour a GRADIENT BACKGROUND by value instead of the
    # fixed-colour value text.
    defaults = {"unit": unit, "mappings": []}
    if thresholds:
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    else:
        defaults["color"] = {"mode": "fixed", "fixedColor": color}
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
                    "colorMode": "background" if thresholds else "value",
                    "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


def gearbox_mode_panel(pid, x, y, w):
    # Picker-only stat: the transmission mode of the selected game, read from the
    # gearbox_mode attribute on game.session.start ("automatic"/"manual"). A hidden
    # Loki query flattens it to the log line; a SQL expr maps it to a numeric code so
    # value mappings can print "Auto"/"Manual". Fixed synthwave-yellow gradient bg.
    return {
        "id": pid, "type": "stat", "title": "Gearbox",
        "description": "Transmission mode of the selected game (gearbox_mode on game.session.start): Auto or Manual.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 5},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "range", "maxLines": 5,
             "expr": SCOPED + ' | event="game.session.start" | line_format "{{.gearbox_mode}}"'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT CASE "
                            "WHEN (SELECT Line FROM A ORDER BY `Time` DESC LIMIT 1) = 'automatic' THEN 1 "
                            "WHEN (SELECT Line FROM A ORDER BY `Time` DESC LIMIT 1) = 'manual' THEN 2 "
                            "ELSE 0 END AS gearbox")},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {
            "unit": "short",
            "color": {"mode": "fixed", "fixedColor": "#ffd319"},  # synthwave yellow
            "mappings": [{"type": "value", "options": {
                "0": {"text": "—", "index": 0},
                "1": {"text": "Auto", "index": 1},
                "2": {"text": "Manual", "index": 2}}}]},
            "overrides": []},
    }


def shifts_per_stage_panel(pid, x, y, w):
    # Picker-only: up/down gear shifts per stage as a stacked bar (one bar per stage,
    # 1-5). Hidden Loki metric splits by (stage_number, direction); a SQL expr LEFT
    # JOINs the fixed 5 stages and pivots direction into Up/Down columns (0-filled),
    # so all 5 stages always show even with no shifts. See gear_shift telemetry note:
    # up - down ≈ crashes (a crash resets the gear without a logged down-shift).
    return {
        "id": pid, "type": "barchart", "title": "Gear shifts per stage",
        "description": ("Up/down gear shifts in each stage (game.gear_shift), stages 1-5. In "
                        "automatic mode a shift is a crossing of the ~160 km/h auto threshold, so "
                        "this tracks braking/slowdowns per stage; in manual mode it is driver input."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number, direction) (count_over_time({SCOPED} | event="game.gear_shift" [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, "
                            "COALESCE(SUM(CASE WHEN a.direction = 'up' THEN a.cnt END), 0) AS `Up shifts`, "
                            "COALESCE(SUM(CASE WHEN a.direction = 'down' THEN a.cnt END), 0) AS `Down shifts` "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, direction, `__value__` AS cnt FROM A) a ON a.stage_number = t.n "
                            "GROUP BY t.n, t.label ORDER BY t.n")},
        ],
        "options": {"orientation": "vertical", "xField": "stage", "stacking": "normal",
                    "showValue": "never", "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "custom": {"fillOpacity": 85, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Shifts", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": "Up shifts"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#26c6da"}}]},
                            {"matcher": {"id": "byName", "options": "Down shifts"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff7043"}}]},
                        ]},
    }


def downshift_speed_hist_panel(pid, x, y, w):
    # Picker-only: down-shift speeds bucketed into 10 km/h bins (bar chart, same trick
    # as Overtake speeds). Up-shifts are all pinned to the ~160 threshold so only
    # down-shifts carry a distribution — lower speed = harder braking (auto mode).
    return {
        "id": pid, "type": "barchart", "title": "Down-shift speeds",
        "description": ("Down-shifts bucketed by speed (10 km/h bins) in the selected game. In "
                        "automatic mode a down-shift fires as speed drops back below the ~160 km/h "
                        "auto threshold, so lower speeds mean harder braking."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "range", "maxLines": 1000,
             "expr": SCOPED + ' | event="game.gear_shift" | direction="down" | line_format "{{.speed_kph}}"'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT CAST(FLOOR(CAST(Line AS DOUBLE)/10)*10 AS CHAR) AS speed_kmh, "
                            "COUNT(*) AS downshifts FROM A WHERE CAST(Line AS DOUBLE) > 0 "
                            "GROUP BY speed_kmh ORDER BY CAST(speed_kmh AS DOUBLE)")},
        ],
        "options": {"orientation": "vertical", "xField": "speed_kmh", "showValue": "never",
                    "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": False, "stacking": "none",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#ff7043"},
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Down-shifts", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": []},
    }


def upshift_speed_hist_panel(pid, x, y, w):
    # Picker-only: up-shift speeds bucketed into 10 km/h bins (bar chart, mirror of
    # Down-shift speeds). Meaningful in MANUAL mode where the driver chooses when to
    # shift up; in AUTO it collapses to a single bar at the ~160 threshold. Cyan to
    # match the "Up shifts" series in the per-stage panel (up=cyan / down=orange).
    return {
        "id": pid, "type": "barchart", "title": "Up-shift speeds",
        "description": ("Up-shifts bucketed by speed (10 km/h bins) in the selected game. Meaningful "
                        "in manual mode (driver picks the shift point); in automatic every up-shift is "
                        "pinned to the ~160 km/h auto threshold, so it collapses to one bar."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "range", "maxLines": 1000,
             "expr": SCOPED + ' | event="game.gear_shift" | direction="up" | line_format "{{.speed_kph}}"'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT CAST(FLOOR(CAST(Line AS DOUBLE)/10)*10 AS CHAR) AS speed_kmh, "
                            "COUNT(*) AS upshifts FROM A WHERE CAST(Line AS DOUBLE) > 0 "
                            "GROUP BY speed_kmh ORDER BY CAST(speed_kmh AS DOUBLE)")},
        ],
        "options": {"orientation": "vertical", "xField": "speed_kmh", "showValue": "never",
                    "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": False, "stacking": "none",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#26c6da"},
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Up-shifts", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": []},
    }


def incidents_by_stage_panel(pid, x, y, w):
    # Picker-only: crashes + off-road events per stage as a stacked bar (stages 1-5).
    # Two hidden Loki metric queries (crash / off_road counts by stage_number); a SQL
    # expr LEFT JOINs both against the fixed 5 stages and 0-fills, so every stage shows.
    # Synthwave warm pair: crashes magenta, off-road deep orange (matches the shift bar).
    return {
        "id": pid, "type": "barchart", "title": "Incidents by stage",
        "description": "Crashes and off-road events in each stage (game.crash + game.off_road), stages 1-5, stacked.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number) (count_over_time({SCOPED} | event="game.crash" [$__range]))'},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number) (count_over_time({SCOPED} | event="game.off_road" [$__range]))'},
            {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, "
                            "COALESCE(c.cnt, 0) AS `Crashes`, "
                            "COALESCE(o.cnt, 0) AS `Off-road` "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, `__value__` AS cnt FROM A) c ON c.stage_number = t.n "
                            "LEFT JOIN (SELECT stage_number, `__value__` AS cnt FROM B) o ON o.stage_number = t.n "
                            "ORDER BY t.n")},
        ],
        "options": {"orientation": "horizontal", "xField": "stage", "stacking": "normal",
                    "showValue": "never", "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Incidents", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": "Crashes"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff2975"}}]},
                            {"matcher": {"id": "byName", "options": "Off-road"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff7043"}}]},
                        ]},
    }


def crashes_by_stage_panel(pid, x, y, w):
    # Picker-only: crashes per stage broken down by TYPE, stacked (stages 1-5). One
    # hidden Loki metric split by (stage_number, crash_type); a SQL expr pivots type
    # into Bump/Spin/Flip columns, LEFT JOINed to the fixed 5 stages and 0-filled.
    # Column order Bump -> Spin -> Flip sets the stack order (bump at the base). Red
    # severity ramp: bump least-red, spin mid, flip the most severe (darkest red).
    return {
        "id": pid, "type": "barchart", "title": "Crashes by stage",
        "description": "Crashes in each stage by type (game.crash crash_type: bump/spin/flip), stages 1-5, stacked bump→spin→flip.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number, crash_type) (count_over_time({SCOPED} | event="game.crash" [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, "
                            "COALESCE(SUM(CASE WHEN a.crash_type = 'bump' THEN a.cnt END), 0) AS `Bump`, "
                            "COALESCE(SUM(CASE WHEN a.crash_type = 'spin' THEN a.cnt END), 0) AS `Spin`, "
                            "COALESCE(SUM(CASE WHEN a.crash_type = 'flip' THEN a.cnt END), 0) AS `Flip` "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, crash_type, `__value__` AS cnt FROM A) a ON a.stage_number = t.n "
                            "GROUP BY t.n, t.label ORDER BY t.n")},
        ],
        "options": {"orientation": "horizontal", "xField": "stage", "stacking": "normal",
                    "showValue": "never", "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Crashes", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": "Bump"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ffd54f"}}]},  # yellow, least severe
                            {"matcher": {"id": "byName", "options": "Spin"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff9800"}}]},  # orange, mid
                            {"matcher": {"id": "byName", "options": "Flip"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#c62828"}}]},  # deep red, most severe
                        ]},
    }


def events_by_stage_panel(pid, x, y, w):
    # Picker-only: total telemetry events of ANY type recorded in each stage (every
    # game.* log line carrying a stage_number), stages 1-5. Same LEFT-JOIN/0-fill
    # pivot as the other per-stage bars; VERTICAL (stage on x, count on y) like the
    # speed histograms.
    return {
        "id": pid, "type": "barchart", "title": "Events by stage",
        "description": "Total telemetry events of any type recorded in each stage (all game.* events with a stage_number), stages 1-5.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number) (count_over_time({SCOPED} | stage_number != "" [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, COALESCE(a.cnt, 0) AS events "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, `__value__` AS cnt FROM A) a ON a.stage_number = t.n "
                            "ORDER BY t.n")},
        ],
        "options": {"orientation": "vertical", "xField": "stage", "showValue": "never",
                    "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": False, "stacking": "none",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#26c6da"},
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Events", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": []},
    }


def overtakes_by_stage_panel(pid, x, y, w):
    # Picker-only: overtakes per stage as a single-series bar (stages 1-5). Hidden Loki
    # metric counts by stage_number; SQL LEFT JOINs the fixed 5 stages and 0-fills.
    # Synthwave purple to match the Overtake-speeds panel.
    return {
        "id": pid, "type": "barchart", "title": "Overtakes by stage",
        "description": "Vehicles overtaken in each stage (game.vehicle_overtake), stages 1-5.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (stage_number) (count_over_time({SCOPED} | event="game.vehicle_overtake" [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, COALESCE(a.cnt, 0) AS overtakes "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, `__value__` AS cnt FROM A) a ON a.stage_number = t.n "
                            "ORDER BY t.n")},
        ],
        "options": {"orientation": "horizontal", "xField": "stage", "showValue": "never",
                    "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": False, "stacking": "none",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#7e57c2"},
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Overtakes", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
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
        "title": "⏱️ Time per stage",
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


def speed_gauge_panel(pid, x, y, w, h, title, description, expr):
    # Session-scoped speed gauge matching the Top speed / Avg speed gauges (km/h,
    # 0-300, red->orange->yellow->green thresholds, circle style).
    return {
        "id": pid, "type": "gauge", "title": title, "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                     "editorMode": "code", "queryType": "instant", "expr": expr}],
        "options": {"barShape": "flat", "barWidthFactor": 0.5,
                    "effects": {"barGlow": False, "centerGlow": False, "gradient": False},
                    "endpointMarker": "point", "minVizHeight": 75, "minVizWidth": 75,
                    "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "segmentCount": 1, "segmentSpacing": 0.3, "shape": "gauge",
                    "showThresholdLabels": False, "showThresholdMarkers": True, "sizing": "auto",
                    "sparkline": False, "style": "circle", "textMode": "auto"},
        "fieldConfig": {"defaults": {"unit": "velocitykmh", "min": 0, "max": 300, "decimals": 0,
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "#e57373", "value": None},
                                         {"color": "#ffb74d", "value": 100},
                                         {"color": "#fff176", "value": 180},
                                         {"color": "#81c784", "value": 250}]}},
                        "overrides": []},
    }


def overtake_speed_hist_panel(pid, x, y):
    # "Histogram" of overtake speeds, built as a BAR CHART because the Histogram viz
    # has no bar-gap control. A hidden Loki query emits speed_kph as the line; a SQL
    # expr buckets it into 10 km/h bins with counts; the bar chart draws gapped purple
    # bars (barWidth 0.9) with an opacity gradient for the synthwave shading.
    return {
        "id": pid, "type": "barchart", "title": "Overtake speeds",
        "description": "Overtakes bucketed by speed (10 km/h bins) in the selected game.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "range", "maxLines": 1000,
             "expr": SCOPED + ' | event="game.vehicle_overtake" | line_format "{{.speed_kph}}"'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT CAST(FLOOR(CAST(Line AS DOUBLE)/10)*10 AS CHAR) AS speed_kmh, "
                            "COUNT(*) AS overtakes FROM A GROUP BY speed_kmh ORDER BY CAST(speed_kmh AS DOUBLE)")},
        ],
        "options": {"orientation": "vertical", "xField": "speed_kmh", "showValue": "never",
                    "barWidth": 0.95, "groupWidth": 0.7, "fullHighlight": False, "stacking": "none",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#7e57c2"},
                                     "custom": {"fillOpacity": 90, "gradientMode": "opacity",
                                                "lineWidth": 1, "axisPlacement": "auto",
                                                "axisLabel": "Overtakes", "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": []},
    }


def checkpoint_buffer_panel(pid, x, y):
    # Seconds left on the clock at each checkpoint (time_remaining_seconds on
    # game.stage.end). Always show a bar for all 5 stages in order (a SQL expr
    # LEFT JOINs stages 1-5 against the data and 0-fills the missing ones — the
    # bargauge won't render a label when only one series is returned). Low = red,
    # high = green. A is hidden; it just feeds the expression.
    return {
        "id": pid, "type": "bargauge", "title": "⏱️ Checkpoint time buffer",
        "description": "Seconds left on the clock at each checkpoint (game.stage.end time_remaining_seconds), stages 1-5. Uncompleted stages show 0.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (stage_number) (max_over_time({SCOPED} | event="game.stage.end" | unwrap time_remaining_seconds [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS stage, COALESCE(a.secs, 0) AS seconds "
                            "FROM (SELECT '1' AS n, 'Stage 1' AS label UNION ALL SELECT '2','Stage 2' "
                            "UNION ALL SELECT '3','Stage 3' UNION ALL SELECT '4','Stage 4' UNION ALL SELECT '5','Stage 5') t "
                            "LEFT JOIN (SELECT stage_number, `__value__` AS secs FROM A) a ON a.stage_number = t.n "
                            "ORDER BY t.n")}],
        "options": {"displayMode": "lcd", "orientation": "horizontal", "valueMode": "color",
                    "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}},
        "fieldConfig": {"defaults": {"unit": "s", "decimals": 0,
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "red", "value": None},    # 0-3
                                         {"color": "orange", "value": 4},    # 4-7
                                         {"color": "green", "value": 8}]},   # 8+
                                     "mappings": []},
                        "overrides": []},
    }


def score_progression_panel(y):
    # Same stacked-bar setup as Time per stage, but each segment is the POINTS scored
    # in that stage (score_end - score_start), stacked up to the final score. Purple ramp.
    def per_stage(n):
        return (f'(max by (player_initials) (max_over_time({SCOPED} | event="game.stage.end" | stage_number="{n}" | unwrap score_end [$__range])) '
                f'- max by (player_initials) (max_over_time({SCOPED} | event="game.stage.start" | stage_number="{n}" | unwrap score_start [$__range])))')
    return {
        "id": 45, "type": "barchart", "title": "Score progression",
        "description": "Points scored in each stage (score_end - score_start on the stage events), stacked to the final score.",
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 6},
        "targets": [
            {"refId": rid, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": per_stage(n)}
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
                    "xField": "Player", "groupWidth": 0.7, "barWidth": 0.97, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "short",
                                     "color": {"mode": "fixed", "fixedColor": SCORE_COLORS[0]},
                                     "custom": {"fillOpacity": 85, "gradientMode": "hue",
                                                "lineWidth": 1, "axisPlacement": "hidden",
                                                "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": f"Stage {i}"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                            for i, c in enumerate(SCORE_COLORS, 1)]},
    }


def _ordinal(n):
    # 1->1st, 2->2nd, 3->3rd, 4->4th … with the 11th/12th/13th exceptions.
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_MEDAL = {1: "#b8860b", 2: "#808891", 3: "#a05a2c"}  # deep gold / silver / bronze (readable w/ white text)


def rank_panel(pid, x, y, title, all_games_expr, selected_expr, description):
    # Picker-only stat: rank the selected game by a metric among ALL games in the
    # dashboard time range (higher = 1st). A = per-game metric for all games (hidden),
    # B = the selected game's metric (hidden, session-scoped), C = SQL that counts how
    # many games rank higher (+1). $session interpolates in Loki queries but NOT in the
    # SQL expr, so the selected value comes via B. A/B are hidden so their
    # numeric-full-long frames don't reach the stat display ("No data" otherwise).
    return {
        "id": pid, "type": "stat", "title": title, "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": 4, "h": 5},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": all_games_expr},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": selected_expr},
            {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT COUNT(*) + 1 AS game_rank FROM A "
                            "WHERE `__value__` > (SELECT MAX(`__value__`) FROM B)")},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/^game_rank$/", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "short",
                                     # 1..100 -> ordinals; podium (1/2/3) gold/silver/bronze background,
                                     # everything else the same dark-blue as the Player tile.
                                     "mappings": [{"type": "value", "options": {
                                         str(n): {"text": _ordinal(n), "index": n - 1,
                                                  **({"color": _MEDAL[n]} if n in _MEDAL else {})}
                                         for n in range(1, 101)}}],
                                     "color": {"mode": "fixed", "fixedColor": "dark-blue"}},
                        "overrides": []},
    }


def score_rank_panel(x, y):
    return rank_panel(
        38, x, y, "Overall Rank",
        f'max by (session_label) (max_over_time({SEL} | event="game.session.end" | unwrap final_score [$__range]))',
        f'max(max_over_time({SCOPED} | event="game.session.end" | unwrap final_score [$__range]))',
        "This game's rank by final score among all games in the dashboard time range (1 = highest score).")


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
        "title": "🎵 Music",
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
        # Not a comparison metric -> plain blue gradient background (same as Total events).
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "value", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "blue"},
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
        if pid in SPECIAL and "thresholds" in SPECIAL[pid]:
            p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
            p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": SPECIAL[pid]["thresholds"]}
            p["options"]["colorMode"] = "background"

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
        f'sum(count_over_time({SCOPED} | event="game.crash" [$__range]))', "short",
        thresholds=[{"color": "green", "value": None},   # 0-3
                    {"color": "orange", "value": 4},     # 4-7
                    {"color": "red", "value": 8}]))      # 8+
    d["panels"].append(stat_panel(
        35, 12, TABLE_H, "Total overtakes", "Total vehicles overtaken in the selected game.",
        f'sum(count_over_time({SCOPED} | event="game.vehicle_overtake" [$__range]))', "short",
        thresholds=[{"color": "red", "value": None},    # 0-9
                    {"color": "orange", "value": 10},   # 10-19
                    {"color": "green", "value": 20}]))  # 20+
    d["panels"].append(stat_panel(
        36, 16, TABLE_H, "Game duration",
        "Wall-clock duration of the selected game (session.end epoch minus session.start epoch).",
        f'(max(max_over_time({SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max(max_over_time({SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        "s",
        thresholds=[{"color": "#e53935", "value": None},  # 0-80s
                    {"color": "#fb8c00", "value": 80},    # 80-160s
                    {"color": "#fdd835", "value": 160},   # 160-240s
                    {"color": "#9ccc65", "value": 240},   # 240-300s
                    {"color": "#43a047", "value": 300}]))  # 300s+
    d["panels"].append(score_rank_panel(20, TABLE_H))  # fills the last header-row slot
    # Stat row: Game state / Player / Gearbox / Stage reached / Off-road / Longest
    # clean streak — six equal 4-wide tiles. Gearbox sits between Player and Stage
    # reached (per request); Longest clean streak is appended at the right end.
    row_y = next(p["gridPos"]["y"] for p in d["panels"] if p.get("id") == 2)
    row_widths = {2: (0, 4), 16: (4, 4), 14: (12, 4), 4: (16, 4)}  # id: (x, w)
    for p in d["panels"]:
        if p.get("id") in row_widths:
            p["gridPos"]["x"], p["gridPos"]["w"] = row_widths[p["id"]]
    d["panels"].append(gearbox_mode_panel(48, 8, row_y, 4))   # between Player and Stage reached
    d["panels"].append(longest_clean_panel(20, row_y, 4))
    # Sort "Overtakes by color" (id 17) bars descending (wrap the already-scoped expr).
    for p in d["panels"]:
        if p.get("id") == 17:
            for t in p.get("targets", []):
                if "expr" in t and not t["expr"].startswith("sort_desc("):
                    t["expr"] = f'sort_desc({t["expr"]})'
            break
    # Overtakes by vehicle (id 5): overtakes are all good -> shades of GREEN.
    for p in d["panels"]:
        if p.get("id") == 5:
            p["fieldConfig"]["defaults"]["color"] = {"mode": "shades", "fixedColor": "#66bb6a"}
            break
    # Crashes by type (id 3): the bargauge fails to render the label when the query
    # returns only ONE crash_type. Work around it with a SQL expr that LEFT JOINs a
    # fixed set of all 3 types against the counts (0-fill for missing) and orders by
    # count desc (most at top). A is hidden — it just feeds the expression.
    for p in d["panels"]:
        if p.get("id") == 3:
            p.pop("transformations", None)
            p["targets"] = [
                {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                 "editorMode": "code", "queryType": "instant",
                 "expr": f'sum by (crash_type) (count_over_time({SCOPED} | event="game.crash" [$__range]))'},
                {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
                 "expression": ("SELECT t.label AS crash_type, COALESCE(a.cnt, 0) AS crashes "
                                "FROM (SELECT 'bump' AS ct, 'Bump' AS label UNION ALL SELECT 'flip','Flip' "
                                "UNION ALL SELECT 'spin','Spin') t "
                                "LEFT JOIN (SELECT crash_type, `__value__` AS cnt FROM A) a ON a.crash_type = t.ct "
                                "ORDER BY crashes DESC")},
            ]
            p["fieldConfig"]["defaults"]["mappings"] = []  # labels now come from the SQL
            break
    # Route map (id 20): recolour from static rainbow to visited/unvisited. Every
    # stage defaults grey (in the DOT); a stage the player entered produces a
    # stage_id series with a value, which the threshold lights red. Unvisited stages
    # have no series, so they stay grey. (Edges left neutral for now — same
    # threshold/override mechanism can drive edgeOverrides later.)
    for p in d["panels"]:
        if p.get("id") == 20:
            p["options"]["dotDiagram"] = route_map_dot()
            p.pop("transformations", None)
            # Route colouring via SQL expressions over Loki (each override reads a FLAT
            # column). Completed stages -> GREEN; the stage the player timed out on -> RED;
            # taken edges -> GREEN; unvisited stages stay grey. A completed game has no red
            # node (every visited stage is green).
            #   A = stage.start events (stage_id as the ordered log line)
            #   B = session.end completion_status (the log line: "timeout"/"completed")
            #   C = per visited stage: status 2 (completed/green) or 1 (timed-out last stage/red)
            #   D = LAG over the ordered stage_ids -> "prev__to__cur" taken edge ids
            node_sql = ("SELECT DISTINCT s.sid AS stage_id, "
                        "CASE WHEN s.sid = (SELECT sid FROM (SELECT Line AS sid, `Time` AS t FROM A) q ORDER BY t DESC LIMIT 1) "
                        "AND (SELECT Line FROM B LIMIT 1) = 'timeout' THEN 1 ELSE 2 END AS status "
                        "FROM (SELECT Line AS sid FROM A) s")
            edge_sql = ("SELECT CONCAT(prev,'__to__',sid) AS edge_id, 1 AS taken "
                        "FROM (SELECT sid, lag(sid) OVER (ORDER BY t ASC) AS prev "
                        "FROM (SELECT `Time` AS t, Line AS sid FROM A) q1) q2 "  # `Time` backticked (MySQL identifier)
                        "WHERE prev IS NOT NULL")
            p["targets"] = [
                {"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                 "editorMode": "code", "queryType": "range", "maxLines": 50,
                 "expr": SCOPED + ' | event="game.stage.start" | line_format "{{.stage_id}}"'},
                {"refId": "B", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                 "editorMode": "code", "queryType": "range", "maxLines": 5,
                 "expr": SCOPED + ' | event="game.session.end" | line_format "{{.completion_status}}"'},
                {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
                 "expression": node_sql},
                {"refId": "D", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
                 "expression": edge_sql},
            ]
            p["options"]["namedThresholds"] = [
                {"id": "node-status", "name": "Node status",
                 "steps": [{"color": "transparent", "value": 0},
                           {"color": "#e53935", "value": 1},    # 1 = timed-out stage -> red
                           {"color": "#43a047", "value": 2}]},  # 2 = completed stage -> green
                {"id": "edge-green", "name": "Edge",
                 "steps": [{"color": "transparent", "value": 0}, {"color": "#43a047", "value": 1}]},
            ]
            p["options"]["nodeOverrides"] = [{
                "id": "route-nodes", "targetNodeIds": ROUTE_NODE_IDS,
                "matchFieldName": "stage_id", "matchPattern": "${id}",
                "rules": [{"kind": "fillColor", "colorFieldName": "status", "thresholdId": "node-status"}]}]
            p["options"]["edgeOverrides"] = [{
                "id": "route-edges", "targetEdgeIds": [f"{a}__to__{b}" for a, b in ROUTE_EDGES],
                "matchFieldName": "edge_id", "matchPattern": "${id}",
                "rules": [{"kind": "strokeColor", "colorFieldName": "taken", "thresholdId": "edge-green"}]}]
            break
    # Final frame (id 21) + Course map (id 22): these screenshots load a beat after
    # the rest, so show a "loading" placeholder rather than a "no screenshot" message;
    # and make the panels tall enough for the ~640x512 image (width:100%) not to clip.
    for p in d["panels"]:
        if p.get("id") in (21, 22):
            p["options"]["defaultContent"] = "Loading screenshot..."
            p["gridPos"]["h"] = 16
    # More rank tiles in a throwaway new row (layout TBD) — same rank_panel pattern
    # as Overall Rank, each "higher = 1st".
    rank_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(rank_panel(
        39, 0, rank_y, "Longest time played",
        f'(max by (session_label) (max_over_time({SEL} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max by (session_label) (max_over_time({SEL} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        f'(max(max_over_time({SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max(max_over_time({SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        "Rank by game duration among all games in the dashboard time range (1 = longest)."))
    d["panels"].append(rank_panel(
        40, 4, rank_y, "Most overtakes",
        f'sum by (session_label) (count_over_time({SEL} | event="game.vehicle_overtake" [$__range]))',
        f'sum(count_over_time({SCOPED} | event="game.vehicle_overtake" [$__range]))',
        "Rank by total overtakes among all games in the dashboard time range (1 = most)."))
    d["panels"].append(rank_panel(
        41, 8, rank_y, "Average speed",
        f'avg by (session_label) (avg_over_time({SEL} | unwrap speed_kph [$__range]))',
        f'avg(avg_over_time({SCOPED} | unwrap speed_kph [$__range]))',
        "Rank by average speed (km/h) among all games in the dashboard time range (1 = fastest)."))
    d["panels"].append(rank_panel(
        42, 12, rank_y, "Longest clean streak",
        f'max by (session_label) (max_over_time({SEL} | event="game.session.end" | unwrap longest_clean_seconds [$__range]))',
        f'max(max_over_time({SCOPED} | event="game.session.end" | unwrap longest_clean_seconds [$__range]))',
        "Rank by longest clean-driving streak among all games in the dashboard time range (1 = longest)."))
    d["panels"].append(speed_gauge_panel(
        46, 16, rank_y, 4, 5, "Fastest overtake",
        "Top speed (km/h) at which an overtake happened in the selected game.",
        f'max(max_over_time({SCOPED} | event="game.vehicle_overtake" | unwrap speed_kph [$__range]))'))
    d["panels"].append(rank_panel(
        47, 20, rank_y, "Fastest overtake rank",
        f'max by (session_label) (max_over_time({SEL} | event="game.vehicle_overtake" | unwrap speed_kph [$__range]))',
        f'max(max_over_time({SCOPED} | event="game.vehicle_overtake" | unwrap speed_kph [$__range]))',
        "Rank by fastest overtake speed among all games in the dashboard time range (1 = fastest)."))

    # Time-per-stage stacked bar — appended at the bottom (below all other panels)
    # so it never overlaps, regardless of the layout above.
    bottom = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(stage_time_bar_panel(bottom))
    # More summary panels in throwaway bottom rows (layout TBD).
    r = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(overtake_speed_hist_panel(43, 0, r))
    d["panels"].append(checkpoint_buffer_panel(44, 12, r))
    r2 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(score_progression_panel(r2))
    # Gear-shift panels — a new row at the very bottom (layout TBD): shifts per stage
    # (stacked up/down) + down-shift speed distribution.
    r3 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(shifts_per_stage_panel(49, 0, r3, 12))
    d["panels"].append(downshift_speed_hist_panel(50, 12, r3, 12))
    # Per-stage breakdowns (new bottom row, layout TBD): incidents (crashes+off-road,
    # stacked) + overtakes.
    r4 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(incidents_by_stage_panel(51, 0, r4, 12))
    d["panels"].append(overtakes_by_stage_panel(52, 12, r4, 12))
    # Up-shift speeds (companion to Down-shift speeds; meaningful in manual mode) +
    # Crashes by stage (crash-type breakdown, stacked).
    r5 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(upshift_speed_hist_panel(53, 0, r5, 12))
    d["panels"].append(crashes_by_stage_panel(54, 12, r5, 12))
    # Events by stage (all-event count) — placed in the Stage Progression row via layout.json.
    r6 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(events_by_stage_panel(55, 0, r6, 12))

    # Match the primary stat row (Game state / Player / Gearbox / Stage reached /
    # Off-road / Longest clean streak) to the header stat row height (5). The live
    # board ships these tiles at h=4; bump them to 5 and push everything below down
    # 1u so the taller row stays flush with the row beneath it.
    STAT_ROW_IDS = {2, 16, 48, 14, 4, 32}
    stat_row_y = next(p["gridPos"]["y"] for p in d["panels"] if p.get("id") == 2)
    for p in d["panels"]:
        if p["gridPos"]["y"] > stat_row_y:
            p["gridPos"]["y"] += 1
    for p in d["panels"]:
        if p.get("id") in STAT_ROW_IDS:
            p["gridPos"]["h"] = 5

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

# ---------------------------------------------------------------------------
# v1 -> v2 transpiler (schema dashboard.grafana.app/v2)
#
# build_picker still assembles the board in the familiar v1 shape (all the panel
# helpers, SQL exprs, session scoping, graphviz). This final step remaps that dict
# to the v2 manifest that `gcx dashboards update` wants: each panel becomes
# spec.elements["panel-<id>"], gridPos moves to a separate spec.layout, datasources
# resolve by DISPLAY NAME inline (no ${DS_LOKI} var / push-time substitution), and a
# few fields are renamed. See reference_v2_dashboard_schema in project memory.
# ---------------------------------------------------------------------------

NAMESPACE = "stacks-1144523"  # Grafana Cloud stack (simonprickett)
DS_NAME = {                    # v1 datasource uid -> v2 datasource display name
    "${DS_LOKI}": "grafanacloud-simonprickett-logs",
    "${DS_TEMPO}": "grafanacloud-simonprickett-traces",
}
_VAR_HIDE = {0: "dontHide", 1: "hideLabel", 2: "hideVariable"}
_AUTOREFRESH_INTERVALS = ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"]
_ANNOTATION_BUILTIN = {  # Grafana's default built-in annotation, v2 form
    "kind": "AnnotationQuery",
    "spec": {
        "builtIn": True, "enable": True, "hide": True,
        "iconColor": "rgba(0, 211, 255, 1)",
        "legacyOptions": {"type": "dashboard"},
        "name": "Annotations & Alerts",
        "query": {"kind": "DataQuery", "group": "grafana", "version": "v0",
                  "datasource": {"name": "-- Grafana --"}, "spec": {}},
    },
}


def _v2_query(t):
    # One v1 target -> one v2 PanelQuery. Datasource resolves by display name; the
    # remaining target keys (expr/queryType/editorMode/maxLines, or type/expression
    # for a SQL __expr__) become the DataQuery spec verbatim.
    ds = t.get("datasource", {})
    uid, typ = ds.get("uid", ""), ds.get("type", "")
    if typ == "__expr__" or uid == "__expr__":
        group, name = "__expr__", "__expr__"
    else:
        group, name = (typ or "loki"), DS_NAME.get(uid, uid)
    qspec = {k: v for k, v in t.items() if k not in ("refId", "datasource", "hide", "key")}
    return {"kind": "PanelQuery", "spec": {
        "query": {"kind": "DataQuery", "group": group, "version": "v0",
                  "datasource": {"name": name}, "spec": qspec},
        "refId": t.get("refId", "A"),
        "hidden": bool(t.get("hide", False)),   # v1 `hide` -> v2 `hidden`
    }}


def _v2_transform(tr):
    # v1 {id, options, ...} -> v2 {group:id, kind:"Transformation", spec:{options, ...}}
    return {"group": tr.get("id"), "kind": "Transformation",
            "spec": {k: v for k, v in tr.items() if k != "id"}}


def _v2_element(p):
    return {"kind": "Panel", "spec": {
        "id": p["id"],
        "title": p.get("title", ""),
        "description": p.get("description", ""),
        "links": p.get("links", []),
        "data": {"kind": "QueryGroup", "spec": {
            "queries": [_v2_query(t) for t in p.get("targets", [])],
            "transformations": [_v2_transform(tr) for tr in p.get("transformations", [])],
            "queryOptions": {},
        }},
        "vizConfig": {"kind": "VizConfig", "group": p["type"], "version": "",
                      "spec": {"options": p.get("options", {}),
                               "fieldConfig": p.get("fieldConfig", {"defaults": {}, "overrides": []})}},
    }}


def _load_layout(element_names):
    # Return the v2 layout object (RowsLayout) from layout.json. Panel gridPos in
    # build_picker is now ignored for v2 — layout is owned by layout.json, captured
    # from the UI. Safety net: any generated panel missing from the saved layout is
    # appended to a trailing "Unplaced" row so a newly-added panel never silently
    # vanishes before it's arranged in the UI.
    layout = json.loads(LAYOUT_FILE.read_text())
    placed = set()
    for row in layout.get("spec", {}).get("rows", []):
        for it in row["spec"]["layout"]["spec"]["items"]:
            placed.add(it["spec"]["element"]["name"])
    missing = [n for n in element_names if n not in placed]
    if missing:
        print(f"  NOTE: {len(missing)} panel(s) not placed in layout.json -> appended "
              f"to an 'Unplaced' row (arrange in the UI, then re-pull): {missing}", file=sys.stderr)
        items = [{"kind": "GridLayoutItem",
                  "spec": {"element": {"kind": "ElementReference", "name": n}}} for n in missing]
        layout.setdefault("spec", {}).setdefault("rows", []).append(
            {"kind": "RowsLayoutRow", "spec": {"title": "Unplaced", "collapse": False,
             "layout": {"kind": "GridLayout", "spec": {"items": items}}}})
    return layout


def _v2_variables(tlist):
    out = []
    for v in tlist:
        if v.get("type") == "textbox":
            out.append({"kind": "TextVariable", "spec": {
                "name": v["name"],
                "current": v.get("current", {"text": "", "value": ""}),
                "query": v.get("query", ""),
                "label": v.get("label", ""),
                "hide": _VAR_HIDE.get(v.get("hide", 0), "dontHide"),
                "skipUrlSync": v.get("skipUrlSync", False),
                "description": v.get("description", ""),
            }})
        # datasource template vars are dropped in v2 (datasources referenced by name)
    return out


def to_v2(v1):
    cursor = {0: "Off", 1: "Crosshair", 2: "Tooltip"}.get(v1.get("graphTooltip", 0), "Off")
    return {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {"name": v1["uid"], "namespace": NAMESPACE},
        "spec": {
            "annotations": [_ANNOTATION_BUILTIN],
            "cursorSync": cursor,
            "description": v1.get("description", ""),
            "editable": v1.get("editable", True),
            "elements": {f"panel-{p['id']}": _v2_element(p) for p in v1["panels"]},
            "layout": _load_layout([f"panel-{p['id']}" for p in v1["panels"]]),
            "links": v1.get("links", []),
            "liveNow": v1.get("liveNow", True),
            "preload": v1.get("preload", True),
            "tags": v1.get("tags", []),
            "timeSettings": {
                "from": v1.get("time", {}).get("from", "now-7d"),
                "to": v1.get("time", {}).get("to", "now"),
                "autoRefresh": v1.get("refresh", "") or "",
                "autoRefreshIntervals": _AUTOREFRESH_INTERVALS,
                "hideTimepicker": False,
                "fiscalYearStartMonth": v1.get("fiscalYearStartMonth", 0),
            },
            "title": v1["title"],
            "variables": _v2_variables(v1.get("templating", {}).get("list", [])),
        },
    }


def main():
    if not LIVE.exists():
        sys.exit(f"missing {LIVE}")
    live = json.loads(LIVE.read_text())
    picker_v1 = build_picker(live)
    n_expr = sum(1 for pid in SPECIAL if "expr" in SPECIAL[pid])
    n_scoped = sum(1 for p in picker_v1["panels"] for t in p.get("targets", [])
                   if 'session_label="$session"' in t.get("expr", ""))
    picker = to_v2(picker_v1)  # remap to the v2 manifest
    PICKER.write_text(json.dumps(picker, indent=2) + "\n")
    rows = picker["spec"]["layout"]["spec"]["rows"]
    print(f"Generated {PICKER.name} (v2 schema) from {LIVE.name}: "
          f"{len(picker['spec']['elements'])} panels, "
          f"{len(rows)} layout rows ({', '.join(r['spec']['title'] for r in rows)}), "
          f"{n_expr} session-scoped query rewrites, {n_scoped} targets filtered by session_label.")

if __name__ == "__main__":
    main()
