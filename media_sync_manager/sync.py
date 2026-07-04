"""Execute reconcile plans and run one full sync cycle across all devices."""

from __future__ import annotations

from . import fsops, log, reconcile
from .errors import PermanentError, TransientError
from .jellyfin import JellyfinClient
from .models import Config, DevicePlan
from .tdarr import TdarrClient

_log = log.get("sync")


def describe(plan: DevicePlan) -> list[str]:
    """Human-readable lines describing the planned actions (used by --dry-run and status)."""
    lines: list[str] = []
    if plan.error:
        lines.append(f"[{plan.device}] SKIPPED: {plan.error}")
        return lines
    for s in plan.submits:
        lines.append(f"[{plan.device}] submit ({s.profile}): {s.relkey} -> {s.input_path}")
    for d in plan.deletes:
        lines.append(f"[{plan.device}] delete orphan: {d.path}")
    for reason in plan.skipped:
        lines.append(f"[{plan.device}] skip: {reason}")
    if not plan.touched and not plan.skipped:
        lines.append(f"[{plan.device}] in sync")
    return lines


def execute(plan: DevicePlan, tdarr: TdarrClient) -> None:
    """Apply a plan's side effects: hardlink + scan new items, delete orphan outputs.

    All submits in a device plan share the device's library_id, so they scan in one call.
    """
    if plan.error:
        return
    if plan.submits:
        tdarr_paths = []
        for s in plan.submits:
            fsops.hardlink(s.source, s.input_path)
            tdarr_paths.append(s.tdarr_path)
        library_id = plan.submits[0].library_id
        tdarr.scan_files(library_id, tdarr_paths)
    for d in plan.deletes:
        fsops.delete_output(d.path)


def run_cycle(
    config: Config,
    jellyfin: JellyfinClient,
    tdarr: TdarrClient,
    *,
    dry_run: bool = False,
) -> list[DevicePlan]:
    """Reconcile and (unless dry_run) apply every device. Returns the plans for reporting.

    Per-device errors are logged and do not abort the cycle; the daemon stays up.
    """
    plans: list[DevicePlan] = []
    for device in config.devices:
        plan = reconcile.plan_device(device, config, jellyfin)
        plans.append(plan)
        if dry_run:
            continue
        try:
            execute(plan, tdarr)
        except TransientError as exc:
            _log.warning("device %s: %s (will retry next cycle)", device.name, exc)
        except PermanentError as exc:
            _log.error("device %s: %s", device.name, exc)
    return plans
