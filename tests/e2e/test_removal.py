"""The removal flow, including the paths nothing verified before: confirm-cancel, the silent-204
guard, partial failure, double-submit, and the chunk boundary."""

from __future__ import annotations

import pytest

from . import fixtures as fx

pytestmark = pytest.mark.e2e


def accept_confirm(page):
    page.on("dialog", lambda d: d.accept())


def dismiss_confirm(page):
    page.on("dialog", lambda d: d.dismiss())


def test_cancelling_the_confirm_removes_nothing(editor, open_playlist):
    """The abort path of an irreversible action."""
    page = open_playlist(editor)
    dismiss_confirm(page)
    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")

    assert editor.fake.removals == []
    assert page.locator("#counts").inner_text().startswith("5 of "), "selection was lost on cancel"


def test_removal_shrinks_the_list_and_reports_the_delta(editor, open_playlist):
    page = open_playlist(editor)
    accept_confirm(page)
    before = len(editor.fake.entries[fx.CASE_SET_ID])

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_function("() => /^Removed /.test(document.querySelector('#status').textContent)")

    status = page.locator("#status").inner_text()
    assert f"{before} → {before - 5}" in status
    assert editor.fake.removals[0][1] == ["b1", "b2", "b3", "b4", "b5"]
    assert page.locator("#counts").inner_text().startswith("0 of ")


def test_silent_204_is_called_out_loudly(make_editor, open_playlist):
    """Jellyfin answers 204 even when no entryId matched (jellyfin#12971), so acceptance is not
    proof. This warning is the only thing standing between the user and believing a removal
    worked — and it was the plan's headline feature with no test behind it."""
    editor = make_editor(pretend_only=True)
    page = open_playlist(editor)
    accept_confirm(page)

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_function("() => document.querySelector('#status').classList.contains('error')")

    status = page.locator("#status").inner_text()
    assert "did not shrink" in status
    assert "5" in status


def test_partial_failure_shows_warnings_and_still_refetches(make_editor, open_playlist):
    """'Still refresh' is a correctness rule, not politeness: the server is the truth."""
    editor = make_editor(fail_after=2)
    page = open_playlist(editor)
    accept_confirm(page)

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_selector("#warnings:not(.d-none)")

    assert "failed" in page.locator("#status").inner_text()
    # the two that succeeded are gone from the refetched tree
    assert page.locator('input[data-role="item"][data-id="b1"]').count() == 0
    assert page.locator('input[data-role="item"][data-id="b3"]').count() == 1


def test_total_failure_reports_and_keeps_the_list(make_editor, open_playlist):
    from media_sync_manager.errors import TransientError

    editor = make_editor(remove_error=TransientError("jellyfin down"))
    page = open_playlist(editor)
    accept_confirm(page)

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_function("() => document.querySelector('#status').classList.contains('error')")

    assert "failed" in page.locator("#status").inner_text().lower()
    assert page.locator('input[data-role="item"][data-id="b1"]').count() == 1


def test_double_click_remove_sends_one_request(editor, open_playlist):
    """The busy guard on a destructive action."""
    page = open_playlist(editor)
    accept_confirm(page)
    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.locator("#remove").dblclick()
    page.wait_for_function("() => /^Removed /.test(document.querySelector('#status').textContent)")
    assert len(editor.fake.removals) == 1


def test_removing_120_crosses_the_chunk_boundary(editor, open_playlist):
    """Chunking is unit-tested in isolation; this is the only test that crosses the boundary in a
    real flow, which is why the bulk fixture exists."""
    page = open_playlist(editor, fx.NAMES[fx.BULK_ID])
    accept_confirm(page)
    page.check("#select-all")
    page.click("#remove")
    page.wait_for_function("() => /^Removed /.test(document.querySelector('#status').textContent)")

    playlist_id, ids = editor.fake.removals[0]
    assert playlist_id == fx.BULK_ID
    assert len(ids) == 120 and len(set(ids)) == 120
    assert editor.fake.entries[fx.BULK_ID] == []


def test_removing_everything_shows_the_empty_state(editor, open_playlist):
    page = open_playlist(editor, fx.NAMES[fx.BULK_ID])
    accept_confirm(page)
    page.check("#select-all")
    page.click("#remove")
    page.wait_for_selector(".empty")
    assert "empty" in page.locator(".empty").inner_text().lower()


def test_empty_playlist_renders_without_crashing(editor, open_playlist):
    page = open_playlist(editor, fx.NAMES[fx.EMPTY_ID])
    assert page.locator(".empty").count() == 1
    assert page.locator("#remove").is_disabled()


def test_reload_button_refetches(editor, open_playlist):
    page = open_playlist(editor)
    # mutate the server out from under the page, then press ↻
    editor.fake.entries[fx.CASE_SET_ID] = [
        e for e in editor.fake.entries[fx.CASE_SET_ID] if e.playlist_item_id != "b1"
    ]
    assert page.locator('input[data-role="item"][data-id="b1"]').count() == 1
    page.click("#reload")
    page.wait_for_function(
        "() => !document.querySelector('input[data-role=\"item\"][data-id=\"b1\"]')"
    )


def test_confirm_message_names_the_count_and_the_playlist(editor, open_playlist):
    """The confirm text is the last thing between you and an irreversible action, and nothing
    asserted what it actually says."""
    page = open_playlist(editor)
    seen = []
    page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_function("() => true")

    assert seen, "no confirm dialog appeared before a destructive action"
    msg = seen[0]
    assert "5" in msg
    assert fx.NAMES[fx.CASE_SET_ID] in msg
    assert "cannot be undone" in msg.lower()


def test_warnings_clear_on_a_later_successful_removal(make_editor, open_playlist):
    """A stale red error block after a subsequent success would misreport the state."""
    editor = make_editor(fail_after=1)
    page = open_playlist(editor)
    page.on("dialog", lambda d: d.accept())

    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    page.click("#remove")
    page.wait_for_selector("#warnings:not(.d-none)")

    editor.fake._fail_after = None          # next removal succeeds
    # Wait for the status to *change*, not merely to match /^Removed /: the failed removal already
    # says "Removed 1 of 5; 4 failed", so a pattern wait returns instantly and asserts nothing.
    first_status = page.locator("#status").inner_text()
    page.locator('input[data-role="item"]').first.check()
    page.click("#remove")
    page.wait_for_function(
        "prev => document.querySelector('#status').textContent.trim() !== prev", arg=first_status
    )
    assert page.locator("#warnings").is_hidden(), "stale warnings survived a successful removal"


# --- what the page does when Jellyfin is unreachable --------------------------


def test_playlist_list_failure_is_reported(make_editor, page):
    """Until FakeJellyfinClient grew load_error, list_playlists could not fail, so this branch was
    unreachable from a test rather than merely untested."""
    from media_sync_manager.errors import TransientError

    editor = make_editor(load_error=TransientError("jellyfin GET /Users/u/Items failed: boom"))
    page.goto(editor.url, wait_until="networkidle")
    page.wait_for_function("() => document.querySelector('#status').classList.contains('error')")

    status = page.locator("#status").inner_text()
    assert "Could not load playlists" in status and "boom" in status
    assert page.locator("#toolbar").is_hidden(), "the toolbar must stay hidden with nothing loaded"


def test_playlist_items_failure_tells_you_how_to_retry(make_editor, page):
    """A dead server mid-session must not leave the page silently blank."""
    from media_sync_manager.errors import TransientError

    editor = make_editor()
    page.goto(editor.url, wait_until="networkidle")
    editor.fake._load_error = TransientError("jellyfin GET /Playlists/x/Items failed: boom")
    page.select_option("#playlist", label=fx.NAMES[fx.CASE_SET_ID])
    page.wait_for_function("() => document.querySelector('#status').classList.contains('error')")

    status = page.locator("#status").inner_text()
    assert "Could not load this playlist" in status
    assert "↻" in status or "retry" in status.lower(), "no recovery hint offered"
    assert page.locator("li.list-group-item").count() == 0, "a stale tree was left on screen"
