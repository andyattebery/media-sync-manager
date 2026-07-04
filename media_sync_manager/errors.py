"""Typed exceptions. Transient vs Permanent drives retry-next-cycle vs surface-and-stop."""

from __future__ import annotations


class MediaSyncError(Exception):
    """Base for all media-sync-manager errors."""


class ConfigError(MediaSyncError):
    """Invalid configuration. Fatal at startup."""


class PathRemapError(MediaSyncError):
    """A path could not be remapped between coordinate systems (no rule matched)."""


class TransientError(MediaSyncError):
    """A recoverable condition for this cycle (network blip, playlist missing, share offline).

    The caller should skip the affected unit of work and retry on the next cycle. Critically,
    a device hitting a TransientError must NOT have its orphan diff computed (never wipe a folder
    because a lookup failed).
    """


class PermanentError(MediaSyncError):
    """A misconfiguration or unrecoverable condition for a unit of work. Surface, do not spin."""
