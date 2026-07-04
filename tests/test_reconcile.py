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


def _jf(items, series=None, find_error=None):
    return FakeJellyfinClient(
        playlists={"PL": items}, series_genres=series or {}, find_error=find_error
    )


def _write_output(device, rel: str) -> Path:
    p = Path(device.output_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"out")
    return p


# 1 -------------------------------------------------------------------------
def test_cold_add_hardlink_path_and_single_scan(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    jf = _jf([make_episode(src, genres=["Animation"])])

    plan = reconcile.plan_device(device, config, jf)

    assert plan.error is None
    assert len(plan.submits) == 1
    s = plan.submits[0]
    assert s.profile == "animation"
    assert s.relkey == RELKEY
    assert s.input_path == posixpath.join(device.input_dir, "animation", REL)
    assert s.tdarr_path == s.input_path  # no tdarr_path_maps
    assert s.library_id == "lib_iphone"
    assert plan.deletes == ()


# 2 -------------------------------------------------------------------------
def test_already_present_with_segment_prefix_no_submit(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    _write_output(device, f"standard/{RELKEY}.mkv")  # flow prepended a <segment>/
    jf = _jf([make_episode(src)])

    plan = reconcile.plan_device(device, config, jf)

    assert plan.submits == ()
    assert plan.deletes == ()  # the present output is claimed by the desired item


# 3 -------------------------------------------------------------------------
def test_in_flight_no_double_submit(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    # an input hardlink already exists from a prior submit
    input_path = Path(device.input_dir) / "standard" / REL
    input_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, input_path)
    jf = _jf([make_episode(src)])

    plan = reconcile.plan_device(device, config, jf)

    assert plan.submits == ()
    assert any("in-flight" in s for s in plan.skipped)


# 4 -------------------------------------------------------------------------
def test_restart_safety_input_link_intact_no_resubmit(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    input_path = Path(device.input_dir) / "standard" / REL
    input_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, input_path)
    jf = _jf([make_episode(src)])

    # a "fresh process" is just a brand-new plan against the same on-disk state
    plan = reconcile.plan_device(device, config, jf)
    assert plan.submits == ()


# 5 -------------------------------------------------------------------------
def test_output_appears_then_satisfied(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    jf = _jf([make_episode(src)])

    first = reconcile.plan_device(device, config, jf)
    assert len(first.submits) == 1

    _write_output(device, f"standard/{RELKEY}.mkv")
    second = reconcile.plan_device(device, config, jf)
    assert second.submits == ()


# 6 -------------------------------------------------------------------------
def test_orphan_deletes_only_device_file(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    _write_output(device, f"standard/{RELKEY}.mkv")  # present + desired
    orphan = _write_output(device, "standard/Old/S01/gone.mkv")  # present, not desired
    jf = _jf([make_episode(src)])

    plan = reconcile.plan_device(device, config, jf)

    assert [d.path for d in plan.deletes] == [str(orphan)]
    assert plan.submits == ()
    assert Path(src).exists()  # original untouched


# 7 -------------------------------------------------------------------------
def test_transient_lookup_failure_does_not_purge(make_device, make_config):
    device = make_device()
    config = make_config([device])
    _write_output(device, "standard/Anything/ep.mkv")  # would be an orphan if we diffed
    jf = _jf([], find_error=TransientError("jellyfin down"))

    plan = reconcile.plan_device(device, config, jf)

    assert plan.error is not None
    assert plan.deletes == ()
    assert plan.submits == ()


# 8 -------------------------------------------------------------------------
def test_empty_playlist_removes_orphans(make_device, make_config):
    device = make_device()
    config = make_config([device])
    orphan = _write_output(device, "standard/Old/ep.mkv")
    jf = _jf([])  # playlist exists, zero items (success)

    plan = reconcile.plan_device(device, config, jf)

    assert plan.error is None
    assert [d.path for d in plan.deletes] == [str(orphan)]


# 9 -------------------------------------------------------------------------
def test_fail_safe_routing_missing_genre_to_standard(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    # episode has no genres and the series resolver returns nothing -> standard
    jf = _jf([make_episode(src, series_id="s1", genres=[])], series={"s1": []})

    plan = reconcile.plan_device(device, config, jf)

    assert plan.submits[0].profile == "standard"
    assert plan.submits[0].input_path == posixpath.join(device.input_dir, "standard", REL)


# 10 ------------------------------------------------------------------------
def test_item_without_media_source_is_skipped(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    good = write_source(REL)
    no_source = MediaItem(id="x", name="No Source", type="Episode", media_sources=())
    jf = _jf([no_source, make_episode(good)])

    plan = reconcile.plan_device(device, config, jf)

    assert len(plan.submits) == 1
    assert plan.submits[0].relkey == RELKEY
    assert any("no usable media source" in s for s in plan.skipped)


# 11 ------------------------------------------------------------------------
def test_multi_version_picks_largest(make_device, make_config, write_source):
    device = make_device()
    config = make_config([device])
    small = write_source("TV/Show/S01/small.mkv")
    big = write_source("TV/Show/S01/big.mkv")
    item = MediaItem(
        id="m",
        name="Multi",
        type="Episode",
        media_sources=(MediaSource(path=small, size=100), MediaSource(path=big, size=999)),
    )
    jf = _jf([item])

    plan = reconcile.plan_device(device, config, jf)

    assert len(plan.submits) == 1
    assert plan.submits[0].source == big
    assert plan.submits[0].relkey == "TV/Show/S01/big"
