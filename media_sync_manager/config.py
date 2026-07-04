"""Load + validate YAML config into the frozen Config dataclass. Hard-fail at startup on errors."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import (
    Config,
    Device,
    JellyfinConfig,
    PathMap,
    Profile,
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


def _path_maps(raw: Any, where: str) -> tuple[PathMap, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: must be a list of {{from, to}} entries")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "from" not in entry or "to" not in entry:
            raise ConfigError(f"{where}[{i}]: must have 'from' and 'to'")
        out.append(PathMap(src=str(entry["from"]), dst=str(entry["to"])))
    return tuple(out)


def _profiles(raw: Any) -> dict[str, Profile]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("profiles: must be a non-empty mapping")
    out: dict[str, Profile] = {}
    for name, body in raw.items():
        body = body or {}
        if not isinstance(body, dict):
            raise ConfigError(f"profiles.{name}: must be a mapping")
        segment = str(body.get("segment", name))
        match = body.get("match") or {}
        genres = match.get("genres") or []
        tags = match.get("tags") or []
        out[name] = Profile(
            name=name,
            segment=segment,
            match_genres=frozenset(str(g).casefold() for g in genres),
            match_tags=frozenset(str(t).casefold() for t in tags),
        )
    return out


def _devices(raw: Any) -> tuple[Device, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("devices: must be a non-empty list")
    out = []
    seen = set()
    for i, d in enumerate(raw):
        where = f"devices[{i}]"
        if not isinstance(d, dict):
            raise ConfigError(f"{where}: must be a mapping")
        name = str(_require(d, "name", where))
        if name in seen:
            raise ConfigError(f"{where}: duplicate device name {name!r}")
        seen.add(name)
        out.append(
            Device(
                name=name,
                playlist_name=str(_require(d, "playlist_name", where)),
                output_dir=str(_require(d, "output_dir", where)),
                library_id=str(_require(d, "library_id", where)),
                input_dir=str(_require(d, "input_dir", where)),
            )
        )
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

    profiles = _profiles(_require(raw, "profiles", "config"))
    default_profile = str(_require(raw, "default_profile", "config"))
    if default_profile not in profiles:
        raise ConfigError(f"default_profile {default_profile!r} is not defined in profiles")

    priority_raw = raw.get("profile_priority") or list(profiles.keys())
    priority = tuple(str(p) for p in priority_raw)
    for p in priority:
        if p not in profiles:
            raise ConfigError(f"profile_priority references unknown profile {p!r}")

    config = Config(
        jellyfin=jellyfin,
        tdarr=tdarr,
        media_root=str(_require(raw, "media_root", "config")).rstrip("/"),
        profiles=profiles,
        default_profile=default_profile,
        profile_priority=priority,
        devices=_devices(_require(raw, "devices", "config")),
        path_maps=_path_maps(raw.get("path_maps"), "path_maps"),
        tdarr_path_maps=_path_maps(raw.get("tdarr_path_maps"), "tdarr_path_maps"),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 45)),
        genre_cache_ttl_seconds=int(raw.get("genre_cache_ttl_seconds", 900)),
    )
    return config


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
