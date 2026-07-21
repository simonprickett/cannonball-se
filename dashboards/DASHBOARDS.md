# Cannonball-SE Grafana Dashboards

Importable Grafana 13 dashboards built on the structured logs described in
[`../ADD_LOGS.md`](../ADD_LOGS.md). Panels query **Loki** with LogQL; the game picker on the
Recent Games board queries **Tempo**. On import you're prompted for the data source(s).

| File | UID | Purpose |
|------|-----|---------|
| `live_game_dashboard.json` | `cannonball-now-playing` | **Now Playing** — hands-off, auto-follows the current/most-recent game (no picker). Identity/state/stage/screenshots are exact at any range; aggregations are a rolling time-window (keep the range short). |
| `recent_games_dashboard.json` | `cannonball-recent-games` | **Recent Games** — pick any recent game from the Game dropdown (Tempo, newest first). Every panel is session-scoped, so exact at any range. Does NOT auto-follow. |
| `aggregate_dashboard.json` | `cannonball-aggregate` | Aggregate stats across all games in the selected time range |
| `leaderboards_dashboard.json` | `cannonball-leaderboards` | Per-run "Hall of Fame" leaderboards |

**`recent_games_dashboard.json` is generated, not hand-edited.** `live_game_dashboard.json` is the
source of truth; `python3 generate.py` derives the Recent Games board from it (same layout/panels/viz,
with session-scoped queries + the Tempo `Game` picker). Edit the live board, then regenerate.

## Required panel plugins

Both must be installed (declared in each dashboard's `__requires`):

- **`grafana-graphviz-panel`** — renders the OutRun route map (Now Playing, Recent Games, aggregate).
- **`marcusolsson-dynamictext-panel`** (Business/Dynamic Text) — renders the base64 screenshots (Now Playing, Recent Games).

## Core conventions

- **A session == one `trace_id`.** It is emitted on every in-game log and doubles as the
  session id. `player_initials` is attached to every in-game log too.
- **Two ways to scope to one game:** the **Now Playing** board auto-follows the latest game via
  `last_over_time`/newest + an `start_epoch_ms > end_epoch_ms` state check (no variable, so it
  can't be pinned and always tracks the current game — but its *aggregation* panels are a rolling
  window, not session-scoped). The **Recent Games** board uses a `$session` picker — a **Tempo**
  query variable `label_values(session_label)` sorted descending (newest first, default), and every
  Loki panel filters `| session_label="$session"`, exact at any range. (Why Tempo: Loki can't
  enumerate structured metadata in a variable; Tempo enumerates the `session_label` **span**
  attribute. And a query variable keeps its selection across refreshes, which is why the picker
  board can't auto-follow — hence the separate Now Playing board.)
- **`game.session.end` is logged *before* the session span is closed** (`outrun.cpp`), so it
  retains `trace_id`. Every per-session query depends on this.
- **No `| json` in queries.** The log *body* is just the event name; all attributes
  (`player_initials`, `trace_id`, `stage_id`, `final_score`, `longest_clean_seconds`,
  `screenshot_jpg`, …) arrive as **OTel structured metadata** and are queried directly as
  labels (`| event="…"`, `| unwrap final_score`, `sum by (stage_id)`). Adding `| json` would
  try to parse the plain-text body and stamp every line with `__error__=JSONParserErr`.
- **Time-range aware.** Aggregate and leaderboard panels use `$__range` / `$__auto`, so the
  numbers follow the time picker.

## Event → panel mapping

| Event | Key attributes | Used for |
|-------|----------------|----------|
| `game.session.start` | `game_mode`, `player_initials`, `music_selection` | "Newest game" identity; games-played counts |
| `game.session.end` | `completion_status`, `final_score`, `final_stage`, `longest_clean_seconds`, `screenshot_jpg` | Session state/result, score & clean-streak leaderboards, game-over screenshot |
| `game.stage.start` | `stage_number`, `stage_id`, `score_start`, `speed_kph` | Route map (`stage_id`), stage reached |
| `game.stage.end` | `stage_number`, `score_end`, `time_remaining_seconds`, `screenshot_jpg` | Score progression, checkpoint screenshots |
| `game.map_screen` | `screenshot_jpg` | Course-map screenshot |
| `game.crash` | `crash_type`, `speed_kph`, `stage_number` | Crashes by type, fastest crash, clean-streak reset |
| `game.off_road` | `speed_kph`, `stage_number` | Off-road counts, clean-streak reset |
| `game.vehicle_overtake` | `vehicle_type`, `speed_kph` | Overtakes by type / leaderboard |
| `game.route_chosen` | `direction`, `stage` | (reference; route is reconstructed from `stage_id`) |
| `game.high_score` | `position`, `initials`, `score` | Aggregate high-score table |

## The route map (`stage_id`)

`game.stage.start` carries `stage_id` = the engine's `stage_lookup_off`, the canonical id of
each of the 15 OutRun sections. The Graphviz DOT node ids are these values. Layout: one column
per stage, **top = highest id = most-left turns** (left increments `stage_lookup_off`).

| Stage | Top ↑ (most left) → Bottom ↓ (most right) |
|-------|-------------------------------------------|
| 1 | Coconut Beach `0` |
| 2 | Gateway `9` · Devil's Canyon `8` |
| 3 | Desert `18` · Alps `17` · Cloudy Mountain `16` |
| 4 | Wilderness `27` · Old Capital `26` · Wheat Field `25` · Seaside Town `24` |
| 5 | Vineyard `36` · Death Valley `35` · Desolation Hill `34` · Autobahn `33` · Lakeside `32` |

The ordered sequence of `stage_id` for a session is the route taken. Panel counts are matched to
DOT nodes by id (`${visits}` injection). To colour the taken path distinctly, add a Node
override in the panel keyed on the injected value.

## Screenshots

Screenshots are base64 JPEG in the `screenshot_jpg` **structured-metadata** attribute. Since
structured metadata isn't a table column, each screenshot panel surfaces it into a field:
`… | line_format "{{.screenshot_jpg}}"` → then `organize` renames `Line` → `screenshot_jpg` and
`filterFieldsByName` keeps only it. A **Dynamic Text** panel (`everyRow: true`) then renders it:
```
<img src="data:image/jpeg;base64,{{{screenshot_jpg}}}" style="width:100%"/>
```
Note the **triple braces** — `{{{ }}}` outputs raw HTML so the base64 `=` padding isn't
HTML-escaped (double braces corrupt it). (We tried `dalvany-image-panel` first — it sanitizes the
`src` and rejects `data:` URLs, dumping the base64 into `alt`; and the native table Image cell
renders but caps images to a tiny row height. Dynamic Text is the only one that renders at full
size.) Watch Loki's max line size (default 256 KB) if `capture_screenshot_base64()` ever grabs
full-resolution frames.

## Known caveats

- **The `$session` picker is backed by Tempo, not Loki — deliberately.** Loki `label_values`
  only enumerates *indexed stream labels*; our session keys (`session_label`, `trace_id`) are
  *structured metadata*, which it can't list (an empty dropdown). Loki variables also can't run
  a metric query, so there's no way to rank/pick the newest there. Tempo, however, exposes
  `session_label` as a searchable **span** attribute (set in `TelemetryManager::start_game_session`),
  so a Tempo `label_values` variable lists them; because `session_label` is time-sortable,
  sorting descending makes the newest game the default. The Loki panels then filter by the same
  `session_label` string. Net: Tempo enumerates + ranks, Loki filters — a cross-datasource
  variable. (`trace_id` couldn't do this — random hex can't sort by time.)
- **Average speed is sampled**, not a true whole-lap mean — `speed_kph` is only logged on
  crash / off-road / stage-start / route / overtake events.
- **"Fewest X" leaderboards are intentionally absent.** A run with zero crashes/off-road
  produces no series in LogQL (absent ≠ zero), so `bottomk` would silently skip the perfect
  runs. Only "most/worst" boards are included.
- **`longest_clean_seconds` is wall-clock.** The game does not allow pausing, so this equals
  in-game driving time between incidents.
- **Leaderboards are per-run** (`trace_id` + `player_initials`). `player_initials` is not a
  unique identity — the same initials from different people are distinct runs, not merged.
