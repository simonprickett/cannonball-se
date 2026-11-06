# Log capture and replay plan

Status: **Phase 1 done (Alloy pass-through); capture and replay not started.** Paused here on 2026-10-30.
The MQTT bridge and receipt printer idea was planned on 2026-11-06 (last sections of this file); nothing implemented.

### Resume here (2026-11-06)

Planning for the **game-end receipt printer** is complete enough to start building. Summary of what is decided:
Alloy bridge branch (game.session.end + game.high_score only, screenshot stripped) -> Python bridge (pairs the two events,
drops zero-score games) -> Mosquitto on the Pi (host/port/credentials configurable) -> Python printer subscriber on the
Pi -> CSN-A2 on the GPIO UART (9600 8N1, 58 mm, 5 V 2 A, Pi TX to printer RX only). Receipt for every non-zero game,
32 columns, no high-score line, QR later once data is on Grafana Play with anonymous access.

State of the hardware: **printer parts are on order, so no printer testing has been possible yet.** Nothing in this plan
has been run on the Pi. The Alloy performance comparison and `otelcol.exporter.file` check from Phase 1 are still open too.

First actions when we resume: (1) check `otelcol.exporter.file` exists in Alloy v1.21.3 and capture a few real games to
settle the open decisions below, (2) write the bridge, (3) add the Alloy branch, (4) printer subscriber and hardware test.
See "Suggested order" at the end. The MQTT and receipt sections start at "Idea: fan select events out to MQTT or Redis".

### Progress

- [x] Alloy v1.21.3 (linux/arm64) installed on the Pi 4 (`redpi4`) from Grafana's apt repo. Install and
      config steps are in [`alloy/README.md`](alloy/README.md).
- [x] Pass-through pipeline (`alloy/config.alloy`): game -> Alloy (`127.0.0.1:4318`) -> Grafana Cloud, **traces and logs**.
- [x] Verified end to end with a real game: Alloy counters showed accepted == sent for logs (89) and spans (7)
      with no send failures; the game showed correctly in Recent Games, and its trace was present in Tempo.
- [x] Game export intervals made configurable in `config.xml` (`trace_export_interval_ms`, `log_export_interval_ms`).
      Logs now ship at 250 ms by default (tested with no visible impact on a Pi 4); traces keep the SDK default of 5000 ms.
- [x] Game's `otlp_endpoint` now ends `/v1` (the code appends `/traces` and `/logs`). Alloy's own endpoint
      ends `/otlp` with no `/v1`. The difference is documented in `alloy/README.md`.
- [ ] Still to do for Phase 1: the performance comparison (Alloy CPU/memory, `vcgencmd get_throttled`,
      temperature, with and without Alloy), and a look at Now Playing during a live game.
- [ ] Capture sink, replay script, rolling-date job, play dashboard variant (everything below).

## Goal

Keep a static dataset of CannonBall-SE game logs (one representative day) that can be reloaded
into Loki on the Grafana play stack whenever its data expires, so a dashboard on play always
shows a recent day of games.

- Logs only. Traces are out of scope.
- No changes to the game code.
- The date may roll: the replay shifts the capture to a different day but keeps the time of day.
- Play stack log retention: **30 days**.

## Approach

```
game --OTLP--> Alloy (on the Pi) --+--> Grafana Cloud (live, as today)
                                   +--> otelcol.exporter.file (capture, switchable)

capture file --> replay script (shift timestamps) --> OTLP gateway on the play stack
```

Alloy runs permanently on the Pi. It may also carry Pi system data later.

Rejected: exporting with `gcx`/LogQL and re-importing. It flattens OTLP structure (resource
attributes, structured metadata) and hits query limits, so the replayed data would not match live data.

## Part 1: Capture (Alloy)

- Point the game's `otlp_endpoint` in `config.xml` at the local Alloy
  (`http://localhost:4318/v1`). This is config only. The game appends `/traces` and `/logs`, so
  the receiver must accept both paths.
- `otelcol.receiver.otlp` fans **logs only** out to:
  - `otelcol.exporter.otlphttp` (Grafana Cloud, current behaviour)
  - `otelcol.exporter.file` (capture)
- Traces are not wired to any exporter, so they are dropped.
- File exporter settings to use: JSON format (one record per line), `zstd` compression if
  screenshots make it large, `rotation` if left on. `append` and `rotation` cannot be combined.
- Keep the capture sink switchable (separate config file or a toggle) so it is only on when
  recording a day.

### Things to verify first

- [ ] Alloy version on the Pi includes `otelcol.exporter.file`. Docs mark it **public preview**.
- [ ] Read the reference page for that exact version (examples differ between versions; a `format`
      attribute appears in some but not in the argument tables).
- [ ] Each captured line is a complete OTLP logs export request (`resourceLogs` ->
      `scopeLogs` -> `logRecords`) that can be re-POSTed as is.
- [ ] Size of one day's capture (screenshots in log lines?).
- [ ] Check the capture for player initials, hostnames or anything else not fit for a public stack.

## Part 2: Replay script

`replay_logs.py <capture> --date YYYY-MM-DD` (name and location TBD).

- Shift by a whole number of days: `target_date - capture_date`. Time of day is preserved.
- Fields to shift:
  - `timeUnixNano` and `observedTimeUnixNano`
  - absolute epoch fields in event bodies/attributes, e.g. `start_epoch_ms`, `end_epoch_ms`.
    Dashboards compute durations from these, and the picker may rely on them.
  - any `session_label` that embeds a date (check how it is built before deciding).
- POST to the play stack's OTLP gateway `/v1/logs` in paced batches.
- Auth: Basic `instance_id:token`, token from a Cloud Access Policy with **`logs:write`** only.
  The OTLP instance ID comes from the stack's OpenTelemetry connection page, and the endpoint
  region must match the stack.
- Idempotency: Loki ignores entries with identical stream, timestamp and line. Confirm on the play
  stack that re-running the same target date does not duplicate.
- Credentials come from env vars, never committed.

## Part 3: Rolling date

- Replay onto **yesterday**, not today. Entries more than a few minutes in the future are
  rejected, so a full day cannot be replayed onto today.
- Loki also rejects entries older than its age window (about a week by default), so never replay
  onto a date older than that.
- Run daily (cron or a scheduled GitHub Action). 30-day retention means a daily run keeps a
  complete recent day available, and it also reloads the data after it expires.
- Play dashboard default time range: yesterday (`now-1d/d` to `now-1d/d`).

## Part 4: Play dashboard variant

- Generate a variant from `dashboards/generate.py` with the "View traces and logs" panel removed
  (no Tempo data to link to).
- Push with `dashboards/push.sh` to the play stack. The play stack needs a Loki datasource whose
  display name matches the one in the dashboard (override as described in `dashboards/DASHBOARDS.md`).
- Dashboards require Grafana 13.3+ (ordinal unit). Confirm the play stack is on 13.3.
- Pushing needs a stack service account token (`dashboards:read` + `dashboards:write`, plus
  `dashboards:create` and `folders:read` for the first push).

## Suggested order

1. ~~Install Alloy and verify the pass-through.~~ Done. Next: check the installed Alloy (v1.21.3) includes
   `otelcol.exporter.file`, and read the reference page for that version.
2. Add the file sink to an Alloy config, run on the Pi for about an hour, inspect the output.
3. Write the replay script against the real capture; test against a non-play stack first.
4. Record a full representative day. Review it for sensitive content and size, then store it
   (bucket or LFS rather than plain git, depending on size).
5. Build the play dashboard variant and push it.
6. Schedule the daily replay.

## Open questions

- Which stack tests the replay before play?
- Where does the static capture live, and who can access it?
- How are `session_label` and the picker affected by shifted timestamps?
- Is Alloy also going to run the Pi system-data pipeline? If so, keep it in the same config.

## Dashboard follow-ups (not part of the replay work itself)

- Update the dashboards' bar charts to use the new bar chart PR so the series name is shown when
  there is only one metric. Noted only; no dashboard changes made yet.

## Idea: fan select events out to MQTT or Redis (direction chosen 2026-11-06, no implementation)

> **Direction chosen:** MQTT, via a Python bridge, game-end events only, feeding a thermal receipt printer.
> See [Game-end receipts](#game-end-receipts-via-mqtt-decided-direction-not-implemented) at the end of this
> file. Nothing below or there is built yet.

Goal: have Alloy emit chosen game events (e.g. crashes, overtakes, game start/end) to an MQTT broker or
Redis, without changing the C++ code. Separate from the capture/replay work, but it would reuse the same
fan-out in the Alloy pipeline.

What the Alloy docs list (checked against the `otelcol` component index, not against the installed
v1.21.3 build): the `otelcol.exporter.*` set includes `kafka`, `googlecloudpubsub`, `awss3`, `syslog`,
`file`, `loki`, `otlp`, `otlphttp` and others, but **no native MQTT or Redis exporter**. So there are
two routes:

1. **Native message bus.** Select events in Alloy and publish to **Kafka** (or Pub/Sub) with the existing
   exporter. Only useful if Kafka is acceptable as the broker, or a Kafka-to-MQTT/Redis bridge is wanted.
2. **Small bridge (most likely).** Alloy sends the selected events over OTLP/HTTP to a local process
   (a ~30-line script, or Node-RED / Redpanda Connect / similar) that publishes them to MQTT or Redis.
   Same shape as the capture-sink fallback in Part 1.

Selecting events in Alloy, either way:
- Filter with `otelcol.processor.filter` (OTTL conditions on the log's `event` attribute, e.g. keep only
  `game.crash` and `game.vehicle_overtake`), or
- Split into per-event pipelines with `otelcol.connector.routing`, so the Grafana Cloud pipeline still gets
  everything while the broker pipeline gets only the selected events.

Things to decide or check before doing it:
- Which events, and the payload shape the consumer wants (OTLP JSON vs a flat JSON message). A bridge can
  flatten it; Alloy cannot.
- Whether MQTT/Redis components exist in Alloy's newer builds (re-check the component list for v1.21.3).
- Delivery: a slow or down broker must not back-pressure the Cloud pipeline. Use a separate batch
  processor and queue for the broker branch.
- Where the broker lives (on the Pi or elsewhere on the network) and how it authenticates.

### If we write a bridge (Python, using only what Alloy provides natively)

Alloy has no MQTT or Redis exporter, and the Pub/Sub exporter is a community component that needs a
Google Cloud project, so the working assumption is a small **Python bridge** fed over OTLP/HTTP. Not built
yet, and the Alloy argument names below are from memory, so check them against the reference page for the
installed Alloy version.

Protocol, Alloy -> bridge:
- `otelcol.exporter.otlphttp "bridge"` does `POST /v1/logs` to a local port (e.g. `http://127.0.0.1:9000/v1/logs`).
- Ask for JSON, uncompressed, so the bridge needs no protobuf or gzip handling: `encoding = "json"` on the
  exporter and `compression = "none"` in its `client` block. (Default is protobuf + gzip.)
- The bridge replies `200` with `{}`. `429`/`5xx` make Alloy retry from its queue; a `400` is treated as
  permanent and the batch is dropped.

Body shape (standard OTLP/JSON):
```
resourceLogs[] -> resource.attributes[] (service.name, host.name)
  scopeLogs[] -> logRecords[]: timeUnixNano, severityText, body, attributes[]  (event = "game.crash" ...)
```
- Attributes are a list of `{key, value:{stringValue|intValue|doubleValue...}}`, not a flat object. Flatten them.
- int64 values arrive as JSON strings.

Bridge job: for each log record, read `event`, flatten attributes to a plain JSON message, publish it:
- MQTT: topic like `cannonball/<host>/<event>` (e.g. `paho-mqtt`).
- Redis: `PUBLISH`, or `XADD` to a stream if history is wanted (e.g. `redis-py`).

Pipeline, as a branch beside the existing Cloud branch:
```
otelcol.receiver.otlp -> otelcol.processor.filter -> otelcol.processor.batch (own, short timeout)
                      -> otelcol.exporter.otlphttp "bridge"
```
The bridge branch gets its own batch processor and sending queue so a stalled bridge cannot hold up Cloud.

Python notes:
- Stdlib `http.server` (or `ThreadingHTTPServer`) is enough for the receiving side; only the MQTT/Redis client is a dependency.
- Run it as a systemd unit on the Pi, `After=` the broker and before/alongside Alloy, restarting on failure.
- Keep it stateless and idempotent where possible; log and skip bad records rather than returning a non-2xx that would trigger retries of a poisoned batch.

Synergy: the same receiving code could double as the **capture sink** for the replay plan (it sees exactly
the logs we would want to record), if we do not use `otelcol.exporter.file`.

Alternatives considered: `otelcol.exporter.syslog` (simpler to parse, but flat and untyped) and
`otelcol.exporter.file` plus a tailing bridge (no network protocol, but not immediate and needs rotation handling).

### Filtering events for the bridge (noted only, not implemented)

Only a subset of game events should reach the bridge. Do this in Alloy, in the bridge branch only, with
`otelcol.processor.filter`.

**The filter processor drops records that match its condition**, so to keep a few events the condition is the
inverse:

```alloy
otelcol.processor.filter "bridge_events" {
  error_mode = "ignore"
  logs {
    log_record = [
      `attributes["event"] != "game.crash" and attributes["event"] != "game.vehicle_overtake"`,
    ]
  }
  output {
    logs = [otelcol.processor.batch.bridge.input]
  }
}
```

This keeps only `game.crash` and `game.vehicle_overtake` for the bridge. Everything else is dropped from that
branch only.

- **Cloud branch unaffected (to confirm).** The receiver feeds both branches and the collector gives each its own
  copy when one of them modifies the data, so the Cloud branch should still get everything. Confirm on the Pi that
  the Cloud log counts do not drop.
- **Is `event` an attribute?** Judging by the Loki labels it looks like a log record attribute, which is what the
  condition assumes. Not checked against the raw OTLP. The first capture will settle it.
- **Longer keep-lists.** Use OTTL `IsMatch`, e.g. `IsMatch(attributes["event"], "^game\\.(crash|vehicle_overtake|session\\.end)$")`,
  instead of chained `and` conditions.
- **Routing instead.** `otelcol.connector.routing` is only worth it if different events go to different
  destinations. For a single bridge, the filter is simpler, and the bridge can pick the MQTT topic from `event`.
- **Decided:** the subset is the game-end events only. See the receipts section below.

## Game-end receipts via MQTT (decided direction, not implemented)

Decided 2026-11-06. This narrows the MQTT idea above to one use: when a game ends, print a small receipt on
an ESC/POS thermal printer. Still planning only; no code, config or packages have been added.

### Shape

**Language:** the bridge and the printer subscriber are both planned in **Python 3** (stdlib `http.server` for the bridge's
OTLP receiver, `paho-mqtt`, `python-escpos`, `pyserial`, `Pillow`), each as its own script run by systemd from a venv.
Python is the user's preference (confirmed 2026-11-06) because it avoids installing another language runtime on the Pi.

```
game -> Alloy -> Grafana Cloud                       (unchanged, gets everything)
          |
          +-> filter (game-end only) -> bridge (Python) -> MQTT broker -> receipt printer (subscriber)
```

Four pieces, each independently movable:

1. **Alloy bridge branch.** `otelcol.processor.filter` keeps only the game-end events, then its own batch
   processor and `otelcol.exporter.otlphttp "bridge"` (JSON, uncompressed), as described above.
2. **Bridge.** Receives OTLP/JSON, flattens each record, publishes one MQTT message per event.
3. **MQTT broker.** Mosquitto, on the Pi to start with.
4. **Printer subscriber.** A separate small process that subscribes, formats a receipt, and drives the
   printer (likely `python-escpos` over USB or serial). It runs wherever the printer is plugged in, which may
   not be the Pi. Keeping it a plain MQTT subscriber is what lets the printer live anywhere.

### Everything on the Pi, but nothing hard-wired to it

All locations are configuration, with `127.0.0.1` defaults, so each piece can move to another host without
code changes:

| Setting | Read by | Default |
|---|---|---|
| Bridge listen host/port | bridge | `127.0.0.1:9000` |
| Bridge endpoint | Alloy (env var, alongside `alloy.env`) | `http://127.0.0.1:9000` |
| MQTT host, port, TLS flag | bridge, printer subscriber | `127.0.0.1:1883` |
| MQTT username/password | bridge, printer subscriber | none (anonymous, localhost only) |
| Topic prefix | bridge, printer subscriber | `cannonball` |
| Printer device (USB ids or serial path), paper width | printer subscriber | to be decided |
| Dashboard base URL (for the QR code) | printer subscriber | to be decided |

Credentials go in env files that are git-ignored, with `*.example` files committed, matching `alloy.env.example`.
If the broker moves off the Pi, only the MQTT host and credentials change.

### Which events

"Minimal game-end events" means the bridge branch keeps only:

- **`game.session.end`**: the final event of a game and the receipt trigger. Carries `final_score`,
  `final_stage`, `end_epoch_ms`, `completion_status`, `completion_code` and `longest_clean_seconds`.
- **`game.high_score`**: carries `initials`, `position` and `score`. Needed only because initials live here. The bridge uses `initials` and ignores `position` and `score`.

Not sent for now: `game.gameover` (fires at the end of play, before the post-game screens, so it is not
the last event), `crash`, `overtake`, `heartbeat` and the rest.

Filter, using the keep-list form noted above:
`IsMatch(attributes["event"], "^game\\.(session\\.end|high_score)$")` (inverted, since the filter drops matches).

### Things found in the code that shape the receipt

These are read from `src/main/engine/outrun.cpp`, not run on the Pi.

1. **Initials exist only for high-score entries.** They are logged on `game.high_score`, not on
   `game.session.end`. In OutRun only players who make the table enter initials. A player who does not
   make it has no initials anywhere in telemetry. The receipt must handle "no initials" (print `---`, or the
   time and score only).
2. **Ordering.** `game.high_score` is logged when the entry timer expires, and `game.session.end` follows in
   the same post-game flow. So when both exist, the high-score event arrives first. The bridge has to pair them
   by game, either by holding a high-score event briefly and attaching it to the session.end that follows, or by
   publishing both and letting the printer subscriber pair them. Holding it in the bridge keeps the printer simple,
   and gives the printer a single "receipt" message per game. A short in-memory map keyed by game ID is enough.
3. **`screenshot_jpg` is on `game.session.end`.** It is a large base64 attribute. It must not go to MQTT. Strip
   it in Alloy (`otelcol.processor.transform` / `delete_key` on the bridge branch only) so it never reaches the
   bridge, rather than relying on the bridge to drop it.
4. **No `game ID` field is logged.** Records carry `trace_id` and `span_id` (the code comments say per-session
   dashboard queries rely on them). The likely game ID is the trace ID, which is 32 hex characters, so
   the receipt would show a short prefix. Not confirmed against the raw OTLP. The first capture will settle it.
5. **Stage and score.** `final_stage` and `final_score` are on `session.end`. Stage name (as opposed to number)
   would need a mapping in the printer subscriber.

### Planned MQTT message

Published by the bridge, one per game, to `cannonball/<host>/game_end`, non-retained, QoS 1:

```json
{
  "game_id": "<trace id>",
  "host": "redpi4",
  "initials": "ABC",          // null when the player did not make the table
  "score": 123456,
  "stage": 4,
  "completion_status": "...",
  "ended_at": "2026-11-06T14:03:21Z"
}
```

Field names are provisional. `ended_at` comes from `end_epoch_ms`, not from the arrival time.

### Receipt content (draft)

Initials, score, game ID (shortened), time the game ended, stage reached, completed or not, and optionally
longest clean run. A QR code at the bottom is to be explored.

### QR code

Encode a link to the Recent Games dashboard scoped to that game. That needs:
- the dashboard's variable for the selected game (not checked, will be in `generate.py` / the dashboard JSON),
- **Grafana Play with anonymous access.** The plan is to get the data onto the play stack, where visitors can open
  dashboards anonymously and still use variables. The QR link points there, not at the private Cloud stack.
  This depends on the replay work (play stack data) and is not started. Since play is public, the initials concern
  in the open decisions applies.
- QR rendering as a bitmap (the printer has no native QR command; see datasheet findings), at a size that scans on
  58 mm paper (384 dots wide). A 9600 baud line sends about 960 bytes/s, so a 200-dot-square QR (about 5 KB) takes
  roughly 5 s. Keep it small.

### Delivery and failure

- Bridge branch has its own queue, so a stopped bridge or broker never blocks the Cloud pipeline.
- Receipts are not worth printing late. Use a short retry, then drop. The printer subscriber should ignore messages
  whose `ended_at` is more than a few minutes old, so a restart does not print a backlog.
- The bridge must be idempotent per game ID, so Alloy retries do not print twice.
- Printer out of paper or offline: the subscriber logs and drops. No retry storm.

### Decisions made (2026-11-06, second pass)

- **Receipt for every game**, not just high scores. So `initials` is null for most receipts, and the receipt layout
  must look fine without them (print `---` or omit the line).
- **Printer: CSN-A2, serial, wired to the Pi's GPIO UART.** So the printer subscriber runs **on the Pi** (the
  device is `/dev/serial0` or similar). The subscriber is still a separate MQTT subscriber, so MQTT host config
  matters if the broker is ever moved off the Pi. The user has the full datasheet.
- **QR code is blocked on Grafana Play.** The link only works once the game data is on the play stack and
  anonymous viewers can open dashboards with variables. Until then the receipt prints without a QR code
  (game ID as text instead). The QR step is last in the order below, and the base URL stays a setting.

### Printer notes (first pass, from general knowledge; **superseded where the datasheet findings below disagree**)

- It is a 58 mm thermal printer, typically 384 dots per line (about 32 characters in the standard font).
- Serial default is commonly 9600 baud, 8N1, but some units ship at 19200. The datasheet and the self-test
  printout will say which.
- It speaks ESC/POS, so `python-escpos` (`escpos.printer.Serial`) should work, including its QR support.
- **Logic levels.** The TTL variant uses its own voltage levels, and the Pi's GPIO is 3.3 V and not 5 V tolerant.
  Check the datasheet for the printer's TX level, and add level shifting (or a resistor divider on the printer-TX
  to Pi-RX line) if it is 5 V. Printer RX from a 3.3 V Pi TX is usually fine.
- **Power.** The printer needs its own supply (datasheet gives voltage and peak current, typically several amps
  at 5-9 V). Do not power it from the Pi. Share ground with the Pi.
- **Pi UART setup.** Enable the hardware UART and disable the serial login console (`raspi-config`, or
  `enable_uart=1` plus removing the console from `cmdline.txt`). The subscriber's user needs the `dialout` group.
  On a Pi 4 `/dev/serial0` maps to the PL011 or mini UART depending on config. Check which, since the mini UART
  clock varies with CPU frequency. Wiring: Pi GPIO14 (TXD) to printer RX, GPIO15 (RXD) to printer TX, plus ground.
- **Unknowns to settle with the datasheet:** baud, logic level, supply, print width in dots, character set
  and any quirks (e.g. needs a delay after a feed, buffer size and whether it uses flow control).
- Keep print jobs short and feed a few lines at the end for tear-off. Cut commands are probably not supported
  (no cutter on most CSN-A2 units).

### Datasheet findings (CSN-A2 User Manual v4.0, 70 pages; read 2026-11-06)

Text was extracted with Ghostscript, so anything that is only in a diagram or image (the wiring picture, the
peak-current figure if it is in a graphic) was not read. The items marked "not found" need a look at the PDF.

- **Print:** thermal line printing, 58 mm paper, **48 mm print width, 384 dots per line, 8 dots/mm (about 203 dpi)**.
  Speed 60 mm/s, max 80 mm/s at 8.5 V. Paper roll max diameter 39 mm.
- **Power:** "DC 5-9 V or 12 V", pin `VH`. Peak current: **not found** in the extracted text. 5 V 2 A is within the
  stated range.
- **Fonts:** ANK 9x17 and 12x24. At 384 dots that is 42 characters per line (9x17) or **32 (12x24)**, plus GBK 24x24.
- **Serial connector (RS232/TTL board):** pin 1 GND, 3 VH, 4 GND, 5 RXD, 6 TXD (pin 2 unused). The pin table does
  not state the logic voltage for the TTL variant. (The separate USB-board TTL header lists +3.3 V, which only
  hints at 3.3 V logic.) Leaving printer TXD unconnected is still the safest wiring. **Baud rate is not
  settable by command** in this manual, and the self-test reports 9600 8N1.
- **No QR code command.** The command set has no `GS ( k`. It does have **raster bit image `GS v 0`** (up to 48
  bytes = 384 dots wide, up to 4095 dots high, normal 203 dpi). So a QR code must be rendered as an image on the Pi,
  which `python-escpos` can do (software QR rendered to a bitmap). 1D barcodes are supported (UPC, EAN, CODE39,
  ITF, CODABAR, CODE93, CODE128) if a barcode of the game ID is ever wanted.
- **Print darkness.** Two controls: `ESC 7 n1 n2 n3` (max heating dots, default 7 = 64 dots; heating time, default
  80 = 800 us; heating interval, default 2) and the simpler **`DC2 # n`**: density = 50% + 5% x n (low 5 bits),
  plus a break time in the top 3 bits. Raising density and heating time darkens print and slows it. A heating time
  that is too short can print blank. Try `DC2 #` first, then heating time, on the 5 V supply.
- **Status.** `GS r 1` reports only "paper near end" (bits 2,3). When the paper has run out the printer goes **offline
  and does not answer**. The status LED also blinks 3 times for no paper, 5 for overheat. Since we are leaving
  printer TX unconnected, a paper-out printer is not detectable from the Pi. Receipts printed with no paper are
  just lost (acceptable by the plan's drop policy).
- **Character set.** Default code page is CP437 (`ESC t 0`). Initials are A-Z and `.` so plain ASCII is fine.
- **Sleep:** `ESC 8` defaults to 0 (never sleeps), so no wake byte is needed.
- **Cutter:** `ESC i` / `GS V` exist only for boards with a cutter. Treat this printer as having none (confirm by
  looking at it). Feed lines for tear-off with `ESC d n`.
- **Other:** `DC2 T` prints a test page; `ESC @` initializes; the thermal head should stay below 65 C in continuous
  printing, which one short receipt per game will not approach.
- **Not found:** receive buffer size, and whether the serial board uses hardware flow control (DTR/DSR) or
  XON/XOFF on the connector. The pinout has no DTR/DSR pin, so assume none and **pace large bitmaps** (send in
  chunks with short pauses) if a QR code ever prints garbled at 9600 baud.

### Printer facts confirmed by the user (2026-11-06)

- Self-test reports **9600 baud, 8N1**. Paper is **58 mm**.
- Power supply on hand is **5 V, 2 A**. The printer is rated for roughly 5-9 V (datasheet to confirm the range).
  Higher voltage generally gives darker, faster printing. 5 V works but prints lighter, which matters most for
  dense output such as a QR code. Plan: start at 5 V, and tune the heat settings (ESC 7: max dots, heat time,
  heat interval) in the subscriber before considering a higher-voltage supply. Never exceed the datasheet maximum.
- **Logic level:** the user read that 3.3 V logic from the Pi is fine (Adafruit docs). The simplest wiring is
  Pi TX (GPIO14) to printer RX plus ground, **leaving printer TX unconnected**, which sidesteps the 5 V-into-3.3 V
  concern. Printer TX is only needed to read paper status. Check the datasheet.

### Settings added by these decisions

| Setting | Read by | Default |
|---|---|---|
| Serial device | printer subscriber | `/dev/serial0` |
| Baud rate | printer subscriber | from datasheet (9600 or 19200) |
| Paper width / dots | printer subscriber | 58 mm / 384 dots |
| QR enabled, dashboard base URL | printer subscriber | off until Grafana Play is ready |

### Decisions made (2026-11-06, third pass)

- **Pairing happens in the bridge.** It holds a `game.high_score` in memory keyed by game ID and merges it into
  the `game.session.end` that follows, publishing one `game_end` message per game. Pending high scores expire after
  a short timeout (about 60 s) if no `session.end` arrives. A bridge restart loses pending ones, which is acceptable.
  If `session.end` ever arrives first (order unconfirmed, see above), hold it a couple of seconds before
  publishing with null initials.
- **Zero-score games get no receipt.** The bridge drops a `session.end` whose `final_score` is 0 (and discards any
  pending high score for that game) and publishes nothing.

### MQTT topics and delivery (draft)

Alloy never talks to MQTT. It only POSTs OTLP to the bridge over HTTP. The bridge is the MQTT publisher, and the
printer subscriber is the MQTT consumer. MQTT pushes to subscribers, so nothing needs to "pull". The topics to
design are between the bridge and its subscribers:

| Topic | Publisher | Payload | Notes |
|---|---|---|---|
| `cannonball/<host>/game_end` | bridge | the paired receipt JSON above | QoS 1, **not retained**. The printer subscribes. |
| `cannonball/<host>/bridge/status` | bridge | `online` / `offline` | Retained, with a Last Will so the broker publishes `offline` if the bridge dies. |
| `cannonball/<host>/printer/status` | printer subscriber | `online` / `offline` (and last print result) | Retained, Last Will. Handy for a dashboard or a "printer is offline" alert. |
| `cannonball/<host>/events/<event>` | bridge | flattened raw event | **Optional, off by default.** For debugging and other consumers. Not needed for receipts. |

- `<host>` is the game host name from the OTLP resource, so several cabinets can share one broker. The printer
  subscribes to `cannonball/<its host>/game_end`, or `cannonball/+/game_end` if one printer serves all.
- **Stale receipts:** set the MQTT 5 message expiry (about 5 minutes) on `game_end` and have the subscriber also
  ignore a `ended_at` older than that. A persistent session with a fixed client ID and QoS 1 lets a briefly restarted
  subscriber catch what it missed, and the expiry stops it printing a backlog. Needs MQTT 5, which Mosquitto and
  `paho-mqtt` 2.x support. Fall back to the `ended_at` check alone if that proves awkward.
- **Duplicates:** QoS 1 can redeliver, so the subscriber remembers the last few printed `game_id`s and skips repeats.
- **Access control (when it leaves localhost):** broker password file and ACLs. The bridge can write `cannonball/#`,
  the printer subscriber can read `cannonball/+/game_end` and write its own status topic.

### Open decisions

- Confirm the Cloud branch counts do not drop once the filter is added.
- Confirm that `event` is a log record attribute in the raw OTLP, and what the game ID should be.
- Receipt content to check in the code: is `final_stage` 0-based and how many stages are there; the real `completion_status`
  and `completion_code` values for the RESULT line; whether a logo is wanted.
- The Pi's timezone, since the receipt prints local time.
- Printer details not found in the datasheet text: peak current (check the 2 A supply is enough) and receive buffer size.
- QR link format. Depends on the play stack's dashboard variable names and anonymous access (see the QR section).
- Should the receipt show the player's initials at all if the stack is public? (Same concern as the replay capture.)

### Receipt layout (draft, 32 characters wide)

Uses the 12x24 font (32 characters across 384 dots). Nothing here has been printed yet, since the printer is not
connected and parts are on order. The example below is sample data, drawn at the real width:

```
================================
           CANNONBALL
          GAME RECEIPT
================================
PLAYER                       ABC
SCORE                  1,234,567
STAGE                          4
RESULT                   TIME UP
--------------------------------
ENDED           2026-11-06 14:03
GAME ID                 3f9a1c2e
--------------------------------
      [ QR CODE - LATER ]
       Scan for your game
--------------------------------
      Thanks for playing!
```

Layout notes:
- **Title** `CANNONBALL` and `GAME RECEIPT`: centred, possibly double-size (`GS ! n`), which halves the characters per
  line to 16. Optional bold with `ESC E`. A logo bitmap is possible (`GS v 0`) but costs print time at 9600 baud.
- **PLAYER** shows initials, or `---` when the player did not make the table (most games).
- **No high-score line.** Dropped 2026-11-06: each host keeps its own high-score table locally, so a table position
  would only mean something on that one machine. The `game.high_score` event is still needed, but only for the initials.
- **SCORE** has thousands separators and is right-aligned. Score comes from `final_score` on `session.end`.
- **STAGE** is `final_stage`. Whether that is 0-based, and how many stages there are, is **not checked** (confirm in the
  code). Possibly print the stage name instead, via a mapping in the printer subscriber.
- **RESULT** is mapped from `completion_status` / `completion_code`, e.g. `COMPLETED` or `TIME UP`. The actual
  values have not been checked and need a lookup in the code.
- **ENDED** is `end_epoch_ms` converted to the Pi's local time (check the Pi's timezone, which matters for the
  receipt). `2026-11-06 14:03` is 16 characters, so it fits next to the label.
- **GAME ID** is the first 8 characters of the game ID (probably the trace ID, unconfirmed).
- **QR block** is empty until the Grafana Play work is done. When it is not enabled, those two lines and their
  divider are left out entirely, not left blank.
- **Tear-off:** end with 3-4 line feeds (`ESC d n`), because there is no cutter. Total height is about 16 lines, so a
  receipt prints in a few seconds without the QR.
- **Optional extras if wanted:** longest clean run (`longest_clean_seconds`), best-of-day rank. Left out of this
  first version, to stay minimal.
- **Fallback widths:** names longer than the space available are cut. Only initials (3 characters) are expected.

### Pi setup instructions to add (planned, nothing written or run yet)

When we implement, these docs need amending. The steps below are a draft from general knowledge, **untested on the Pi**,
and package names and paths must be checked on the real OS.

- **New `bridge/README.md`** (or a section in `alloy/README.md`) covering, in order:
  1. Install Mosquitto: `sudo apt install mosquitto mosquitto-clients`, then `systemctl enable --now mosquitto`.
  2. Broker config in `/etc/mosquitto/conf.d/cannonball.conf`: listen on `127.0.0.1:1883` only by default, and
     `allow_anonymous true` for localhost. For a non-local broker, a password file and ACLs (see the topic section) plus
     a listener on the LAN address.
  3. Check it with `mosquitto_sub -t 'cannonball/#' -v` in one terminal and `mosquitto_pub` in another.
  4. Install Python dependencies in a venv (`paho-mqtt`, `python-escpos`, `pyserial`, probably `Pillow` for the
     QR image).
  5. Bridge and subscriber env files copied from `*.example`, with the settings table above.
  6. systemd units for the bridge and the printer subscriber, `After=mosquitto.service`, `Restart=on-failure`.
  7. Verifying end to end: play a game, `mosquitto_sub` shows a `game_end` message, then the receipt prints.
  8. Rolling back: stop the units and remove the Alloy bridge branch. The Cloud branch is unaffected.
- **`alloy/README.md`:** add the bridge branch (filter, strip `screenshot_jpg`, its own batch/queue, `otlphttp` to the
  bridge endpoint) and the new env var for the bridge endpoint, plus how to disable the branch.
- **`alloy/config.alloy` and `alloy/alloy.env.example`:** the actual branch and the endpoint variable.
- **`RASPBERRY_PI_BUILD.md`:** a short section on the serial printer: wiring (Pi GPIO14 TX to printer RX, ground,
  printer TX left unconnected, separate 5 V 2 A supply on `VH`), enabling the UART and turning off the serial
  console in `raspi-config`, the `dialout` group, and a `printf` test to `/dev/serial0` (e.g. `DC2 T` test page) once
  the parts arrive.
- **Timezone:** the receipt prints local time, so the Pi's timezone should be set (`raspi-config`) and noted in the docs.

### Suggested order (when we start)

1. Capture a few real games with the file sink and inspect the raw OTLP for `event`, trace ID and the ordering
   of `high_score` and `session.end`.
2. Write the bridge, publishing to a local Mosquitto, and check with `mosquitto_sub`.
3. Add the Alloy bridge branch (filter, strip screenshot, own batch/queue), verify Cloud counts are unchanged.
4. Write the printer subscriber against a dummy printer or text output. In parallel, wire up the CSN-A2, enable
   the UART, and print a test page from `python-escpos` using the datasheet's settings.
5. Connect them: real receipts from real games.
5b. Add the QR link once Grafana Play data and anonymous access are in place.
6. systemd units for bridge and subscriber, plus README and `*.example` env files.
