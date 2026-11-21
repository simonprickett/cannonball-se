#!/usr/bin/env python3
"""Render a game_end fixture to a sink.

  print_receipt.py fixtures/high_score.json --sink tcp://localhost:9100   # escpresso
  print_receipt.py fixtures/high_score.json --sink file:out/high.raw      # raw ESC/POS bytes
  print_receipt.py fixtures/high_score.json --sink serial:/dev/serial0    # the Pi's printer
"""
import argparse
import json
import logging
from pathlib import Path

from escpos.printer import Dummy, Network, Serial

from config import ReceiptConfig
from receipt import render


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", type=Path)
    ap.add_argument("--sink", default="tcp://localhost:9100")
    ap.add_argument("--qr", action=argparse.BooleanOptionalAction, default=None,
                    help="force the QR on/off (default: RECEIPT_QR_ENABLED)")
    ap.add_argument("--logo", help="logo image path (default: RECEIPT_LOGO_PATH)")
    ap.add_argument("--base-url", help="dashboard base URL (default: RECEIPT_DASHBOARD_BASE_URL)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = ReceiptConfig.from_env()
    if args.qr is not None:
        cfg.qr_enabled = args.qr
    if args.logo:
        cfg.logo_path = args.logo
    if args.base_url:
        cfg.dashboard_base_url = args.base_url.rstrip("/")

    msg = json.loads(args.fixture.read_text())
    kind, _, target = args.sink.partition(":")

    if kind == "file":
        p = Dummy()
        render(p, msg, cfg)
        out = Path(target)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(p.output)
        print(f"wrote {len(p.output)} bytes to {out}")
        return
    if kind == "tcp":
        host, _, port = target.lstrip("/").partition(":")
        p = Network(host, int(port or 9100))
    elif kind == "serial":
        # The printer is wired Pi TX -> printer RX only, so no DSR/DTR handshake.
        p = Serial(target, baudrate=9600, dsrdtr=False)
    else:
        ap.error("sink must be tcp://host:port, file:path or serial:/dev/...")
    try:
        render(p, msg, cfg)
    finally:
        p.close()


if __name__ == "__main__":
    main()
