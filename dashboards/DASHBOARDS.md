# Cannonball-SE Grafana Dashboards

Three importable Grafana 13 dashboards built on the structured logs described in
[`ADD_LOGS.md`](ADD_LOGS.md). All panels query **Loki** with LogQL; on import you are
prompted for the Loki data source (`DS_LOKI`).

| File | UID | Purpose |
|------|-----|---------|
| `last_game_dashboard.json` | `cannonball-last-game` | Deep dive on the most recent game (in progress or just finished) |
| `aggregate_dashboard.json` | `cannonball-aggregate` | Aggregate stats across all games in the selected time range |
| `leaderboards_dashboard.json` | `cannonball-leaderboards` | Per-run "Hall of Fame" leaderboards |

## Required panel plugins

Both must be installed (declared in each dashboard's `__requires`):

- **`grafana-graphviz-panel`** — renders the OutRun route map (last-game + aggregate).
- **`dalvany-image-panel`** — renders the base64 screenshots (last-game only).

## Core conventions

- **A session == one `trace_id`.** It is emitted on every in-game log and doubles as the
  session id. `player_initials` is attached to every in-game log too.
- **The last-game dashboard is driven by a `$session` picker that defaults to the newest game.**
  The picker is a **Tempo** query variable — `label_values(session_label)` sorted descending — so
  the time-sortable `session_label` puts the latest game first and selects it by default. Every
  Loki panel then filters `| session_label="$session"`, so it's exact at any time range. (Why
  Tempo: Loki can't enumerate structured metadata in a variable, but Tempo enumerates the
  `session_label` **span** attribute — see the caveats.)
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

Screenshots are base64 JPEG in the `screenshot_jpg` attribute. The Dynamic Image panel builds a
data URL by concatenation: **Base URL** `data:image/jpeg;base64,` + **field** `screenshot_jpg`.
Watch Loki's max line size (default 256 KB) if `capture_screenshot_base64()` ever grabs
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
