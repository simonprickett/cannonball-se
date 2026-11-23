# Receipts

Game-end thermal receipts for CannonBall-SE (CSN-A2, 58 mm, 384 dots, ESC/POS).

Planning background: the "Game-end receipts via MQTT" section at the end of
[`../LOG_REPLAY_PLAN.md`](../LOG_REPLAY_PLAN.md).

Receipt rendering for a CSN-A2 thermal printer: designed on the Mac with a simulator, then proven on a real printer.
To set up a Raspberry Pi with the printer, see [`PI_SETUP.md`](PI_SETUP.md).

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

The emulator is more permissive than a real printer. It may render commands a printer lacks, and it prints instantly,
whereas the real printer takes seconds for the logo at 9600 baud. Treat the preview as a layout check, not proof the
printer will accept every byte. (Native QR, `GS ( k`, was confirmed on a real CSN-A2 on 2026-11-23, despite the datasheet.)

## Configuration

Environment variables (see `receipts.env.example`; the real `receipts.env` is git-ignored):

| Variable | Default | Meaning |
|---|---|---|
| `RECEIPT_QR_ENABLED` | `false` | Print a QR linking to the game's dashboard page |
| `RECEIPT_QR_MODE` | `auto` | `auto`: native QR if the link fits `RECEIPT_QR_NATIVE_MAX_CHARS`, else bitmap. `native` / `bitmap` force one. Native is fast (a few hundred bytes); bitmap is ~10 s at 9600 baud |
| `RECEIPT_QR_NATIVE_MAX_CHARS` | `29` | Longest link sent to the printer's native QR. A real CSN-A2 printed 29 characters but not 147; the true cutoff is untested |
| `RECEIPT_QR_BOX_WIDTH`, `RECEIPT_QR_BOX_HEIGHT` | `4`, `3` | Bitmap QR module size in dots. The CSN-A2 prints rows ~1.2x taller than columns are wide, so 4x3 prints nearly square (~26 x 24 mm); 3x3 gave 20 x 24 mm |
| `RECEIPT_DASHBOARD_BASE_URL` | unset | Public Grafana base URL; the QR is skipped (with a warning) if unset |
| `RECEIPT_DASHBOARD_PATH` | `/d/cannonball-recent-games/recent-games` | Dashboard uid and slug |
| `RECEIPT_QR_WINDOW_MINUTES` | `15` | Link time range starts this long before the game ended |
| `RECEIPT_LOGO_PATH` | unset | Logo image (PNG best) printed instead of the title text; falls back to text if unreadable |
| `RECEIPT_LOGO_WIDTH` | `320` | Logo width in dots (max 384). Taller logos print slower: ~1 s per 1 KB at 9600 baud |
| `RECEIPT_FEED_LINES` | `8` | Blank lines fed after the last line (`ESC d n`) so the receipt clears the tear bar |

`--qr/--no-qr`, `--logo` and `--base-url` on the CLI override the environment. The native QR module size is chosen
from the link length so the code always fits the 384-dot paper.
