from __future__ import annotations

import os
from pathlib import Path

from fakes import FakeJellyfinClient

from media_sync_manager import reconcile
from media_sync_manager.errors import TransientError
from media_sync_manager.models import MediaItem, MediaSource

REL = "TV Shows/Meadowlark/S01/E01.mkv"
RELKEY = "TV Shows/Meadowlark/S01/E01"


def _jf(playlists):
    return FakeJellyfinClient(playlists=playlists)


def _only(plans):
    assert len(plans) == 1
    return plans[0]


def _input(env, name, segment, rel) -> Path:
    return Path(env.transcode) / name / segment / rel


def _output(env, name, seg_rel) -> Path:
    return Path(env.transcode) / name / "sync" / f"{seg_rel}.mkv"


def _make_link(src: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dest)


def _write_output(env, name, seg_rel) -> Path:
    p = _output(env, name, seg_rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"out")
    return p


# 1 cold add ----------------------------------------------------------------
def test_cold_add(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("2D Animation", "animation")])
    config = make_config([t])
    src = write_source(REL)
    plan = _only(reconcile.plan_all(config, _jf({"2D Animation": [make_episode(src)]})))

    assert plan.error is None
    assert len(plan.adds) == 1
    a = plan.adds[0]
    assert a.segment == "animation"
    assert a.playlist == "2D Animation"
    assert a.relkey == RELKEY
    assert a.input_path == str(_input(env, "iphone", "animation", REL))
    assert a.tdarr_path == a.input_path  # no tdarr_path_maps
    assert a.library_id == "lib_iphone"
    assert plan.removes == ()
    assert plan.deletes == ()


# 2 present input -> no add -------------------------------------------------
def test_present_input_no_add(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    src = write_source(REL)
    _make_link(src, _input(env, "iphone", "standard", REL))
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert plan.adds == ()
    assert plan.removes == ()


# 3 remove un-listed input --------------------------------------------------
def test_remove_unlisted_input(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    kept = write_source(REL)
    gone = write_source("TV Shows/Old/S01/E01.mkv")
    _make_link(kept, _input(env, "iphone", "standard", REL))
    stale_input = _input(env, "iphone", "standard", "TV Shows/Old/S01/E01.mkv")
    _make_link(gone, stale_input)
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(kept)]})))
    assert plan.adds == ()
    assert [r.input_path for r in plan.removes] == [str(stale_input)]
    assert Path(gone).exists()  # original untouched


# 4 sweep orphan output -----------------------------------------------------
def test_sweep_orphan_output(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    src = write_source(REL)
    _make_link(src, _input(env, "iphone", "standard", REL))
    _write_output(env, "iphone", f"standard/{RELKEY}")  # backed -> kept
    orphan = _write_output(env, "iphone", "standard/Old/S01/E01")  # not desired -> swept
    plan = _only(reconcile.plan_all(config, _jf({"PL": [make_episode(src)]})))
    assert [d.path for d in plan.deletes] == [str(orphan)]


# 5 transient suppresses removes + sweep ------------------------------------
def test_transient_suppresses_removes_and_sweep(env, make_target, make_playlist, make_config, write_source):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    stray_src = write_source("TV Shows/Stray/E01.mkv")
    _make_link(stray_src, _input(env, "iphone", "standard", "TV Shows/Stray/E01.mkv"))
    _write_output(env, "iphone", "standard/TV Shows/Stray/E01")
    jf = FakeJellyfinClient(find_error=TransientError("jellyfin down"))
    plan = _only(reconcile.plan_all(config, jf))
    assert plan.error is not None
    assert plan.removes == ()
    assert plan.deletes == ()
    assert plan.adds == ()


# 6 empty playlist removes inputs + sweeps outputs --------------------------
def test_empty_playlist_removes(env, make_target, make_playlist, make_config, write_source):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    stray_src = write_source("TV Shows/Old/E01.mkv")
    stale_input = _input(env, "iphone", "standard", "TV Shows/Old/E01.mkv")
    _make_link(stray_src, stale_input)
    orphan = _write_output(env, "iphone", "standard/TV Shows/Old/E01")
    plan = _only(reconcile.plan_all(config, _jf({"PL": []})))
    assert plan.error is None
    assert [r.input_path for r in plan.removes] == [str(stale_input)]
    assert [d.path for d in plan.deletes] == [str(orphan)]


# 7 re-categorisation (move between playlists) ------------------------------
def test_recategorisation(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("2D", "animation"), make_playlist("Std", "standard")])
    config = make_config([t])
    x = write_source(REL)
    # previously Standard: stale input + output under standard/
    stale_input = _input(env, "iphone", "standard", REL)
    _make_link(x, stale_input)
    stale_output = _write_output(env, "iphone", f"standard/{RELKEY}")
    plan = _only(reconcile.plan_all(config, _jf({"2D": [make_episode(x)], "Std": []})))

    assert [f"{a.segment}/{a.relkey}" for a in plan.adds] == [f"animation/{RELKEY}"]
    assert [r.input_path for r in plan.removes] == [str(stale_input)]
    assert [d.path for d in plan.deletes] == [str(stale_output)]


# 8 no media source ---------------------------------------------------------
def test_no_media_source_skipped(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    good = write_source(REL)
    no_src = MediaItem(id="x", name="No Source", type="Episode", media_sources=())
    plan = _only(reconcile.plan_all(config, _jf({"PL": [no_src, make_episode(good)]})))
    assert len(plan.adds) == 1
    assert plan.adds[0].relkey == RELKEY
    assert any("no usable media source" in s for s in plan.skipped)


# 9 multi-version -----------------------------------------------------------
def test_multi_version_picks_largest(env, make_target, make_playlist, make_config, write_source):
    t = make_target(playlists=[make_playlist("PL", "standard")])
    config = make_config([t])
    small = write_source("TV Shows/S/small.mkv")
    big = write_source("TV Shows/S/big.mkv")
    item = MediaItem(
        id="m", name="Multi", type="Episode",
        media_sources=(MediaSource(path=small, size=100), MediaSource(path=big, size=999)),
    )
    plan = _only(reconcile.plan_all(config, _jf({"PL": [item]})))
    assert len(plan.adds) == 1
    assert plan.adds[0].source == big
    assert plan.adds[0].relkey == "TV Shows/S/big"


# 10 two playlists, one target ----------------------------------------------
def test_two_playlists_one_target(env, make_target, make_playlist, make_config, write_source, make_episode):
    t = make_target(playlists=[make_playlist("2D", "animation"), make_playlist("Std", "standard")])
    config = make_config([t])
    a = write_source("TV Shows/A/E01.mkv")
    b = write_source("TV Shows/B/E01.mkv")
    plan = _only(reconcile.plan_all(config, _jf({"2D": [make_episode(a)], "Std": [make_episode(b)]})))
    by_seg = {x.segment: x for x in plan.adds}
    assert by_seg["animation"].relkey == "TV Shows/A/E01"
    assert by_seg["standard"].relkey == "TV Shows/B/E01"
