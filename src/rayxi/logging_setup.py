"""Centralised logging setup for RayXI.

Call configure() once at process startup. After that every module just does:
    import logging
    log = logging.getLogger("rayxi.whatever")

Two outputs:
  • stderr  — INFO+ (same as before, captured by uvicorn / terminal)
  • logs/errors.log — WARNING+ with full tracebacks, rotating at 5 MB × 3 files
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_DIR = Path("logs")
_ERR_FILE = _LOG_DIR / "errors.log"

_CONSOLE_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_FILE_FMT = "%(asctime)s  %(levelname)-8s  %(name)s\n  %(message)s\n"
_DATE_FMT = "%Y-%m-%dT%H:%M:%S"

_configured = False


def configure(console_level: int = logging.INFO) -> None:
    """Set up logging. Safe to call multiple times (idempotent)."""
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # ── Console handler ──────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    # ── Rotating file handler — WARNING and above, full tracebacks ───────
    file_handler = logging.handlers.RotatingFileHandler(
        _ERR_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(_ErrorFormatter(_FILE_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_handler)

    # Silence noisy third-party loggers at file level
    for name in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


class _ErrorFormatter(logging.Formatter):
    """Formats records with full exc_info traceback and a blank separator."""

    def format(self, record: logging.LogRecord) -> str:
        # Always include exc_info if present; force exc_text to be rendered
        s = super().format(record)
        return s + "\n" + ("─" * 72)
