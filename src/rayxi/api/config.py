"""Shared configuration helpers for the RayXI API.

Environment variables:
    RAYXI_SCHEMAS_DIR   Path to the directory containing FSM schema JSON files.
                        Defaults to  <cwd>/schemas.
    RAYXI_WEB_DIR       Path to the web/ directory (templates + static assets).
                        Defaults to  <cwd>/web.
"""

from __future__ import annotations

import os
from pathlib import Path


def schemas_dir() -> Path:
    env = os.environ.get("RAYXI_SCHEMAS_DIR")
    return Path(env) if env else Path.cwd() / "schemas"


def web_dir() -> Path:
    env = os.environ.get("RAYXI_WEB_DIR")
    return Path(env) if env else Path.cwd() / "web"
