# Receipts

Game-end thermal receipts for CannonBall-SE (CSN-A2, 58 mm, 384 dots, ESC/POS).

Planning background: the "Game-end receipts via MQTT" section at the end of
[`../LOG_REPLAY_PLAN.md`](../LOG_REPLAY_PLAN.md).

This folder starts with receipt design on the Mac, using a printer simulator, before anything goes to the Pi.

## Running

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python print_receipt.py fixtures/high_score.json                  # escpresso on localhost:9100
.venv/bin/python print_receipt.py fixtures/high_score.json --sink file:out/x.raw
```

## Previewing in the emulator

[escpresso](https://github.com/jflaflamme/escpresso) is a virtual ESC/POS printer with a GUI preview. It listens on
TCP port 9100, which is this tool's default sink.

```
# One-off install (needs Rust: https://rustup.rs or `brew install rust`)
cargo install escpresso

# Start it; a window opens and it listens on localhost:9100. Choose 58 mm paper in its UI.
escpresso
```

Then, in another terminal, from this folder:

```
.venv/bin/python print_receipt.py fixtures/high_score.json       # initials, timed out
.venv/bin/python print_receipt.py fixtures/no_initials.json      # PLAYER shows ---
.venv/bin/python print_receipt.py fixtures/continuous_long.json  # 9-digit score, stage 15
.venv/bin/python print_receipt.py fixtures/completed.json --logo assets/logo.png --qr --base-url https://play.example.org
```

The last one prints the logo and a QR (the example URL scans but goes nowhere).

The emulator is more permissive than the CSN-A2. It renders commands the printer lacks (for example native QR, `GS ( k`),
and it prints instantly, whereas the real printer takes seconds for the logo and QR at 9600 baud. Stick to the commands
in the printer datasheet and treat the preview as a layout check, not proof the printer will accept every byte.

## Configuration

Environment variables (see `receipts.env.example`; the real `receipts.env` is git-ignored):

| Variable | Default | Meaning |
|---|---|---|
| `RECEIPT_QR_ENABLED` | `false` | Print a QR linking to the game's dashboard page |
| `RECEIPT_DASHBOARD_BASE_URL` | unset | Public Grafana base URL; the QR is skipped (with a warning) if unset |
| `RECEIPT_DASHBOARD_PATH` | `/d/cannonball-recent-games/recent-games` | Dashboard uid and slug |
| `RECEIPT_QR_WINDOW_MINUTES` | `15` | Link time range starts this long before the game ended |
| `RECEIPT_LOGO_PATH` | unset | Logo image (PNG best) printed instead of the title text; falls back to text if unreadable |
| `RECEIPT_LOGO_WIDTH` | `320` | Logo width in dots (max 384). Taller logos print slower: ~1 s per 1 KB at 9600 baud |

`--qr/--no-qr`, `--logo` and `--base-url` on the CLI override the environment. The QR is a raster bitmap (`GS v 0`);
the CSN-A2 has no native QR command.
