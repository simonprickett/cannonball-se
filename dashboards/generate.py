#!/usr/bin/env python3
"""
Single source of truth for the Cannonball-SE game dashboards.

The LIVE board (live_game_dashboard.json) is authored/refined by hand (or exported
from Grafana). This script derives the PICKER board (last_game_dashboard.json) from
it so the two never drift: same panels, layout, viz, DOT, screenshots — only the
query scoping and variables differ.

Usage:  python3 dashboards/generate.py
"""
import json, pathlib, sys, urllib.parse

HERE = pathlib.Path(__file__).parent
LIVE = HERE / "live_game_dashboard.json"
PICKER = HERE / "recent_games_dashboard.json"
LIVE_ENGINE = HERE / "live_engine_dashboard.json"
# Data-driven layout: the v2 spec.layout (RowsLayout) captured from the Grafana UI.
# Arrange in the UI, then `gcx dashboards get cannonball-recent-games` and save its
# spec.layout here (see dashboards/README/push.sh). Panel CONTENT stays in this
# generator; only positions + rows/tabs live in layout.json.
LAYOUT_FILE = HERE / "layout.json"

SEL = '{service_name="cannonball-se"}'
SCOPED = f'{SEL} | session_label="$session"'
# host_name is a resource attribute (structured metadata, like session_label) carried on
# every log line — see the Live Engine board's Host Selector, same pattern as the game picker.
HOST_SCOPED = f'{SEL} | host_name="$host"'

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
        # Hidden helper var: the selected game's gearbox_mode ("automatic"/"manual"),
        # set by the picker-table data link alongside `session`. Drives the Gearbox
        # Usage row's conditional rendering (show only when == "manual"). Empty for
        # pre-telemetry games or a hand-typed session -> row stays hidden.
        {"name": "gearbox", "label": "Gearbox mode", "type": "textbox",
         "query": "", "current": {"text": "", "value": ""},
         "options": [{"text": "", "value": "", "selected": True}],
         "hide": 2, "skipUrlSync": False,
         "description": "gearbox_mode of the selected game; set by the picker-table data link, drives the Gearbox Usage row conditional rendering."},
    ]


def recent_games_panel():
    # Loki-driven picker table: one row per game (session_label) over the dashboard
    # time range, newest first. Driven off game.session.start so each row also carries
    # the game's gearbox_mode. Clicking a row's data link sets BOTH the `session` and
    # `gearbox` textbox vars and reloads — session scopes every panel; gearbox drives
    # the Gearbox Usage row's conditional rendering (show only for manual games).
    # gearbox_mode is kept in the frame (for the link) but hidden as a column.
    return {
        "id": 30,
        "type": "table",
        "title": "Game Selector",
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
            # One series per game: session_label + gearbox_mode (from the single
            # session.start event each game emits). Pre-telemetry games have no
            # gearbox_mode label -> empty cell -> Gearbox row hidden for them.
            "expr": f'sum by (session_label, gearbox_mode) (count_over_time({SEL} | event="game.session.start" [$__range]))',
        }],
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False},
                    "sortBy": [{"displayName": "Game", "desc": True}]},
        "transformations": [
            {"id": "organize", "options": {
                # Keep the game (session_label) + gearbox_mode; drop Time and the count.
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
                         # Set session (scopes the board) AND gearbox (row conditional render).
                         "url": "/d/cannonball-recent-games/?var-session=${__value.raw}"
                                "&var-gearbox=${__data.fields[\"gearbox_mode\"]}&${__url_time_range}",
                         "targetBlank": False,
                     }]},
                 ]},
                # gearbox_mode stays in the frame (the link reads it) but is hidden as a column.
                {"matcher": {"id": "byName", "options": "gearbox_mode"},
                 "properties": [{"id": "custom.hidden", "value": True}]},
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
        "description": "Share of total game time spent on each stage — each segment is that stage's % of the game's total duration (stage_duration_seconds on game.stage.end, incl. the final stage to game over). Percent-stacked to 100%; hover for exact seconds and %.",
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
        "options": {"orientation": "horizontal", "stacking": "percent", "showValue": "auto",
                    "xField": "Player",
                    "groupWidth": 0.7, "barWidth": 0.97, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "percentunit",
                                     "color": {"mode": "fixed", "fixedColor": STAGE_COLORS[0]},
                                     "custom": {"fillOpacity": 85, "gradientMode": "hue",
                                                "lineWidth": 1, "axisPlacement": "auto",
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
                    "segmentCount": 63, "segmentSpacing": 0.3, "shape": "gauge",  # 63 = dashed arc
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
        "id": 45, "type": "barchart", "title": "Score per stage",
        "description": "Share of the final score earned in each stage — each segment is that stage's % of total points (score_end - score_start per stage). Percent-stacked to 100%; hover for exact points and %.",
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
        "options": {"orientation": "horizontal", "stacking": "percent", "showValue": "auto",
                    "xField": "Player", "groupWidth": 0.7, "barWidth": 0.97, "fullHighlight": True,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "fieldConfig": {"defaults": {"unit": "percentunit",
                                     "color": {"mode": "fixed", "fixedColor": SCORE_COLORS[0]},
                                     "custom": {"fillOpacity": 85, "gradientMode": "hue",
                                                "lineWidth": 1, "axisPlacement": "auto",
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
        38, x, y, "Rank: Overall",
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


# Grafana Cloud stack + Tempo/Loki datasources for the "View trace"/"View logs"
# panel's Explore links. Separate from DS_NAME (which resolves PANEL/QUERY
# datasource refs at v1->v2 time) because these are hand-built browser URLs, not
# query datasource refs.
GRAFANA_BASE_URL = "https://simonprickett.grafana.net"
TEMPO_DS_UID = "grafanacloud-traces"
LOKI_DS_UID = "grafanacloud-logs"


def _tempo_trace_url_template():
    # Traces Drilldown (the "grafana-exploretraces-app" plugin, née Explore Traces) —
    # a richer trace view (waterfall + service breakdown) than plain Explore's TraceQL
    # link, and a simpler URL: flat query params instead of a schemaVersion/panes JSON
    # blob. Confirmed against grafana/explore-traces source:
    #   - `traceId` is synced via its own SceneObjectUrlSync key (not a `var-`), and
    #     just opens a trace-detail drawer over whatever's behind it
    #     (src/pages/Explore/TraceExploration.tsx).
    #   - `var-ds` (VAR_DATASOURCE) is a DataSourceVariable with NO safe default —
    #     unset, it falls back to whatever datasource the browser last used
    #     (localStorage) — so it must be passed explicitly.
    #   - Every other var-* (primarySignal/metric/groupBy/filters/spanListColumns/
    #     latencyThreshold/durationPercentiles) and `actionView` has an in-app
    #     default (PrimarySignalVariable self-inits when empty; the un-set action
    #     view falls through to TracesByServiceScene's own default tab, "breakdown" —
    #     the first entry in actionViewsDefinitions) — dropped for simplicity.
    # `from`/`to` ARE kept explicit, same reasoning as the old link: Tempo's
    # retention is shorter than Loki's, so the app's own default time range can't be
    # trusted to cover an older game's trace.
    #
    # trace_id is only known at RENDER time (one per selected game), so the id is a
    # placeholder here; because it's made only of letters/underscore, urlencode leaves
    # it untouched and we can safely swap it for a mustache tag afterwards — the
    # dynamictext panel substitutes the real trace_id into the URL with no further
    # encoding needed (trace ids are plain hex, always URL-safe).
    placeholder = "TRACE_ID_PLACEHOLDER"
    query = urllib.parse.urlencode({
        "from": "now-30d",
        "to": "now",
        "traceId": placeholder,
        "var-ds": TEMPO_DS_UID,
    })
    url = f"{GRAFANA_BASE_URL}/a/grafana-exploretraces-app/explore?{query}"
    assert placeholder in url, "placeholder got percent-encoded — check its charset"
    return url.replace(placeholder, "{{{trace_id}}}")


def _loki_logs_url_template():
    # Plain Explore (not the Logs Drilldown app — session_label/trace_id are
    # structured metadata, and drilldown apps browse indexed labels/detected
    # fields, the same mismatch that ruled Tempo's tag-values picker out — see
    # [[project_loki_only_picker]]/reference_v2_dashboard_schema). Same
    # schemaVersion/panes shape as the original Tempo Explore link.
    #
    # Filters by trace_id, NOT session_label, even though this is the LOGS link —
    # trace_id uniquely identifies the same game (verified: `| trace_id="<id>"`
    # alone returns the full event set for the session) and is the only field this
    # panel already reliably extracts. Two earlier attempts at session_label both
    # failed live: "${session}" isn't interpolated by this panel type at all, and
    # splitting "trace_id::session_label" out of one query via `extractFields`
    # regex came back empty for session_label (untraced — maybe session_label's
    # spaces/colons from its timestamp prefix, maybe the transform itself; not
    # worth another guess when trace_id alone already does the job with the
    # SAME single-field mechanism already proven for the trace link below).
    placeholder = "TRACE_ID_PLACEHOLDER"
    panes = {
        "log": {
            "datasource": LOKI_DS_UID,
            "queries": [{
                "refId": "A",
                "datasource": {"type": "loki", "uid": LOKI_DS_UID},
                "queryType": "range",
                "expr": f'{SEL} | trace_id="{placeholder}"',
            }],
            "range": {"from": "now-30d", "to": "now"},
        }
    }
    query = urllib.parse.urlencode(
        {"schemaVersion": 1, "panes": json.dumps(panes, separators=(",", ":")), "orgId": 1})
    url = f"{GRAFANA_BASE_URL}/explore?{query}"
    assert placeholder in url, "placeholder got percent-encoded — check its charset"
    return url.replace(placeholder, "{{{trace_id}}}")


def view_trace_panel(pid, x, y, w, h):
    # Picker-only: direct links to the selected game's trace (Tempo, via Traces
    # Drilldown) and its raw logs (Loki, via Explore). The OTel trace is the
    # game_session root span plus its stage_N/post_game children — see
    # src/main/telemetry.cpp. Both links key off trace_id (see
    # _loki_logs_url_template for why the logs link uses trace_id rather than
    # session_label), fetched exactly like the (now-removed) Final frame/Course
    # map screenshot panels: one Loki line -> rename to a named field ->
    # interpolate into the dynamictext panel's HTML.
    return {
        "id": pid,
        "type": "marcusolsson-dynamictext-panel",
        "title": "🔗 View traces and logs",
        "description": ("Opens the selected game's trace (Tempo, via Traces Drilldown) "
                         "and its raw logs (Loki, via Explore). Tempo's trace retention "
                         "is shorter than Loki's, so a very old game from the picker may "
                         "404 the trace link even though its log rows (and the logs link) "
                         "are still around."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {
            "content": (
                '<div style="display:flex;flex-direction:column;gap:8px;'
                'text-align:center;font-size:16px;">'
                f'<a href="{_tempo_trace_url_template()}" target="_blank" '
                'rel="noopener">View trace ↗</a>'
                f'<a href="{_loki_logs_url_template()}" target="_blank" '
                'rel="noopener">View logs ↗</a>'
                '</div>'
            ),
            "defaultContent": "No trace_id for this game.",
            "everyRow": True,
        },
        "targets": [{
            "refId": "A",
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code",
            "queryType": "range",
            "expr": SCOPED + ' | trace_id != "" | line_format "{{.trace_id}}"',
            "maxLines": 1,
        }],
        "transformations": [
            {"id": "organize", "options": {"renameByName": {"Line": "trace_id"}}},
            {"id": "filterFieldsByName", "options": {"include": {"names": ["trace_id"]}}},
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
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
        # Synthwave gradient backdrop. Kept dark purple->indigo so the green/red/grey
        # nodes stay high-contrast (no pink/magenta, which would wash out red nodes).
        '  bgcolor="#160a2e:#3d1a6d";\n'
        '  gradientangle=90;\n'
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, '
        'fillcolor="#4a4a4a", fontcolor="white", color="#00000000", penwidth=1.5];\n'
        '  edge [arrowsize=0.7, color="#9e9e9e", penwidth=1.2];\n\n'
        + '\n'.join(nodes) + '\n\n'
        + '\n'.join(ranks) + '\n\n'
        + '\n'.join(invis) + '\n\n'
        + '\n'.join(edges) + '\n'
        '}'
    )


def duplicate_panel(src, new_id, title=None):
    # An IDENTICAL copy of panel `src` that re-uses the source's already-fetched query
    # results via Grafana's "-- Dashboard --" datasource (referenced by panelId), so
    # Loki runs the source's query only ONCE no matter how many copies exist. viz +
    # fieldConfig + transformations are copied verbatim; `withTransforms` is omitted so
    # the copy receives the source's RAW query results and re-applies the (copied)
    # transforms to reshape them identically. The transpiler maps the dashboard
    # datasource (type "datasource" / uid "-- Dashboard --" + panelId) with no changes.
    dup = json.loads(json.dumps(src))  # deep copy (viz, fieldConfig, transformations)
    dup["id"] = new_id
    if title is not None:
        dup["title"] = title
    dup["datasource"] = {"type": "datasource", "uid": "-- Dashboard --"}
    dup["targets"] = [{"refId": "A",
                       "datasource": {"type": "datasource", "uid": "-- Dashboard --"},
                       "panelId": src["id"]}]
    return dup


# ---------------------------------------------------------------------------
# LIVE ENGINE DASHBOARD — standalone (no picker, not derived from the live
# board). Focused on cabinet/engine state rather than one game's story:
# attract mode vs. playing, coin-box economics, and a live snapshot + raw log
# feed of whatever game is running now. This generator function IS the source
# of truth (there's no hand-authored "live" JSON behind it). Content-first
# per the user's request — layout/colour styling is deliberately plain for
# now and will be revisited once the content is right.
# ---------------------------------------------------------------------------

COIN_PRICE_USD = 0.25
# The whole board defaults to TODAY (now/d -> now), so every panel — coin
# economics included — just uses $__range rather than a separate fixed
# window. (A literal range vector over e.g. 30 days would still work, but
# "today" is the grain the user actually wants: a day's takings, not a
# rolling lookback.) NOTE: if the time picker is widened past 30d1h, metric
# queries on this Loki stack hard-error ("query time range exceeds the
# limit") — same as any $__range panel on the other boards; not special to
# this one.


def _line_stat(pid, x, y, w, title, description, event, field, sel=SEL, mappings=None, color="dark-blue"):
    # Stat tile: the <field> attribute off the most recently seen <event> log
    # line (a string field, so line_format rather than unwrap). noValue covers
    # attract mode / before the first game of all time. `mappings` lets a
    # caller re-case/relabel the raw logged value for display (e.g. the C++
    # side logs "automatic"/"VERY EASY" — shouty or inconsistent casing some
    # dashboards don't want) without changing what's actually logged — if a
    # mapping option carries its own "color", fixedColor:"text" defers to it
    # (same sentinel trick as Engine State/Alive/Selected host); otherwise
    # every value just gets the flat `color` background.
    fmt = "{{." + field + "}}"
    expr = f'{sel} | event="{event}" | line_format "{fmt}"'
    return {
        "id": pid, "type": "stat", "title": title, "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": 5},
        "targets": [{"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                     "editorMode": "code", "queryType": "range", "maxLines": 1, "expr": expr}],
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed",
                      "fixedColor": "text" if (mappings and any("color" in m for m in mappings.values())) else color},
            "noValue": "—",
            "mappings": [{"type": "value", "options": mappings}] if mappings else []},
                        "overrides": []},
        "options": {"colorMode": "background", "graphMode": "none",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": title, "values": False},
                    "textMode": "value"},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "tsNs": True, "id": True, "labels": True, "labelTypes": True},
            "renameByName": {"Line": title},
        }}],
    }


def _stat(pid, x, y, w, h, title, description, expr, unit="short", color="blue", colorMode="value"):
    # Generic plain stat tile (fixed colour, no thresholds yet — content first).
    return {
        "id": pid, "type": "stat", "title": title, "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                     "editorMode": "code", "queryType": "instant", "expr": expr}],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": colorMode, "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": unit, "mappings": [],
                                     "color": {"mode": "fixed", "fixedColor": color}},
                        "overrides": []},
    }


def _revenue_panel(pid, x, y, w, h, coins_expr):
    # SQL expr: coins (hidden query A) * $/coin. A matching instant query so the
    # SQL step runs once over the single scalar from A.
    #
    # Same circle/LED gauge style as Utilization (segmentCount 63, shape/style
    # "circle", showThresholdMarkers), max pinned to $20 so the arc fills
    # completely at/above that — but the gauge's min/max only clamp the VISUAL
    # fill, not the center text, which always shows the real reduced value
    # (e.g. $27.50 still reads as $27.50 even though the ring is full). Same
    # red/orange/yellow/green threshold palette as the stat version, just
    # reused as gauge color bands instead of a background gradient.
    return {
        "id": pid, "type": "gauge", "title": "💰 Revenue (today)",
        "description": (f"Coins inserted in the selected time range (today, by default) × "
                        f"${COIN_PRICE_USD:.2f}/coin. Gauge maxes out at $20 — the number in the "
                        f"middle keeps showing the real total even past that."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": coins_expr},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": f"SELECT `__value__` * {COIN_PRICE_USD} AS revenue FROM A"},
        ],
        "options": {"barShape": "flat", "barWidthFactor": 0.5,
                    "effects": {"barGlow": False, "centerGlow": False, "gradient": False},
                    "endpointMarker": "point", "minVizHeight": 75, "minVizWidth": 75,
                    "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "segmentCount": 63, "segmentSpacing": 0.3, "shape": "circle",
                    "showThresholdLabels": False, "showThresholdMarkers": True, "sizing": "auto",
                    "sparkline": False, "style": "circle", "textMode": "auto"},
        "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 2, "min": 0, "max": 20,
                                     "mappings": [], "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "#e57373", "value": None},
                                         {"color": "#ffb74d", "value": 5},
                                         {"color": "#fff176", "value": 10},
                                         {"color": "#81c784", "value": 15}]}},
                        "overrides": []},
    }


def _logs_panel(pid, x, y, w, h, title, description, expr, sort="Descending", prettify=False):
    return {
        "id": pid, "type": "logs", "title": title, "description": description,
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"dedupStrategy": "none", "enableLogDetails": True, "prettifyLogMessage": prettify,
                    "showLabels": False, "showTime": True, "sortOrder": sort, "wrapLogMessage": True},
        "targets": [{"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                     "editorMode": "code", "queryType": "range", "expr": expr}],
    }


def _alive_panel(pid, x, y, w, h):
    # Is the selected host alive RIGHT NOW? Any log line (the game.heartbeat, OR a
    # real in-game event) in the last 60 real seconds counts. Deliberately
    # hardcoded [60s], not $__range — aliveness is "right now", independent of
    # whatever historical range the picker is set to.
    #
    # Was 30s; bumped to 60s (2026-10-07, live testing) — 30s flickered OFFLINE
    # during normal quiet stretches with no telemetry at all: the menu sequence
    # between games (start, music select, name entry) and sitting still mid-game
    # without crashing/overtaking. The real fix is the heartbeat firing on every
    # frame regardless of state (not just attract mode) — see maybe_log_heartbeat()
    # call site in outrun.cpp — but a wider window is a cheap second line of
    # defence against any brief gap.
    return {
        "id": pid, "type": "stat", "title": "📶 Alive",
        "description": ("Has the selected host logged a heartbeat or any game event in the last "
                        "60 seconds? Independent of the time picker above. OFFLINE until the "
                        "game.heartbeat telemetry change is built onto the cabinet."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{
            "refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code", "queryType": "instant",
            "expr": f'sum(count_over_time({HOST_SCOPED} [60s])) or vector(0)',
        }],
        "fieldConfig": {"defaults": {
            "mappings": [
                {"type": "value", "options": {"0": {"text": "Offline", "color": "red", "index": 1}}},
                {"type": "range", "options": {"from": 1, "to": 9999999,
                                               "result": {"text": "Online", "color": "green", "index": 0}}},
            ],
            "color": {"mode": "fixed", "fixedColor": "text"},
        }, "overrides": []},
        "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "value"},
    }


def _credits_available_panel(pid, x, y, w, h, coins_expr):
    # Coins inserted minus games started, in the selected time range — no C++ change
    # needed, since Input::COIN (the real cabinet's coin path) already logs every
    # insert (see oinputs.cpp::do_credits; coin1/coin2 are dead/unreachable fields
    # from an unported upstream feature, not a second untelemetered coin path).
    games_expr = f'sum(count_over_time({HOST_SCOPED} | event="game.session.start" [$__range])) or vector(0)'
    return {
        "id": pid, "type": "stat", "title": "💰 Credits available",
        "description": ("Coins inserted minus games started, in the selected time range (today, "
                        "by default) — clamped at 0. A credit carried across midnight won't show "
                        "until it's spent or another coin is inserted today."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": coins_expr},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": games_expr},
            {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             # Scalar subqueries (not a join) — same style already proven in rank_panel's SQL.
             "expression": ("SELECT CASE WHEN ((SELECT `__value__` FROM A) - (SELECT `__value__` FROM B)) > 0 "
                            "THEN ((SELECT `__value__` FROM A) - (SELECT `__value__` FROM B)) ELSE 0 END AS credits")},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        "fieldConfig": {"defaults": {"unit": "short", "mappings": [],
                                     # Same synthwave-yellow as the Recent Games "Gearbox" panel —
                                     # coin-economics tiles the user wants visually grouped with it.
                                     "color": {"mode": "fixed", "fixedColor": "#ffd319"}},
                        "overrides": []},
    }


def _avg_duration_panel(pid, x, y, w, h):
    # Mean game duration across completed games for the selected host. JOIN
    # on session_label (exact), NOT an ordinal ROW_NUMBER() pairing — an
    # earlier attempt paired the Nth start with the Nth end by row position,
    # which goes silently wrong (and stays wrong for every later game) the
    # moment starts and ends aren't equal in count — confirmed broken live
    # (an abandoned/incomplete game elsewhere in the selected range produced
    # a bogus "1 day" average).
    #
    # This used to also carry a sparkline (one row per game instead of a
    # single AVG(), plus a convertFieldType transform to give it a real time
    # axis) — removed per the user's request; back to the simple scalar form.
    return {
        "id": pid, "type": "stat", "title": "⏱️ Average game duration",
        "description": ("Mean game length (session.end minus session.start) across completed "
                        "games for the selected host, in the selected time range."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (session_label) (max_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))'},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (session_label) (max_over_time({HOST_SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range]))'},
            {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT AVG((b.`__value__` - a.`__value__`)/1000.0) AS avg_duration_seconds "
                            "FROM A a JOIN B b ON a.session_label = b.session_label")},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        # Simple traffic light, low = good for the operator: green <=1min,
        # yellow for the 1-4min middle ground (covers the user's "2-3 min"
        # callout plus the unstated 1-2min gap, kept as one band for a true
        # 3-color light), red at 4min+. Same bands reused on Latest game
        # duration below.
        "fieldConfig": {"defaults": {"unit": "s", "decimals": 0, "mappings": [],
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "green", "value": None},
                                         {"color": "yellow", "value": 61},
                                         {"color": "red", "value": 240}]}},
                        "overrides": []},
    }


def _utilization_panel(pid, x, y, w, h):
    # % of the time the machine has actually been ON today spent PLAYING
    # (not % of the wall-clock range) — derivable purely from existing
    # events, no C++ change needed. Sum of completed games' durations /
    # (now - first heartbeat seen in range) * 100.
    #
    # "On since" = the FIRST game.heartbeat's own heartbeat_epoch_ms in the
    # selected range (D below) — not a native Loki Time column converted via
    # an SQL date function (to_timestamp() already failed once this session;
    # avoiding that whole class of risk by using the attribute's own ms value
    # instead, same unwrap pattern already proven for start/end_epoch_ms).
    # Deliberately NOT given an "or vector(0)" fallback: if there's truly no
    # heartbeat in range, "machine on since" is unknowable, and the SQL's
    # scalar subquery on an empty D correctly propagates NULL -> "No data",
    # which is the honest answer, not a misleading 0%.
    #
    # Session durations still use the exact session_label JOIN (same as
    # Average game duration — safe against abandoned/mismatched sessions,
    # unlike ordinal pairing). "Now" = $__to, not a fresh clock read, matching
    # the rest of this board's "as of the render" convention; both $__to and
    # $__from are confirmed interpolating inside a SQL expression.
    # Caveat: a game still IN PROGRESS right now isn't counted until it ends
    # (the JOIN needs both a start and an end) — self-corrects once it does.
    return {
        "id": pid, "type": "gauge", "title": "📊 Utilization",
        "description": ("Percentage of the time the machine has been ON today (since the "
                        "first heartbeat seen in the selected range) spent PLAYING, for the "
                        "selected host — sum of completed games' durations ÷ (now minus "
                        "first heartbeat). A game still in progress isn't counted until it ends."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (session_label) (max_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))'},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'max by (session_label) (max_over_time({HOST_SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range]))'},
            {"refId": "D", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'min(min_over_time({HOST_SCOPED} | event="game.heartbeat" | unwrap heartbeat_epoch_ms [$__range]))'},
            {"refId": "E", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT COALESCE(SUM(b.`__value__` - a.`__value__`), 0) "
                            "/ ($__to - (SELECT `__value__` FROM D)) * 100.0 AS utilization_pct "
                            "FROM A a JOIN B b ON a.session_label = b.session_label")},
        ],
        # Same circle/LED-segment gauge style as the Top speed/Avg speed gauges
        # (speed_gauge_panel): segmentCount 63 (dashed-arc "LED" look),
        # style:"circle", showThresholdMarkers — reused verbatim per the
        # user's request for "the gauge that's a circle with the LED style".
        "options": {"barShape": "flat", "barWidthFactor": 0.5,
                    "effects": {"barGlow": False, "centerGlow": False, "gradient": False},
                    "endpointMarker": "point", "minVizHeight": 75, "minVizWidth": 75,
                    "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "segmentCount": 63, "segmentSpacing": 0.3, "shape": "circle",  # full circle, not an arc
                    "showThresholdLabels": False, "showThresholdMarkers": True, "sizing": "auto",
                    "sparkline": False, "style": "circle", "textMode": "auto"},
        "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "decimals": 1,
                                     "color": {"mode": "thresholds"},
                                     # Color bands: red (barely used) -> orange -> yellow -> green
                                     # (well utilized). Easy to retune later — just these 4 values.
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "#e57373", "value": None},
                                         {"color": "#ffb74d", "value": 20},
                                         {"color": "#fff176", "value": 40},
                                         {"color": "#81c784", "value": 60}]},
                                     "mappings": []},
                        "overrides": []},
    }


def _time_since_last_game_panel(pid, x, y, w, h):
    # Minutes since the most recently started game began, floored to a whole
    # number — NOT the absolute start time. An absolute epoch (the previous
    # version, displayed via the "dateTimeFromNow" unit as "X ago") can't be
    # meaningfully thresholded: the raw value keeps growing every second, so
    # a fixed cutoff would never mean the same thing twice. Flooring (not
    # rounding) matches "latest game duration"'s counting-up feel — at 9m59s
    # it should still read 9, not jump to 10 a second early.
    start_expr = f'max(last_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range])) or vector(0)'
    return {
        "id": pid, "type": "stat", "title": "⏱️ Time since last game",
        "description": ("Minutes since the most recently started game began, for the selected "
                        "host — a long gap means the cabinet has been sitting idle, not "
                        "earning. Floored to a whole minute."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": start_expr},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": "SELECT FLOOR(($__to - `__value__`) / 60000.0) AS minutes_since_start FROM A"},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        # Idle traffic light: green while recently played, yellow for a mid
        # stretch, red once it's been a while — same bands discussed with the
        # user for this panel, inverted sense from the duration panels (here
        # a BIG gap is bad, not a long game).
        "fieldConfig": {"defaults": {"unit": "m", "decimals": 0, "mappings": [],
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "green", "value": None},
                                         {"color": "yellow", "value": 10},
                                         {"color": "red", "value": 30}]}},
                        "overrides": []},
    }


def _latest_duration_panel(pid, x, y, w, h):
    # Duration of the LATEST game. If it's still in progress — latest start newer
    # than latest end, same comparison as the Engine State panel — count up live
    # from start to $__to (the dashboard's "to" boundary; "now" for the default
    # today range). Otherwise it's simply end minus start. `$__to` is a core
    # Grafana macro (not a datasource variable), so — unlike $host/$session — it
    # DOES interpolate inside a SQL expression.
    start_expr = f'max(last_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range])) or vector(0)'
    end_expr = f'max(last_over_time({HOST_SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) or vector(0)'
    return {
        "id": pid, "type": "stat", "title": "⏱️ Latest game duration",
        "description": ("How long the most recently started game has lasted: counts up live "
                        "from its start time while still in progress (latest start newer than "
                        "latest end), otherwise end minus start — for the selected host in the "
                        "selected time range."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": start_expr},
            {"refId": "B", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant", "expr": end_expr},
            {"refId": "C", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT CASE WHEN (SELECT `__value__` FROM A) > (SELECT `__value__` FROM B) "
                            "THEN ($__to - (SELECT `__value__` FROM A)) / 1000.0 "
                            "ELSE ((SELECT `__value__` FROM B) - (SELECT `__value__` FROM A)) / 1000.0 "
                            "END AS duration_seconds")},
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                    "textMode": "auto", "wideLayout": True, "showPercentChange": False},
        # Same traffic-light bands as Average game duration — see that panel.
        "fieldConfig": {"defaults": {"unit": "s", "decimals": 0, "mappings": [],
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": [
                                         {"color": "green", "value": None},
                                         {"color": "yellow", "value": 61},
                                         {"color": "red", "value": 240}]}},
                        "overrides": []},
    }


CAR_COLORS = {  # car_pal (0-4) -> (label, background colour), from the user
    0: ("Red", "red"),
    1: ("Blue", "blue"),
    2: ("Yellow", "yellow"),
    3: ("Green", "green"),
    4: ("Turquoise", "#40e0d0"),
}


def _car_colour_panel(pid, x, y, w, h):
    # car_pal from the most recent game.startup, mapped to its actual in-game
    # colour as the panel's BACKGROUND (fixedColor:"text" + colorMode:"background"
    # tells Grafana to paint the background with whatever colour is attached to
    # the matched value mapping — same mechanism as the Engine State/Alive panels).
    return {
        "id": pid, "type": "stat", "title": "🚗 Car colour",
        "description": ("Configured car colour (config.xml <engine><car_color>, car_pal 0-4) "
                        "from the most recent game.startup on the selected host."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{
            "refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code", "queryType": "instant",
            "expr": f'max(last_over_time({HOST_SCOPED} | event="game.startup" | unwrap car_pal [$__range]))',
        }],
        "fieldConfig": {"defaults": {
            "mappings": [{"type": "value", "options": {
                str(n): {"text": label, "color": color, "index": n}
                for n, (label, color) in CAR_COLORS.items()
            }}],
            "color": {"mode": "fixed", "fixedColor": "text"},
        }, "overrides": []},
        "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "value"},
    }


def _revenue_per_hour_panel(pid, x, y, w, h):
    # Cumulative revenue over time (running total), line + gradient fill —
    # NOT hourly bars. An earlier hourly-bucket version needed `offset -1h`
    # to label each bar by its bucket START rather than its END (LogQL
    # range-vector windows are trailing: count_over_time(X[1h]) at T covers
    # (T-1h,T], so an 11-12 event naturally labels at 12:00). But that
    # offset trick has a fatal side effect for a LIVE dashboard: the grid
    # point for the current, still-forming hour needs to evaluate as if
    # "now" were 1h in the future (to look at its own (T,T+1h] window), and
    # Loki returns NO data at all for a grid point whose effective eval time
    # is still in the future — not partial data for the elapsed portion,
    # nothing. Confirmed live: a coin inserted 4 minutes earlier didn't show
    # up at all, with or without the offset, because the current hour's
    # bucket can only resolve once real wall-clock time passes its END.
    #
    # A running total sidesteps this entirely: no bucket, no offset, no
    # "which label does this belong to" question. Each fine-grained step
    # ($__interval, auto-sized from panel width) just counts coins in that
    # tiny slice (`or vector(0)` so every step is an explicit number, never
    # a gap — same reasoning as the old hourly version), and the
    # `calculateField`/cumulative transform turns those per-step deltas into
    # a running sum. Confirmed via `gcx logs metrics ... --step 5m`: a coin
    # inserted 4 minutes prior showed up immediately at the next 5m step,
    # with no delay — the fix for the live-data gap the user hit.
    return {
        "id": pid, "type": "timeseries", "title": "📈 Cumulative revenue",
        "description": ("Running total revenue (game.coin_inserted events × $0.25/coin) "
                        "across the selected range, for the selected host. Updates live as "
                        "coins are inserted — no hourly bucketing, so there's no lag waiting "
                        "for an hour to complete."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "range",
             "expr": f'sum(count_over_time({HOST_SCOPED} | event="game.coin_inserted" [$__interval])) * {COIN_PRICE_USD} or vector(0)'},
        ],
        "transformations": [
            # mode MUST be "cumulativeFunctions", not "cumulative" — confirmed
            # against Grafana's own calculateField.ts source (CalculateFieldMode
            # enum). Got this wrong first; an unrecognized mode string doesn't
            # error, it silently no-ops, so the panel just showed the raw
            # per-step deltas (spikes that drop back to 0) instead of a running
            # total — looked plausible enough to be mistaken for "working" at a
            # glance, caught only by checking the actual shape against what a
            # true monotonic cumulative sum must look like.
            {"id": "calculateField", "options": {
                "mode": "cumulativeFunctions",
                "cumulative": {"field": "Value", "reducer": "sum"},
                "alias": "Revenue",
                "replaceFields": True,
            }},
        ],
        "options": {"tooltip": {"mode": "single", "sort": "none"},
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False}},
        "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 2,
                                     "color": {"mode": "fixed", "fixedColor": "#fb8c00"},
                                     "custom": {"drawStyle": "line", "barAlignment": 0,
                                                "lineWidth": 2, "fillOpacity": 25,
                                                "gradientMode": "opacity", "spanNulls": False,
                                                "showPoints": "never", "pointSize": 5,
                                                "axisPlacement": "auto", "axisLabel": "Revenue",
                                                "stacking": {"mode": "none", "group": "A"},
                                                "thresholdsStyle": {"mode": "off"}},
                                     "mappings": []},
                        "overrides": []},
    }


def _music_plays_panel(pid, x, y, w, h):
    # Count of games started per music track (game.session.start's music_selection),
    # fixed-category LEFT JOIN fill-zero so all 4 bars always render (bargauge won't
    # label a single returned series — same workaround as checkpoint_buffer_panel).
    # Track-name convention matches the single-game music_panel helper (id 33):
    # 0 Magical Sound Shower / 1 Passing Breeze / 2 Splash Wave / else Custom.
    return {
        "id": pid, "type": "bargauge", "title": "🎵 Music plays",
        "description": ("Count of games started with each music track, for the selected host "
                        "in the selected time range."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": "A", "hide": True, "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
             "editorMode": "code", "queryType": "instant",
             "expr": f'sum by (music_selection) (count_over_time({HOST_SCOPED} | event="game.session.start" [$__range]))'},
            {"refId": "B", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "sql",
             "expression": ("SELECT t.label AS track, COALESCE(SUM(a.cnt), 0) AS plays "
                            "FROM (SELECT '0' AS v, 'Magical Sound Shower' AS label UNION ALL SELECT '1','Passing Breeze' "
                            "UNION ALL SELECT '2','Splash Wave' UNION ALL SELECT '3','Custom') t "
                            "LEFT JOIN (SELECT CASE WHEN CAST(music_selection AS SIGNED) >= 3 THEN '3' "
                            "ELSE music_selection END AS v, `__value__` AS cnt FROM A) a ON a.v = t.v "
                            "GROUP BY t.label, t.v ORDER BY t.v")},
        ],
        "options": {"displayMode": "lcd", "orientation": "horizontal", "valueMode": "color",
                    "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}},
        # A value-based ramp (pale peach at low counts) looked washed-out/cold
        # with this data's mostly-small play counts — just one flat saturated
        # warm orange instead, matching Revenue per hour.
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#fb8c00"},
                                     "mappings": []},
                        "overrides": []},
    }


HOST_TABLE_H = 6  # grid units for the Host Selector table


def host_selector_panel():
    # Every host (host_name) active in the selected time range (today, by
    # default), alphabetical. Same picker pattern as Recent Games' "Game
    # Selector": host_name is structured metadata (not a stream label — the
    # only stream label is service_name), so Loki label_values can't
    # enumerate it. Instead, list hosts via a metric query and let a row's
    # data link set the $host textbox. Deliberately UNSCOPED (plain SEL) —
    # this is the one panel that must see every host, not just the selected one.
    return {
        "id": 200, "type": "table", "title": "🖥️ Host Selector",
        "description": ("Every cabinet (host_name) active in the selected time range (today, "
                        "by default). Click a row to scope the whole board to that cabinet — "
                        "useful once more than one is deployed."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": 0, "y": 0, "w": 8, "h": HOST_TABLE_H},
        "targets": [{
            "refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code", "queryType": "instant",
            "expr": f'sum by (host_name) (count_over_time({SEL} [$__range]))',
        }],
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                    "sortBy": [{"displayName": "Host", "desc": False}]},
        "transformations": [
            {"id": "organize", "options": {
                # The count (count_over_time's Value, named "Value #A" for a single
                # refId) only existed to drive sorting; not meaningful to a picker,
                # so drop it like the Game Selector does.
                "excludeByName": {"Time": True, "Value": True, "Value #A": True},
                "renameByName": {"host_name": "Host"},
            }},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Host"},
                 "properties": [
                     {"id": "custom.width", "value": 240},
                     {"id": "links", "value": [{
                         "title": "View this host",
                         "url": "/d/cannonball-live-engine/?var-host=${__value.raw}&${__url_time_range}",
                         "targetBlank": False,
                     }]},
                 ]},
            ],
        },
    }


def _selected_host_panel(pid, x, y, w, h):
    # Shows which host the whole board is currently scoped to, or a loud
    # "NO HOST SELECTED" when $host is empty — distinct from the Host Selector
    # table, which lists candidates rather than stating the current selection.
    #
    # $host is a plain Grafana template variable: it's textually substituted
    # into the query BEFORE the request reaches Loki, so `line_format "$host"`
    # becomes a literal string ("redpi4", or "" when unset) baked into the
    # LogQL text itself. Deliberately queries ALL events (plain SEL, no host
    # filter) rather than HOST_SCOPED, so this doesn't depend on the selected
    # host actually having data — it only needs the SERVICE to have logged
    # anything, ever, which is a much safer assumption than "this specific
    # host has recent events" (a newly-picked host with zero data yet would
    # otherwise show empty/NO HOST SELECTED, which would be wrong).
    return {
        "id": pid, "type": "stat", "title": "📍 Selected host",
        "description": ("Which host the rest of this board is scoped to. Click a row in Host "
                        "Selector (or type one) to change it."),
        "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{
            "refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "editorMode": "code", "queryType": "range", "maxLines": 1,
            "expr": f'{SEL} | line_format "$host"',
        }],
        "fieldConfig": {"defaults": {
            "noValue": "NO HOST SELECTED",
            "mappings": [
                {"type": "special", "options": {
                    "match": "empty", "result": {"text": "NO HOST SELECTED", "color": "red"}}},
                # No "text" here -> Grafana keeps the original value (the host
                # name) on display, just tints it blue for the "selected" state.
                {"type": "regex", "options": {"pattern": ".+", "result": {"color": "blue"}}},
            ],
            # "text" (not an actual colour) tells Grafana to use whatever colour
            # the matched mapping carries, instead of forcing a fixed one — same
            # trick as Engine State/Alive. A real fixedColor here would override
            # the mappings' own colours, which is exactly the bug this fixes.
            "color": {"mode": "fixed", "fixedColor": "text"},
        }, "overrides": []},
        "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                    # "fields" must name the renamed string field explicitly — left as ""
                    # (auto) first, which defaults to picking a NUMERIC field and silently
                    # found none, rendering as if the value were empty regardless of $host.
                    # Same explicit-fields requirement as the proven _line_stat helper.
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "Host", "values": False},
                    "textMode": "value"},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "tsNs": True, "id": True, "labels": True, "labelTypes": True},
            "renameByName": {"Line": "Host"},
        }}],
    }


def build_live_engine():
    # `or vector(0)` guarantees a non-empty result even when $host matches nothing (e.g. no
    # host picked yet) — without it, the Revenue panel's SQL expression errors on an empty
    # input frame (no `__value__` column to select), rather than just showing "No data".
    coins_expr = f'sum(count_over_time({HOST_SCOPED} | event="game.coin_inserted" [$__range])) or vector(0)'
    coin_line_fmt = "Coin inserted — {{.credits}} credit(s) now in machine"
    raw_line_fmt = "{{.event}}  stage={{.stage_number}}  speed={{.speed_kph}}  score={{.score}}"

    y0 = HOST_TABLE_H  # everything below the Host Selector shifts down by its height

    panels = [
        host_selector_panel(),
        _selected_host_panel(206, 8, 0, 16, HOST_TABLE_H),

        # Row 1 — engine state, aliveness, coin economics snapshot (operator-focused)
        {
            "id": 201, "type": "stat", "title": "🎮 Engine state",
            "description": ("What's happening right now on the selected host: ATTRACT MODE, "
                            "PLAYING, POST GAME (game.gameover fired — GAME OVER text / bonus "
                            "road / course map / high-score table / name entry — but "
                            "game.session.end hasn't landed yet), or UNKNOWN if the host hasn't "
                            "logged anything in the last 60s (same check as Alive) — an "
                            "abandoned/killed session never logs game.session.end, so without "
                            "this fallback a dead cabinet's last game would show PLAYING or "
                            "POST GAME forever. Confirmed live 2026-10-07 — a session cut short "
                            "by stopping the Pi stayed PLAYING indefinitely."),
            "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
            "gridPos": {"x": 0, "y": y0, "w": 6, "h": 5},
            "targets": [{
                "refId": "A", "datasource": {"type": "loki", "uid": "${DS_LOKI}"},
                "editorMode": "code", "queryType": "instant",
                # alive = 1 if the host logged anything in the last 60s, else 0 (same
                # [60s] window as _alive_panel). session_open = 1 if the latest
                # session.start is newer than the latest session.end (a game is
                # underway in SOME sense — playing or post-game). post_game = 1 if
                # the latest game.gameover is newer than the latest session.start
                # (gameplay control has ended for THIS session, but session.end
                # hasn't landed yet). Arithmetic ternary (no `if` in LogQL):
                # result = (1-alive)*3 + alive*session_open*(1+post_game)
                #   -> 3 UNKNOWN (not alive, regardless of anything else)
                #   -> 0 ATTRACT (alive, session closed)
                #   -> 1 PLAYING (alive, session open, not yet post-game)
                #   -> 2 POST GAME (alive, session open, post-game event is newer)
                "expr": (
                    '(1 - ((sum(count_over_time(' + HOST_SCOPED + ' [60s])) or vector(0)) > bool 0)) * 3 '
                    '+ ((sum(count_over_time(' + HOST_SCOPED + ' [60s])) or vector(0)) > bool 0) '
                    f'* ((max(last_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range])) or vector(0)) '
                    f'> bool (max(last_over_time({HOST_SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) or vector(0))) '
                    f'* (1 + ((max(last_over_time({HOST_SCOPED} | event="game.gameover" | unwrap gameover_epoch_ms [$__range])) or vector(0)) '
                    f'> bool (max(last_over_time({HOST_SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range])) or vector(0))))'
                ),
            }],
            "fieldConfig": {"defaults": {
                "mappings": [{"type": "value", "options": {
                    "1": {"text": "Playing", "color": "green", "index": 0},
                    # Orange, not blue — attract mode is the "no money coming
                    # in" state, worth visually flagging as the lesser state
                    # vs. Playing's green.
                    "0": {"text": "Attract mode", "color": "#fb8c00", "index": 1},
                    # Purple — distinct from all 3 other states, reads as
                    # "wrapping up" (game over text / bonus road / course map /
                    # high-score table / name entry, all bundled as one state).
                    "2": {"text": "Post game", "color": "#ab47bc", "index": 2},
                    "3": {"text": "Unknown", "color": "#808080", "index": 3},
                }}],
                "color": {"mode": "fixed", "fixedColor": "text"},
            }, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "textMode": "value"},
        },
        _alive_panel(205, 6, y0, 6, 5),
        _credits_available_panel(212, 12, y0, 6, 5, coins_expr),
        _stat(210, 18, y0, 6, 5, "💰 Coins inserted (today)",
              "game.coin_inserted events in the selected time range (today, by default) on the "
              "selected host — one per physical coin.",
              coins_expr, color="#ffd319", colorMode="background"),

        # Row 2 — revenue + game timing, for the selected host/time range
        _revenue_panel(211, 0, y0 + 5, 6, 5, coins_expr),
        _time_since_last_game_panel(213, 6, y0 + 5, 6, 5),
        _latest_duration_panel(214, 12, y0 + 5, 6, 5),
        _avg_duration_panel(216, 18, y0 + 5, 6, 5),

        # Row 3 — utilization (% of the range spent PLAYING vs attract mode)
        _utilization_panel(225, 0, y0 + 10, 6, 5),

        # Row 4 — config snapshot, from the one-time game.startup event
        _car_colour_panel(217, 0, y0 + 15, 6, 5),
        # Automatic is easier to drive -> green. Manual keeps the synthwave
        # yellow (#ffd319, the original flat colour both values used to
        # share, matching the "Gearbox" panel on Recent Games) as the
        # harder/"watch out" mode.
        _line_stat(218, 6, y0 + 15, 6, "🕹️ Gearbox mode",
                   "Configured transmission mode (automatic/manual) from the most recent "
                   "game.startup on the selected host.",
                   "game.startup", "gearbox_mode", sel=HOST_SCOPED,
                   mappings={"automatic": {"text": "Automatic", "color": "green"},
                             "manual": {"text": "Manual", "color": "#ffd319"}}),
        # Green -> red ramp by difficulty (easy=good/green, hard=danger/red).
        # INFINITE isn't on that scale at all (no time pressure, a distinct
        # mode rather than a difficulty level) -> neutral blue, not green.
        _line_stat(219, 12, y0 + 15, 6, "🎯 Difficulty",
                   "Configured game-time DIP-switch difficulty from the most recent "
                   "game.startup on the selected host.",
                   "game.startup", "difficulty", sel=HOST_SCOPED,
                   mappings={"VERY EASY": {"text": "Very easy", "color": "#4caf50"},
                             "EASY": {"text": "Easy", "color": "#8bc34a"},
                             "NORMAL": {"text": "Normal", "color": "#fdd835"},
                             "HARD": {"text": "Hard", "color": "#fb8c00"},
                             "HARDEST": {"text": "Hardest", "color": "#e53935"},
                             "INFINITE": {"text": "Infinite", "color": "blue"}}),
        # Same green -> red ramp; DISABLED (no traffic at all) is its own
        # neutral blue, not part of the difficulty scale either.
        _line_stat(223, 18, y0 + 15, 6, "🚦 Traffic difficulty",
                   "Configured traffic DIP-switch difficulty from the most recent game.startup "
                   "on the selected host.",
                   "game.startup", "traffic_difficulty", sel=HOST_SCOPED,
                   mappings={"EASY": {"text": "Easy", "color": "#8bc34a"},
                             "NORMAL": {"text": "Normal", "color": "#fdd835"},
                             "HARD": {"text": "Hard", "color": "#fb8c00"},
                             "HARDEST": {"text": "Hardest", "color": "#e53935"},
                             "DISABLED": {"text": "Disabled", "color": "blue"}}),

        # Row 5 — usage patterns
        _revenue_per_hour_panel(240, 0, y0 + 20, 24, 8),

        # Row 6 — music track popularity
        _music_plays_panel(250, 0, y0 + 28, 24, 6),

        # Row 7 — live coin feed for the selected host
        _logs_panel(215, 0, y0 + 34, 24, 6, "🪙 Coin inserts",
                    "Every game.coin_inserted event on the selected host, newest first.",
                    f'{HOST_SCOPED} | event="game.coin_inserted" | line_format "{coin_line_fmt}"'),

        # Row 8 — raw log feed for the selected host
        _logs_panel(230, 0, y0 + 40, 24, 10, "📜 Raw event log",
                    "Every event for the selected host in the selected time range (today, by "
                    "default), newest first — the full, unfiltered feed from whatever game is "
                    "running now (or the most recent one, in attract mode).",
                    f'{HOST_SCOPED} | line_format "{raw_line_fmt}"', prettify=True),
    ]

    return {
        "uid": "cannonball-live-engine",
        "title": "Cannonball-SE — Live Engine",
        "description": ("Operator view of the cabinet for TODAY: attract mode vs. playing, "
                        "aliveness (heartbeat + events), coin-box economics (coins + revenue "
                        "@ $0.25/coin + credits available), usage by hour, music popularity, "
                        "and the raw log feed. Click a host in the Host Selector to scope the "
                        "whole board — for when multiple cabinets share this Loki instance. "
                        "Content-first cut; layout/styling to be refined."),
        "tags": ["cannonball-se", "loki", "game", "live", "engine"],
        "editable": True,
        "graphTooltip": 0,
        "liveNow": True,
        "preload": True,
        "refresh": "10s",
        "time": {"from": "now/d", "to": "now"},
        "panels": panels,
        "templating": {"list": [
            {"name": "host", "label": "Host", "type": "textbox",
             "query": "", "current": {"text": "", "value": ""},
             "options": [{"text": "", "value": "", "selected": True}],
             "hide": 0, "skipUrlSync": False,
             "description": "host_name of the cabinet to view. Click a row in 'Host Selector' "
                            "to set it, or type one directly (e.g. redpi4, cannonball3)."},
        ]},
    }


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
    # Drop the Final frame (id 21) + Course map (id 22) single-shot screenshots —
    # removed in the v2 layout (Route map moved into the Overview row; Route row gone).
    d["panels"] = [p for p in d["panels"] if p.get("id") not in (21, 22)]
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
    # gridPos here is a placeholder — v2 layout is owned by layout.json (see
    # _load_layout); this panel starts in the auto-appended "Unplaced" row until
    # it's dragged into place in the UI and layout.json is re-pulled.
    d["panels"].append(view_trace_panel(59, 0, row_y + 4, 6, 4))
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
    # Key moments (id 23): reverse the screenshot flow so the most recent (game-over)
    # frame is first. The live board reads them oldest-first (Loki direction "forward");
    # flip to "backward" for the picker only.
    for p in d["panels"]:
        if p.get("id") == 23:
            p["options"]["defaultContent"] = "Loading screenshots..."
            for t in p.get("targets", []):
                t["direction"] = "backward"
            # Loki returns the frame time-ascending regardless of query direction, so
            # also sort rows by Time DESC — inserted BEFORE the field-filter that drops
            # Time — to force the newest (game-over) screenshot to render first.
            txs = p.get("transformations", [])
            idx = next((i for i, t in enumerate(txs) if t.get("id") == "filterFieldsByName"), len(txs))
            txs.insert(idx, {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": True}]}})
            p["transformations"] = txs
            break
    # More rank tiles in a throwaway new row (layout TBD) — same rank_panel pattern
    # as Overall Rank, each "higher = 1st".
    rank_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(rank_panel(
        39, 0, rank_y, "Rank: Game duration",
        f'(max by (session_label) (max_over_time({SEL} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max by (session_label) (max_over_time({SEL} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        f'(max(max_over_time({SCOPED} | event="game.session.end" | unwrap end_epoch_ms [$__range])) '
        f'- max(max_over_time({SCOPED} | event="game.session.start" | unwrap start_epoch_ms [$__range]))) / 1000',
        "Rank by game duration among all games in the dashboard time range (1 = longest)."))
    d["panels"].append(rank_panel(
        40, 4, rank_y, "Rank: Overtaking",
        f'sum by (session_label) (count_over_time({SEL} | event="game.vehicle_overtake" [$__range]))',
        f'sum(count_over_time({SCOPED} | event="game.vehicle_overtake" [$__range]))',
        "Rank by total overtakes among all games in the dashboard time range (1 = most)."))
    d["panels"].append(rank_panel(
        41, 8, rank_y, "Rank: Average speed",
        f'avg by (session_label) (avg_over_time({SEL} | unwrap speed_kph [$__range]))',
        f'avg(avg_over_time({SCOPED} | unwrap speed_kph [$__range]))',
        "Rank by average speed (km/h) among all games in the dashboard time range (1 = fastest)."))
    d["panels"].append(rank_panel(
        42, 12, rank_y, "Rank: Clean streak",
        f'max by (session_label) (max_over_time({SEL} | event="game.session.end" | unwrap longest_clean_seconds [$__range]))',
        f'max(max_over_time({SCOPED} | event="game.session.end" | unwrap longest_clean_seconds [$__range]))',
        "Rank by longest clean-driving streak among all games in the dashboard time range (1 = longest)."))
    d["panels"].append(speed_gauge_panel(
        46, 16, rank_y, 4, 5, "Fastest overtake",
        "Top speed (km/h) at which an overtake happened in the selected game.",
        f'max(max_over_time({SCOPED} | event="game.vehicle_overtake" | unwrap speed_kph [$__range]))'))
    d["panels"].append(rank_panel(
        47, 20, rank_y, "Rank: Fastest overtake",
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
    incidents51 = incidents_by_stage_panel(51, 0, r4, 12)
    d["panels"].append(incidents51)
    overtakes52 = overtakes_by_stage_panel(52, 12, r4, 12)
    d["panels"].append(overtakes52)
    # Duplicate of Overtakes-by-stage (id52) shown in the Stage Progression row too —
    # re-uses id52's query results (no extra Loki query). Positioned via layout.json.
    d["panels"].append(duplicate_panel(overtakes52, 56))
    # Up-shift speeds (companion to Down-shift speeds; meaningful in manual mode) +
    # Crashes by stage (crash-type breakdown, stacked).
    r5 = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in d["panels"])
    d["panels"].append(upshift_speed_hist_panel(53, 0, r5, 12))
    crashes54 = crashes_by_stage_panel(54, 12, r5, 12)
    d["panels"].append(crashes54)
    # Duplicates of Incidents-by-stage (id51) + Crashes-by-stage (id54) shown in the
    # Stage Progression row too — reuse the sources' query results (no extra Loki
    # queries). Positioned via layout.json.
    d["panels"].append(duplicate_panel(incidents51, 57))
    d["panels"].append(duplicate_panel(crashes54, 58))
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

    # Emoji title prefixes (Recent-Games-only). Applied last so it covers inherited,
    # picker-added AND duplicated panels uniformly by id. 😃 player, ✅ stage, 💯 score,
    # 🏁 overall/state, 🚘🚘 overtaking, ⏱️ speed/duration/streak, 🕹️ gear (+⏱️ for shift
    # speeds), 🙈 incidents, 😳 off-road, 📈 events, 🤕 crashes. (Music/Route/Key moments/
    # per-stage already carry their own emoji from earlier edits — not listed here.)
    TITLE_EMOJI = {
        30: "🕹️",  # Game Selector table
        16: "😃", 14: "✅", 7: "💯", 45: "💯",
        2: "🏁", 38: "🏁",
        5: "🚘🚘", 17: "🚘🚘", 35: "🚘🚘", 40: "🚘🚘", 43: "🚘🚘",
        46: "🚘🚘", 47: "🚘🚘", 52: "🚘🚘", 56: "🚘🚘",
        6: "⏱️", 15: "⏱️", 32: "⏱️", 36: "⏱️", 39: "⏱️", 41: "⏱️", 42: "⏱️",
        48: "🕹️", 49: "🕹️", 50: "🕹️⏱️", 53: "🕹️⏱️",
        51: "🙈", 57: "🙈",
        4: "😳",
        31: "📈", 55: "📈",
        3: "🤕", 10: "🤕", 34: "🤕", 54: "🤕", 58: "🤕",
    }
    for p in d["panels"]:
        emoji = TITLE_EMOJI.get(p.get("id"))
        if emoji and not p.get("title", "").startswith(emoji):
            p["title"] = f"{emoji} {p['title']}"

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
    # A per-TARGET "interval"/"intervalMs" (the classic v1 "min step" override)
    # lands inside the DataQuery's own spec via _v2_query's generic passthrough
    # — which Loki's query plugin just ignores as an unrecognized field, so it
    # silently does nothing. The min-interval override actually belongs at the
    # QueryGroup level (sibling to "queries"), which the transpiler previously
    # always left empty. Confirmed live: an hourly bar chart kept showing a
    # sub-hour staircase (3 coins minutes apart rendered as 3 separate steps)
    # until the override was moved from the target to panel-level "interval"/
    # "maxDataPoints", mapped here into queryOptions.
    query_options = {}
    if "interval" in p:
        query_options["interval"] = p["interval"]
    if "maxDataPoints" in p:
        query_options["maxDataPoints"] = p["maxDataPoints"]
    return {"kind": "Panel", "spec": {
        "id": p["id"],
        "title": p.get("title", ""),
        "description": p.get("description", ""),
        "links": p.get("links", []),
        "data": {"kind": "QueryGroup", "spec": {
            "queries": [_v2_query(t) for t in p.get("targets", [])],
            "transformations": [_v2_transform(tr) for tr in p.get("transformations", [])],
            "queryOptions": query_options,
        }},
        "vizConfig": {"kind": "VizConfig", "group": p["type"], "version": "",
                      "spec": {"options": p.get("options", {}),
                               "fieldConfig": p.get("fieldConfig", {"defaults": {}, "overrides": []})}},
    }}


def _flat_grid_layout(panels):
    # A plain (non-rows) v2 GridLayout built straight from each panel's own
    # gridPos. Used for boards with no UI-captured layout.json yet — once one
    # is arranged in the Grafana UI and pulled back, switch that board over to
    # _load_layout() instead (see dashboards/layout.json for the Recent Games one).
    items = [{"kind": "GridLayoutItem", "spec": {
        "x": p["gridPos"]["x"], "y": p["gridPos"]["y"],
        "width": p["gridPos"]["w"], "height": p["gridPos"]["h"],
        "element": {"kind": "ElementReference", "name": f"panel-{p['id']}"}}}
        for p in panels]
    return {"kind": "GridLayout", "spec": {"items": items}}


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
        # GridLayout (unlike AutoGridLayout) does NOT auto-flow missing items — Grafana
        # defaults an omitted x/y/width/height to 0, so the row renders with real panels
        # at zero size (invisible). Stack them full-width instead so they're actually
        # visible pre-arrangement.
        items = [{"kind": "GridLayoutItem",
                  "spec": {"element": {"kind": "ElementReference", "name": n},
                           "x": 0, "y": i * 8, "width": 24, "height": 8}}
                 for i, n in enumerate(missing)]
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


def to_v2(v1, use_layout_file=True):
    # use_layout_file=False builds a flat GridLayout straight from the panels'
    # own gridPos instead of pulling dashboards/layout.json (which only holds
    # the Recent Games arrangement) — for a board with no UI-captured layout yet.
    cursor = {0: "Off", 1: "Crosshair", 2: "Tooltip"}.get(v1.get("graphTooltip", 0), "Off")
    layout = (_load_layout([f"panel-{p['id']}" for p in v1["panels"]]) if use_layout_file
              else _flat_grid_layout(v1["panels"]))
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
            "layout": layout,
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

    live_engine_v1 = build_live_engine()
    live_engine = to_v2(live_engine_v1, use_layout_file=False)  # no layout.json yet — flat grid
    LIVE_ENGINE.write_text(json.dumps(live_engine, indent=2) + "\n")
    print(f"Generated {LIVE_ENGINE.name} (v2 schema): "
          f"{len(live_engine['spec']['elements'])} panels, flat GridLayout (not yet arranged in UI).")

if __name__ == "__main__":
    main()
