"""The chain the editor exists to serve: remove here, the file leaves the device.

Everything else tests one half. The editor suite proves the right request is sent; the reconcile
suite proves an absent item gets its input and output retired. Nothing joined them, and until
`FakeJellyfinClient` projected one playlist into both surfaces, nothing could: an editor removal was
invisible to `playlist_items()`, so a test of this chain would have passed while testing nothing.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient, linked_playlist

from media_sync_manager import fsops, reconcile, sync
from media_sync_manager.errors import TransientError

REL = "TV Shows/Meadowlark/S01/E01.mkv"
PLAYLIST = "2D Animation"
SEGMENT = "animation"


def _world(env, make_config, make_target, make_playlist, write_source):
    """An episode that is in the playlist, linked as an input, and already transcoded."""
    src = write_source(REL)
    config = make_config(
        [make_target("iphone", playlists=[make_playlist(PLAYLIST, SEGMENT)])]
    )
    jellyfin = FakeJellyfinClient(**linked_playlist(PLAYLIST, [src]))

    input_path = Path(posixpath.join(str(env.transcode), "iphone", SEGMENT, REL))
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"x")

    output = Path(posixpath.join(str(env.transcode), "iphone", "sync", SEGMENT, REL))
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.with_suffix(".mp4")          # Tdarr rewrites the container
    output.write_bytes(b"transcoded")

    return config, jellyfin, src, input_path, output


def test_editor_removal_retires_the_input_and_the_transcoded_output(
    env, make_config, make_target, make_playlist, write_source
):
    config, jellyfin, src, input_path, output = _world(
        env, make_config, make_target, make_playlist, write_source
    )
    assert input_path.exists() and output.exists()

    # 1. what the editor does when you tick a row and press Remove
    entry_id = jellyfin.playlist_entries(PLAYLIST)[0].playlist_item_id
    result = jellyfin.remove_playlist_entries(PLAYLIST, [entry_id])
    assert result.removed == 1

    # 2. what the poller does on its next cycle
    plan = reconcile.plan_target(config.targets[0], config, jellyfin)
    sync.execute(plan, FakeTdarrClient(), fsops.HARDLINK)

    assert not input_path.exists(), "the input link survived the removal"
    assert not output.exists(), "the transcoded copy survived the removal"
    # The promise the whole project rests on, exercised here on real files.
    assert Path(src).exists(), "the ORIGINAL under media_root was deleted"


def test_a_jellyfin_blip_does_not_purge_the_device(
    env, make_config, make_target, make_playlist, write_source
):
    """The suppression guard. Without it a transient fetch error reads as 'the playlist is empty'
    and the next cycle deletes everything on the device."""
    config, jellyfin, _src, input_path, output = _world(
        env, make_config, make_target, make_playlist, write_source
    )
    jellyfin._find_error = TransientError("jellyfin down")

    plan = reconcile.plan_target(config.targets[0], config, jellyfin)
    sync.execute(plan, FakeTdarrClient(), fsops.HARDLINK)

    assert plan.error is not None
    assert plan.removes == () and plan.deletes == ()
    assert input_path.exists() and output.exists()


def test_one_failing_playlist_suppresses_removes_for_the_whole_target(
    env, make_config, make_target, make_playlist, write_source
):
    """reconcile.py sets `incomplete` and continues when ONE of several playlists fails, so a
    partial view cannot purge items the other playlist did not mention. FakeJellyfinClient has
    supported this via find_error_for since it was written and no test had ever used it."""
    src = write_source(REL)
    config = make_config(
        [make_target("iphone", playlists=[
            make_playlist(PLAYLIST, SEGMENT), make_playlist("Standard", "standard")
        ])]
    )
    jellyfin = FakeJellyfinClient(
        **linked_playlist(PLAYLIST, [src]), find_error_for={"Standard"}
    )
    stale = Path(posixpath.join(str(env.transcode), "iphone", SEGMENT, "TV Shows/Old/E99.mkv"))
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")

    plan = reconcile.plan_target(config.targets[0], config, jellyfin)
    sync.execute(plan, FakeTdarrClient(), fsops.HARDLINK)

    assert plan.error is not None
    assert plan.removes == (), "a partial playlist view was allowed to remove inputs"
    assert stale.exists()
