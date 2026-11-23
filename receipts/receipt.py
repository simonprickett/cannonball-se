"""Game-end receipt layout for the CSN-A2 (58 mm, 384 dots, 32 columns in the 12x24 font).

render() takes any python-escpos printer object, so the same code drives the escpresso
emulator (Network), the Pi's UART (Serial) and tests (Dummy).

QR codes: "native" uses the printer's own QR command (GS ( k), confirmed on a real CSN-A2 (2026-11-23) for
short links (29 chars worked, 147 did not) and tiny on the wire; "bitmap" sends a raster image (GS v 0) and
works for any length, but takes about 10 s at 9600 baud. RECEIPT_QR_MODE=auto (default) picks native when the
link fits RECEIPT_QR_NATIVE_MAX_CHARS, otherwise bitmap.
"""
from __future__ import annotations  # lets `X | None` hints work on Python < 3.10 (older Pi OS)

import functools
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import qrcode
from escpos.constants import QR_ECLEVEL_M, QR_MODEL_2
from PIL import Image, ImageOps

from config import ReceiptConfig

log = logging.getLogger(__name__)

DOTS = 384  # CSN-A2 print width

WIDTH = 32
RULE_HEAVY = "=" * WIDTH
RULE_LIGHT = "-" * WIDTH

RESULTS = {"completed": "COMPLETED", "timeout": "TIME UP"}


def row(label: str, value: str, width: int = WIDTH) -> str:
    """Label left, value right-aligned; value wins if they don't both fit."""
    value = value[: width - 1]
    return label[: width - len(value) - 1].ljust(width - len(value)) + value


def local_time(ended_at: str) -> str:
    return parse_ended(ended_at).astimezone().strftime("%Y-%m-%d %H:%M")


def parse_ended(ended_at: str) -> datetime:
    return datetime.fromisoformat(ended_at.replace("Z", "+00:00"))


def build_url(msg: dict, cfg: ReceiptConfig) -> str | None:
    """Link to this game on the dashboard, or None if the QR can't be built (and why is logged)."""
    if not cfg.dashboard_base_url:
        log.warning("QR enabled but RECEIPT_DASHBOARD_BASE_URL is not set; printing without QR")
        return None
    label = msg.get("session_label")
    if not label:
        log.warning("QR enabled but game_end has no session_label; printing without QR")
        return None
    ended = parse_ended(msg["ended_at"])
    start_ms = int((ended - timedelta(minutes=cfg.qr_window_minutes)).timestamp() * 1000)
    end_ms = int((ended + timedelta(minutes=5)).timestamp() * 1000)
    return (f"{cfg.dashboard_base_url}{cfg.dashboard_path}"
            f"?var-session={quote(label)}&from={start_ms}&to={end_ms}")


def native_qr_size(url: str, max_dots: int = DOTS - 12) -> int:
    """Largest module size (dots, 1-8) at which the printer's own QR fits the paper.

    The printer picks the QR version itself, so assume byte mode (the largest case) and
    error level M, and count modules with the qrcode library.
    """
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(qrcode.util.QRData(url, mode=qrcode.util.MODE_8BIT_BYTE))
    qr.make(fit=True)
    return max(1, min(8, max_dots // qr.modules_count))


def qr_image(url: str, box: int = 4) -> Image.Image:
    """QR centred on a full-width white canvas, so alignment does not depend on ESC a affecting rasters."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("1")
    canvas = Image.new("1", (DOTS, code.height), 1)
    canvas.paste(code, ((DOTS - code.width) // 2, 0))
    return canvas


@functools.lru_cache(maxsize=4)
def logo_image(path: str, width: int) -> Image.Image | None:
    """Logo as a 1-bit, full-width, centred bitmap (greyscale + Floyd-Steinberg dither). None if unusable."""
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, "white")
                im = Image.alpha_composite(bg, im)  # transparent areas -> white, not black
            grey = ImageOps.autocontrast(im.convert("L"))
    except (OSError, ValueError) as e:
        log.warning("cannot load logo %s: %s; printing text title", path, e)
        return None
    width = max(8, min(width, DOTS))
    grey = grey.resize((width, max(1, round(grey.height * width / grey.width))), Image.LANCZOS)
    logo = grey.convert("1")
    canvas = Image.new("1", (DOTS, logo.height), 1)
    canvas.paste(logo, ((DOTS - logo.width) // 2, 0))
    return canvas


def render(p, msg: dict, cfg: ReceiptConfig | None = None) -> None:
    cfg = cfg or ReceiptConfig()
    qr_url = build_url(msg, cfg) if cfg.qr_enabled else None
    initials = (msg.get("initials") or "").strip() or "---"
    result = RESULTS.get(msg.get("completion_status"), str(msg.get("completion_status", "")).upper())

    # Tell python-escpos the paper width. This silences its "media.width.pixel ... not set" print()
    # and makes it reject any image wider than the paper.
    try:
        p.profile.profile_data["media"]["width"]["pixels"] = DOTS
    except (AttributeError, KeyError, TypeError):
        pass

    p._raw(b"\x1b@")  # ESC @ initialise
    p.set(align="center", bold=True)
    p.text(RULE_HEAVY + "\n")
    logo = logo_image(cfg.logo_path, cfg.logo_width) if cfg.logo_path else None
    if logo is not None:
        p.image(logo, impl="bitImageRaster")  # GS v 0 raster
    else:
        p.set(align="center", bold=True, custom_size=True, width=2, height=2)
        p.text("CANNONBALL\n")
    p.set(align="center", bold=True, normal_textsize=True)
    p.text("GAME RECEIPT\n")
    p.text(RULE_HEAVY + "\n")

    p.set(align="left", bold=False)
    p.text(row("PLAYER", initials) + "\n")
    p.text(row("SCORE", f"{int(msg['score']):,}") + "\n")
    p.text(row("STAGE", str(msg["stage"])) + "\n")
    p.text(row("RESULT", result) + "\n")
    p.text(RULE_LIGHT + "\n")
    p.text(row("ENDED", local_time(msg["ended_at"])) + "\n")
    p.text(row("GAME ID", str(msg["game_id"])[:8]) + "\n")
    p.text(RULE_LIGHT + "\n")

    if qr_url:
        p.set(align="center")
        if cfg.qr_mode == "native" or (cfg.qr_mode == "auto" and len(qr_url) <= cfg.qr_native_max_chars):
            p.qr(qr_url, ec=QR_ECLEVEL_M, size=native_qr_size(qr_url), model=QR_MODEL_2, native=True)
        else:
            p.image(qr_image(qr_url), impl="bitImageRaster")  # GS v 0 raster
        p.text("Scan for your game\n")
        p.text(RULE_LIGHT + "\n")

    p.set(align="center")
    p.text("Thanks for playing!\n")
    # No cutter: ESC d n feeds n lines so the receipt clears the tear bar.
    p._raw(b"\x1bd" + bytes([max(0, min(cfg.feed_lines, 255))]))
