from __future__ import annotations

import os
import posixpath
from pathlib import Path

from fakes import FakeJellyfinClient

from media_sync_manager import reconcile
from media_sync_manager.errors import TransientError
from media_sync_manager.models import MediaItem, MediaSource

REL = "TV/Show/S01/ep.mkv"
RELKEY = "TV/Show/S01/ep"


def _jf(playlists):
    return FakeJellyfinClient(playlists=playlists)


def _write_output(target, seg_rel: str) -> Path:
    """Write an output file at output_dir/<seg_rel>.mkv, return its path."""
    p = Path(target.output_dir) / f"{seg_rel}.mkv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"out")
    return p


def _only(plans):
    assert len(plans) == 1
    return plans[0]


# 1 cold add ----------------------------------------------------------------
def test_cold_add(make_target, make_config, write_source, make_episode):
    t = make_target(playlist="2D Animation", segment="animation")
    config = make_config([t])
    src = write_source(REL)
    plan = _only(reconcile.plan_all(config, _jf({"2D Animation": [make_episode(src)]})))

    assert plan.error is None
    assert len(plan.submits) == 1
    s = plan.submits[0]
    assert s.segment == "animation"
    assert s.playlist == "2D Animation"
    assert s.relkey == RELKEY
    assert s.match_key == f"animation/{RELKEY}"
    assert s.input_path == posixpath.join(t.input_dir, "animation", REL)
    assert s.tdarr_path == s.input_path
    assert s.library_id == "lib_iphone"
    assert plan.deletes == ()


# 2 already present ---------------------------------------------------------
def test_already_present_no_submit(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    _write_output(t, f"standard/{RELKEY}")
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert plan.submits == ()
    assert plan.deletes == ()


# 3 in-flight ---------------------------------------------------------------
def test_in_flight_no_double_submit(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    input_path = Path(t.input_dir) / "standard" / REL
    input_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, input_path)
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert plan.submits == ()
    assert any("in-flight" in s for s in plan.skipped)


# 4 restart safety ----------------------------------------------------------
def test_restart_safety(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    input_path = Path(t.input_dir) / "standard" / REL
    input_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, input_path)
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert plan.submits == ()


# 5 output appears ----------------------------------------------------------
def test_output_appears_then_satisfied(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    jf = _jf({"PL": [make_episode(src)]})
    assert len(_only(reconcile.plan_all(config, jf)).submits) == 1
    _write_output(t, f"standard/{RELKEY}")
    assert _only(reconcile.plan_all(config, jf)).submits == ()


# 6 orphan removal ----------------------------------------------------------
def test_orphan_deletes_only_device_file(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    _write_output(t, f"standard/{RELKEY}")
    orphan = _write_output(t, "standard/Old/gone")
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert [d.path for d in plan.deletes] == [str(orphan)]
    assert plan.submits == ()
    assert Path(src).exists()


# 7 transient safety --------------------------------------------------------
def test_transient_lookup_failure_does_not_purge(make_target, make_config):
    t = make_target(segment="standard")
    config = make_config([t])
    _write_output(t, "standard/Anything/ep")
    jf = FakeJellyfinClient(find_error=TransientError("jellyfin down"))
    plan = _only(reconcile.plan_all(config, jf))
    assert plan.error is not None
    assert plan.deletes == ()
    assert plan.submits == ()


# 8 empty playlist ----------------------------------------------------------
def test_empty_playlist_removes_orphans(make_target, make_config):
    t = make_target(segment="standard")
    config = make_config([t])
    orphan = _write_output(t, "standard/Old/ep")
    plan = _only(reconcile.plan_all(config, _jf({"PL": []})))
    assert plan.error is None
    assert [d.path for d in plan.deletes] == [str(orphan)]


# 9 segment comes from which playlist (two targets, one shared output) ------
def test_segment_from_playlist(make_target, make_config, write_source, make_episode):
    t2d = make_target(playlist="2D", segment="animation", device="iphone")
    tstd = make_target(playlist="Std", segment="standard", device="iphone")
    config = make_config([t2d, tstd])
    a = write_source("TV/A/ep.mkv")
    b = write_source("TV/B/ep.mkv")
    plan = _only(
        reconcile.plan_all(config, _jf({"2D": [make_episode(a)], "Std": [make_episode(b)]}))
    )
    by_key = {s.match_key: s for s in plan.submits}
    assert by_key["animation/TV/A/ep"].segment == "animation"
    assert by_key["standard/TV/B/ep"].segment == "standard"


# 10 shared output_dir: neither target deletes the other's files ------------
def test_shared_output_no_cross_delete(make_target, make_config, write_source, make_episode):
    t2d = make_target(playlist="2D", segment="animation", device="iphone")
    tstd = make_target(playlist="Std", segment="standard", device="iphone")
    config = make_config([t2d, tstd])
    a = write_source("TV/A/ep.mkv")
    b = write_source("TV/B/ep.mkv")
    _write_output(t2d, "animation/TV/A/ep")
    _write_output(tstd, "standard/TV/B/ep")
    plan = _only(
        reconcile.plan_all(config, _jf({"2D": [make_episode(a)], "Std": [make_episode(b)]}))
    )
    assert plan.submits == ()
    assert plan.deletes == ()  # each output is claimed by its own segment's desired item


# 11 re-categorisation: moving an item between playlists re-encodes + retires
def test_recategorisation(make_target, make_config, write_source, make_episode):
    t2d = make_target(playlist="2D", segment="animation", device="iphone")
    tstd = make_target(playlist="Std", segment="standard", device="iphone")
    config = make_config([t2d, tstd])
    x = write_source("TV/X/ep.mkv")
    stale = _write_output(tstd, "standard/TV/X/ep")  # was standard, now moved to 2D
    plan = _only(reconcile.plan_all(config, _jf({"2D": [make_episode(x)], "Std": []})))
    assert [s.match_key for s in plan.submits] == ["animation/TV/X/ep"]
    assert [d.path for d in plan.deletes] == [str(stale)]


# 12 no media source --------------------------------------------------------
def test_item_without_media_source_is_skipped(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    good = write_source(REL)
    no_source = MediaItem(id="x", name="No Source", type="Episode", media_sources=())
    plan = _only(reconcile.plan_all(config, _jf({"PL": [no_source, make_episode(good)]})))
    assert len(plan.submits) == 1
    assert plan.submits[0].relkey == RELKEY
    assert any("no usable media source" in s for s in plan.skipped)


# 13 multi-version ----------------------------------------------------------
def test_multi_version_picks_largest(make_target, make_config, write_source):
    t = make_target(segment="standard")
    config = make_config([t])
    small = write_source("TV/Show/S01/small.mkv")
    big = write_source("TV/Show/S01/big.mkv")
    item = MediaItem(
        id="m",
        name="Multi",
        type="Episode",
        media_sources=(MediaSource(path=small, size=100), MediaSource(path=big, size=999)),
    )
    plan = _only(reconcile.plan_all(config, _jf({"PL": [item]})))
    assert len(plan.submits) == 1
    assert plan.submits[0].source == big
    assert plan.submits[0].relkey == "TV/Show/S01/big"
