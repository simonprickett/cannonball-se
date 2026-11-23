"""Receipt settings, read from environment variables (systemd EnvironmentFile on the Pi, or `set -a; . receipts.env`)."""
import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ReceiptConfig:
    qr_enabled: bool = False
    # "auto" = native QR command when the link is short enough, else bitmap; "native" / "bitmap" force one.
    qr_mode: str = "auto"
    # Longest link (characters) the printer's native QR accepts. The CSN-A2 printed 29 but not 147; the real
    # cutoff is untested, so this is the largest length verified. Raise it once the cutoff is known.
    qr_native_max_chars: int = 29
    # Public Grafana base URL, e.g. https://play.grafana.org (no trailing slash). QR needs it.
    dashboard_base_url: str = ""
    dashboard_path: str = "/d/cannonball-recent-games/recent-games"
    # Time range put in the link: from (ended - this) to (ended + 5 min). Loki panels use $__range.
    qr_window_minutes: int = 15
    # Optional logo image (any format Pillow reads) printed instead of the CANNONBALL title text.
    logo_path: str = ""
    logo_width: int = 320  # dots, max 384
    # Blank lines fed after the last line so the receipt clears the tear bar (no cutter).
    feed_lines: int = 8

    @classmethod
    def from_env(cls) -> "ReceiptConfig":
        return cls(
            qr_enabled=_flag("RECEIPT_QR_ENABLED"),
            qr_mode=os.environ.get("RECEIPT_QR_MODE", cls.qr_mode).strip().lower(),
            qr_native_max_chars=int(os.environ.get("RECEIPT_QR_NATIVE_MAX_CHARS", cls.qr_native_max_chars)),
            dashboard_base_url=os.environ.get("RECEIPT_DASHBOARD_BASE_URL", "").rstrip("/"),
            dashboard_path=os.environ.get("RECEIPT_DASHBOARD_PATH", cls.dashboard_path),
            qr_window_minutes=int(os.environ.get("RECEIPT_QR_WINDOW_MINUTES", cls.qr_window_minutes)),
            logo_path=os.environ.get("RECEIPT_LOGO_PATH", ""),
            logo_width=int(os.environ.get("RECEIPT_LOGO_WIDTH", cls.logo_width)),
            feed_lines=int(os.environ.get("RECEIPT_FEED_LINES", cls.feed_lines)),
        )
