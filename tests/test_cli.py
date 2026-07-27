from __future__ import annotations

import errno
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient

from media_sync_manager import cli
from media_sync_manager.errors import TransientError

REL = "TV Shows/Meadowlark/S01/E01.mkv"


def _exdev(*_args, **_kwargs):
    raise OSError(errno.EXDEV, "cross-device link")


def _jf(items):
    return FakeJellyfinClient(playlists={"PL": items})


def _target(make_target, make_playlist):
    return make_target(playlists=[make_playlist("PL", "standard")])


def _input_path(env) -> str:
    return str(Path(env.transcode) / "iphone" / "standard" / REL)


def test_dry_run_changes_nothing(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=True, out=lines.append)

    assert rc == 0
    assert td.scans == []
    assert not Path(_input_path(env)).exists()
    assert any("add" in ln for ln in lines)


def test_real_sync_executes(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=False, out=lambda _l: None)

    assert rc == 0
    assert Path(_input_path(env)).exists()
    assert len(td.scans) == 1
    library_id, paths, _mode = td.scans[0]
    assert library_id == "lib_iphone"
    assert paths == [_input_path(env)]


def test_transient_error_returns_nonzero(make_target, make_playlist, make_config):
    config = make_config([_target(make_target, make_playlist)])
    jf = FakeJellyfinClient(find_error=TransientError("down"))
    rc = cli.cmd_sync(config, jf, FakeTdarrClient(), dry_run=False, out=lambda _l: None)
    assert rc == 1


def test_doctor_reports_the_probed_input_mode(env, make_target, make_playlist, make_config):
    """doctor had no coverage at all, which is how the bogus st_dev check survived."""
    config = make_config([_target(make_target, make_playlist)])
    lines: list[str] = []

    cli.cmd_doctor(config, _jf([]), FakeTdarrClient(), out=lines.append)

    assert any("input mode" in ln and "hardlink" in ln for ln in lines)
    assert not any("same filesystem" in ln for ln in lines)


def test_doctor_flags_transcode_root_outside_media_root(
    env, make_target, make_playlist, make_config, monkeypatch
):
    """Siblings make a relative symlink resolve outside an SMB share, where Samba hides the file."""
    monkeypatch.setattr(os, "link", _exdev)  # force symlink mode
    config = make_config([_target(make_target, make_playlist)])
    lines: list[str] = []

    rc = cli.cmd_doctor(config, _jf([]), FakeTdarrClient(), out=lines.append)

    # conftest deliberately puts media/ and transcode/ side by side.
    assert any("FAIL" in ln and "transcode_root is under media_root" in ln for ln in lines)
    assert rc == 1


def test_doctor_does_not_explain_a_failure_that_did_not_happen(
    env, make_target, make_playlist, make_config, monkeypatch
):
    """An `[OK ]` line must not print the reason the check would have failed.

    doctor used to render `[OK ] transcode_root is under media_root: '…' is outside '…', so a
    relative symlink resolves outside an SMB share…` — a passing check explaining the problem it did
    not find. Blanket-suppressing detail on success is the wrong fix: four of the ten check() calls
    are informative when they pass, so this also asserts `input mode` keeps its detail.
    """
    monkeypatch.setattr(os, "link", _exdev)  # force symlink mode, the only mode that runs the check
    nested = Path(env.media) / "transcode"   # conftest puts them side by side; nest it here
    nested.mkdir()
    config = replace(make_config([_target(make_target, make_playlist)]), transcode_root=str(nested))
    # A library that actually watches the input dir, so every check passes and rc is 0 — otherwise
    # the assertion below would pass on a doctor that failed for an unrelated reason.
    td = FakeTdarrClient(
        libraries=[{"_id": "lib_iphone", "folder": str(nested / "iphone" / "standard")}]
    )
    lines: list[str] = []

    rc = cli.cmd_doctor(config, _jf([]), td, out=lines.append)

    under = [ln for ln in lines if "transcode_root is under media_root" in ln]
    assert under and under[0].startswith("[OK ]"), under
    assert "wide links" not in under[0], f"an [OK ] line explained a failure: {under[0]!r}"
    assert under[0].rstrip() == "[OK ] transcode_root is under media_root"
    # The other direction: detail that IS informative on success must survive.
    assert any(ln.startswith("[OK ] input mode") and "symlink" in ln for ln in lines)
    assert rc == 0


def test_status_is_read_only(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_status(config, _jf([make_episode(src)]), td, out=lines.append)

    assert rc == 0
    assert td.scans == []
    assert not Path(_input_path(env)).exists()


# --- web subcommand ----------------------------------------------------------


def test_web_parser_defaults():
    args = cli.build_parser().parse_args(["web"])
    # 0.0.0.0 because 127.0.0.1 inside a container is unreachable through a port mapping.
    assert (args.host, args.port) == ("0.0.0.0", 8087)
    assert cli.build_parser().parse_args(["web", "--port", "9000"]).port == 9000


def test_web_without_the_extra_explains_the_extra(monkeypatch):
    """The message must name the install extra, not an internal module."""
    monkeypatch.setitem(sys.modules, "flask", None)
    lines: list[str] = []
    rc = cli.cmd_web(None, None, None, host="0.0.0.0", port=8087, out=lines.append)
    assert rc == 2
    assert any("media-sync-manager[web]" in ln for ln in lines)


def test_web_passes_host_and_port_through(monkeypatch):
    from media_sync_manager import web as web_mod

    seen = {}

    class StubApp:
        def run(self, host, port):
            seen["host"], seen["port"] = host, port

    monkeypatch.setattr(web_mod, "create_app", lambda jellyfin: StubApp())
    jf = _jf([])
    rc = cli.cmd_web(None, jf, None, host="1.2.3.4", port=9999, out=lambda _l: None)
    assert rc == 0 and seen == {"host": "1.2.3.4", "port": 9999}


def test_shipped_container_commands_parse():
    """The Dockerfile CMD and the compose `command:` must be parseable by our own parser.

    `--config` is a top-level argument, so `run --config …` and `web --config …` are argparse errors
    that exit 2 before anything runs — i.e. neither container could start, and `docker compose up -d`
    was broken for both services. Nothing caught it because every documented one-shot uses
    `docker compose run --rm … <cmd>`, which overrides CMD and falls back to the argparse default
    path. Parsed from the real files rather than a copy, so the two cannot drift.
    """
    import json
    import re

    root = Path(__file__).resolve().parent.parent

    dockerfile = (root / "Dockerfile").read_text()
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))

    compose = (root / "docker-compose.yml").read_text()
    commands = [json.loads(m) for m in re.findall(r"^\s*command:\s*(\[.*\])$", compose, re.M)]

    assert cmd, "no CMD found in the Dockerfile"
    assert commands, "no command: found in docker-compose.yml"
    for argv in [cmd, *commands]:
        # parse_args calls sys.exit(2) on an unrecognised argument.
        cli.build_parser().parse_args(argv)


def test_core_commands_do_not_import_flask():
    """The whole point of the optional extra: run/sync/status/doctor stay on requests+pyyaml."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import media_sync_manager.cli; "
         "print('flask' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"
