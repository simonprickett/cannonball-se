# Log capture and replay plan

Status: **Phase 1 done (Alloy pass-through); capture and replay not started.** Paused here on 2026-10-30.

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

## Idea: fan select events out to MQTT or Redis (not planned yet, no implementation)

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
- **To decide:** which events make up the subset. `game.heartbeat` should probably stay out.
