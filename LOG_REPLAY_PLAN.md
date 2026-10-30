# Log capture and replay plan

Status: **plan only, nothing implemented yet.**

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

1. Check Alloy version and exporter availability, read the reference page.
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
