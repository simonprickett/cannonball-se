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
