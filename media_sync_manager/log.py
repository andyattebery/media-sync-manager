"""Logging setup. Plain, structured-ish lines to stdout (Docker captures them)."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger("media_sync_manager")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"media_sync_manager.{name}")
