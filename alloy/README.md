# Grafana Alloy for CannonBall-SE

Alloy runs on the Raspberry Pi next to the game. The game sends its OpenTelemetry data to
Alloy on localhost, and Alloy forwards it to Grafana Cloud.

## Purpose

This is the first step towards capturing a day of game logs and replaying them into another
Grafana stack (see [`../LOG_REPLAY_PLAN.md`](../LOG_REPLAY_PLAN.md)). Alloy can also later export
system data from the Pi.

**Phase 1 (this document): pass-through.** Alloy forwards **traces and logs** to Grafana Cloud
unchanged. The aims are to verify the data still flows end to end, and to check Alloy does not hurt
game performance on the Pi.

```
CannonBall-SE --OTLP/HTTP--> Alloy (127.0.0.1:4318) --OTLP/HTTP + auth--> Grafana Cloud
```

A side benefit is that the Grafana Cloud credentials live in Alloy's environment file, not in the
game's `config.xml`.

## Files

| File | Purpose |
|------|---------|
| `config.alloy` | Alloy pipeline: OTLP receiver -> batch -> Grafana Cloud OTLP gateway |
| `alloy.env.example` | Template for `/etc/default/alloy` (endpoint, instance ID, token) |

## Prerequisites

- Raspberry Pi running 64-bit Raspberry Pi OS (Debian). See [`../RASPBERRY_PI_BUILD.md`](../RASPBERRY_PI_BUILD.md).
- The game already sends telemetry to your Grafana Cloud stack directly.
- A Grafana Cloud **Access Policy token** with the `traces:write` and `logs:write` scopes. The one the game
  uses today works. Reuse it, or create a dedicated one.
- Your stack's OTLP gateway URL and OTLP instance ID (the values currently in the game's `config.xml`).

## 1. Install Alloy

Alloy is installed from Grafana's apt repository, which supports arm64.

```bash
sudo apt-get install -y gpg wget
sudo mkdir -p /etc/apt/keyrings
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install -y alloy
alloy --version
```

The package installs a systemd service called `alloy`, its config at `/etc/alloy/config.alloy`, and
its environment file at `/etc/default/alloy`. The service runs as the `alloy` user.

## 2. Configure Alloy

Copy the pipeline from this repo:

```bash
sudo cp alloy/config.alloy /etc/alloy/config.alloy
```

Edit `/etc/default/alloy`. Keep the existing `CONFIG_FILE` and `CUSTOM_ARGS` lines and add the three
`GCLOUD_*` values (see `alloy.env.example`):

```bash
sudo nano /etc/default/alloy
```

```
GCLOUD_OTLP_ENDPOINT="https://otlp-gateway-prod-<region>.grafana.net/otlp"
GCLOUD_INSTANCE_ID="<instance id>"
GCLOUD_TOKEN="glc_<token>"
```

- `GCLOUD_OTLP_ENDPOINT` is the base URL and stops at `/otlp`, with **no `/v1`** on the end.

> **The two endpoint settings differ on purpose.** Alloy's OTLP HTTP exporter appends `/v1/traces` and
> `/v1/logs` itself, so its endpoint ends `/otlp`. The game's `config.xml` `<otlp_endpoint>` ends `/v1`
> (the game appends `/traces` and `/logs`). Copying the game's URL into Alloy sends requests to
> `…/otlp/v1/v1/traces` and fails with a 404.
>
> | Setting | Example |
> |---------|---------|
> | `/etc/default/alloy` `GCLOUD_OTLP_ENDPOINT` (Alloy → Cloud) | `https://otlp-gateway-prod-<region>.grafana.net/otlp` |
> | game `config.xml` `<otlp_endpoint>` pointed straight at Cloud | `https://otlp-gateway-prod-<region>.grafana.net/otlp/v1` |
> | game `config.xml` `<otlp_endpoint>` pointed at Alloy | `http://127.0.0.1:4318/v1` |
- Restrict the file, since it holds the token: `sudo chmod 640 /etc/default/alloy` and
  `sudo chown root:alloy /etc/default/alloy`.

Check the config, then start the service:

```bash
alloy fmt /etc/alloy/config.alloy > /dev/null   # syntax check
sudo systemctl enable --now alloy
sudo systemctl restart alloy
systemctl status alloy
journalctl -u alloy -n 50 --no-pager
```

## 3. Point the game at Alloy

In the game's `config.xml`, change the `<telemetry>` block:

```xml
<telemetry>
    <otlp_endpoint>http://127.0.0.1:4318/v1</otlp_endpoint>
    <instance_id></instance_id>
    <auth_token></auth_token>
    ...
</telemetry>
```

- The game appends `/traces` and `/logs` to this base URL, so one endpoint covers both signals.
- Leave `instance_id` and `auth_token` empty. With no token the game sends no `Authorization` header, and
  Alloy adds the credentials. Sending them to Alloy is harmless, but there is no need.
- Restart the game.

## 4. Verify the data flows

1. Play a game, then watch it arrive:
   - Alloy's own counters: `curl -s http://127.0.0.1:12345/metrics | grep -E 'otelcol_(receiver_accepted|exporter_sent)'`.
     Both traces (`spans`) and logs (`log_records`) counters should rise after a game, and
     `otelcol_exporter_send_failed_*` should stay at zero.
   - Alloy's UI is at <http://127.0.0.1:12345> (reach it with `ssh -L 12345:127.0.0.1:12345 <pi>`).
2. In Grafana Cloud, open the dashboards (Now Playing, Recent Games) and Explore -> Tempo / Loki. The new game's logs and trace should
   appear exactly as before.
3. If nothing arrives:
   - `journalctl -u alloy -f` for export errors. A 401 means a wrong instance ID or token, a 404 usually means the wrong region or a
     `/v1` (or `/v1/...`) suffix wrongly left on `GCLOUD_OTLP_ENDPOINT`.
   - Run the game with `<debug>1</debug>` and check its console output for export errors to `127.0.0.1:4318`.

## 5. Check performance impact

Compare a game with and without Alloy on the same Pi.

```bash
# Alloy's CPU and memory while a game is running
top -b -n 1 -p "$(pgrep -x alloy)"
systemctl status alloy | grep -E 'Memory|CPU'

# Pi health during play: throttling flags (0x0 = fine) and temperature
vcgencmd get_throttled
vcgencmd measure_temp
```

1. Baseline: stop Alloy (`sudo systemctl stop alloy`), point `config.xml` straight at Grafana Cloud as before, play a full game and note
   the frame rate / smoothness, `vcgencmd get_throttled` and temperature.
2. With Alloy: restore the local endpoint, start Alloy, play the same game, and compare.

Because Alloy is a local hop on the loopback interface and only handles a few events per second, it should add very little CPU and memory.
Record what you actually see.

## Starting and stopping Alloy

Alloy runs as the systemd service `alloy`.

| Task | Command |
|------|---------|
| Start now | `sudo systemctl start alloy` |
| Stop now | `sudo systemctl stop alloy` |
| Restart (needed after editing `/etc/alloy/config.alloy` or `/etc/default/alloy`) | `sudo systemctl restart alloy` |
| Start automatically at every boot | `sudo systemctl enable alloy` |
| Stop starting at boot | `sudo systemctl disable alloy` |
| Enable at boot **and** start now | `sudo systemctl enable --now alloy` |
| Is it running? | `systemctl is-active alloy` (`active`) |
| Will it start at boot? | `systemctl is-enabled alloy` (`enabled`) |
| Full status | `systemctl status alloy --no-pager` |
| Logs | `journalctl -u alloy -n 50 --no-pager` (add `-f` to follow, `-b` for this boot only) |

Alloy reads `/etc/alloy/config.alloy` and `/etc/default/alloy` only when it starts, so always restart it
after changing either file.

## Starting at boot

The `alloy` package installs a systemd service. `sudo systemctl enable --now alloy` (step 2) makes it
start at every boot as well as now. Check with:

```bash
systemctl is-enabled alloy     # should print "enabled"
systemctl is-active alloy      # "active" once running
```

If it prints `disabled`, run `sudo systemctl enable alloy`.

Startup order matters a little. If the game starts before Alloy is listening on `127.0.0.1:4318`, its first
export attempts fail and those batches may be lost (the game does not queue them for long). Alloy starts
quickly and the game takes a while to reach a game session, so this is unlikely to matter in practice, but
if you see missing events right after a reboot, check `journalctl -u alloy -b` and when the game launched
relative to it. To make the order explicit, start the game's service `After=alloy.service`.

## Status

Verified on a Pi 4 (Raspberry Pi OS, 64-bit) with Alloy v1.21.3: logs and traces flow game -> Alloy -> Grafana Cloud
with accepted == sent counts and no failures, and the game appears correctly in the Recent Games dashboard. The
performance comparison (section 5) has not been recorded yet.

## Rolling back

Stop Alloy and put the original `<otlp_endpoint>`, `<instance_id>` and `<auth_token>` back in `config.xml`:

```bash
sudo systemctl disable --now alloy
```

## Next steps

Once pass-through is verified: add a switchable file sink for logs (capture), then the replay script. See
[`../LOG_REPLAY_PLAN.md`](../LOG_REPLAY_PLAN.md).
