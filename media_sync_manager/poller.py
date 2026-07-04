"""Long-lived poller: run a sync cycle every poll_interval, single-instance, signal-aware."""

from __future__ import annotations

import fcntl
import signal
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import log, sync
from .errors import PermanentError
from .jellyfin import JellyfinClient
from .models import Config
from .tdarr import TdarrClient

_log = log.get("poller")

_DEFAULT_LOCK = "/run/media-sync-manager.lock"


class _Stopper:
    def __init__(self) -> None:
        self.stop = False

    def handle(self, *_args: object) -> None:
        self.stop = True


def acquire_lock(lock_path: str = _DEFAULT_LOCK):
    """Take an exclusive, non-blocking flock so a second instance refuses to start."""
    try:
        fh = open(lock_path, "w")
    except OSError:
        lock_path = str(Path(tempfile.gettempdir()) / "media-sync-manager.lock")
        fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise PermanentError(f"another instance holds the lock: {lock_path}") from exc
    return fh


def run_forever(
    config: Config,
    jellyfin: JellyfinClient,
    tdarr: TdarrClient,
    *,
    lock_path: str = _DEFAULT_LOCK,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.handle)
    signal.signal(signal.SIGINT, stopper.handle)

    lock = acquire_lock(lock_path)
    _log.info("started; polling every %ss", config.poll_interval_seconds)
    try:
        while not stopper.stop:
            try:
                sync.run_cycle(config, jellyfin, tdarr)
            except Exception as exc:  # a cycle error must never kill the daemon
                _log.error("cycle failed: %s", exc)
            waited = 0
            while waited < config.poll_interval_seconds and not stopper.stop:
                sleep(1)
                waited += 1
    finally:
        lock.close()
        _log.info("stopped")
