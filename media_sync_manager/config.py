"""Load + validate YAML config into the frozen Config dataclass. Hard-fail at startup on errors."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from . import fsops
from .errors import ConfigError
from .models import (
    Config,
    JellyfinConfig,
    PathMap,
    Playlist,
    Target,
    TdarrConfig,
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    """Recursively expand ${ENV} references in strings; raise on a missing variable."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"environment variable {name!r} referenced in config is not set")
            return os.environ[name]

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field {key!r}")
    return d[key]


def _path_maps(raw: Any, where: str, remote_key: str, remote_is_src: bool) -> tuple[PathMap, ...]:
    """Parse `{local, <remote_key>}` entries into PathMap(src, dst) in the direction remap needs.

    path_maps rewrite jellyfin->local (remote is the src); tdarr_path_maps rewrite local->tdarr
    (remote is the dst). Anchoring both on `local` keeps the config free of confusing from/to.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: must be a list of {{local, {remote_key}}} entries")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "local" not in entry or remote_key not in entry:
            raise ConfigError(f"{where}[{i}]: must have 'local' and '{remote_key}'")
        local = str(entry["local"])
        remote = str(entry[remote_key])
        out.append(
            PathMap(src=remote, dst=local) if remote_is_src else PathMap(src=local, dst=remote)
        )
    return tuple(out)


def _targets(raw: Any) -> tuple[Target, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("targets: must be a non-empty list")
    out = []
    seen: set[str] = set()
    for i, t in enumerate(raw):
        where = f"targets[{i}]"
        if not isinstance(t, dict):
            raise ConfigError(f"{where}: must be a mapping")
        name = str(_require(t, "name", where))
        if name in seen:
            raise ConfigError(f"{where}: duplicate target name {name!r}")
        seen.add(name)
        library_id = str(_require(t, "library_id", where))
        pls_raw = _require(t, "playlists", where)
        if not isinstance(pls_raw, list) or not pls_raw:
            raise ConfigError(f"{where}.playlists: must be a non-empty list")
        playlists = []
        for j, p in enumerate(pls_raw):
            pw = f"{where}.playlists[{j}]"
            if not isinstance(p, dict):
                raise ConfigError(f"{pw}: must be a mapping")
            playlists.append(
                Playlist(
                    playlist_name=str(_require(p, "playlist", pw)),
                    segment=str(_require(p, "segment", pw)),
                    library_id=(str(p["library_id"]) if p.get("library_id") else None),
                )
            )
        out.append(Target(name=name, library_id=library_id, playlists=tuple(playlists)))
    return tuple(out)


def parse(raw: dict[str, Any]) -> Config:
    """Build a validated Config from an already-loaded (and env-expanded) mapping."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    jf = _require(raw, "jellyfin", "config")
    jellyfin = JellyfinConfig(
        url=str(_require(jf, "url", "jellyfin")).rstrip("/"),
        api_key=str(_require(jf, "api_key", "jellyfin")),
        user_id=str(_require(jf, "user_id", "jellyfin")),
    )

    td = _require(raw, "tdarr", "config")
    tdarr = TdarrConfig(
        url=str(_require(td, "url", "tdarr")).rstrip("/"),
        username=(str(td["username"]) if td.get("username") else None),
        password=(str(td["password"]) if td.get("password") else None),
        request_timeout_seconds=int(td.get("request_timeout_seconds", 20)),
        submit_timeout_seconds=int(td.get("submit_timeout_seconds", 21600)),
    )

    input_mode = str(raw.get("input_mode", fsops.AUTO))
    if input_mode not in fsops.MODES:
        raise ConfigError(
            f"input_mode: must be one of {list(fsops.MODES)}, got {input_mode!r}"
        )

    return Config(
        jellyfin=jellyfin,
        tdarr=tdarr,
        media_root=str(_require(raw, "media_root", "config")).rstrip("/"),
        transcode_root=str(_require(raw, "transcode_root", "config")).rstrip("/"),
        targets=_targets(_require(raw, "targets", "config")),
        path_maps=_path_maps(raw.get("path_maps"), "path_maps", "jellyfin", remote_is_src=True),
        tdarr_path_maps=_path_maps(raw.get("tdarr_path_maps"), "tdarr_path_maps", "tdarr", remote_is_src=False),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 45)),
        input_mode=input_mode,
    )


def load(path: str | Path) -> Config:
    """Read a YAML file, expand ${ENV}, and validate into a Config."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return parse(_expand(raw))
