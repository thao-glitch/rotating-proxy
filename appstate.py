#!/usr/bin/env python3
"""
Where the app keeps its settings (proxy_state.json).

From a checkout that is next to the scripts -- the test suites and the
documented "delete proxy_state.json to go back to the shipped list" both
depend on it.  From a packaged (PyInstaller "frozen") build it moves to
the user's per-user config directory, because the install location of a
packaged app (/opt, Program Files, /Applications) is never writable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Rotating Proxy"          # display name / folder name on Windows+mac
APP_SLUG = "rotating-proxy"          # folder name on Linux


def state_file() -> Path:
    """Absolute path of the state file for this install."""
    if not getattr(sys, "frozen", False):
        # running from a checkout (or the test suites): next to the scripts
        return Path(__file__).resolve().with_name("proxy_state.json")

    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return base / APP_NAME / "proxy_state.json"
    if sys.platform == "darwin":
        return (home / "Library" / "Application Support" / APP_NAME
                / "proxy_state.json")
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / APP_SLUG / "proxy_state.json"
