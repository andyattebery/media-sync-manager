"""Selection mechanics in a real DOM.

Every test here backs a guarantee stated elsewhere in the design. The first two exist because plan
review found both bugs in prose and neither is visible to a test that does not drive a browser.
"""

from __future__ import annotations

import pytest

from . import fixtures as fx

pytestmark = pytest.mark.e2e


def indeterminate(page, selector):
    return page.locator(selector).evaluate("e => e.indeterminate")


def item_boxes(page, season_key):
    return page.locator(f'.season-group[data-key="{season_key}"] input[data-role="item"]')


def all_checked(locator):
    return all(locator.nth(i).is_checked() for i in range(locator.count()))


def test_season_box_checks_every_episode_beneath_it(editor, open_playlist):
    """The itemBoxes registry: without it state.selected updates and the group boxes move, but the
    episode rows never visually change — the headline interaction, silently broken."""
    page = open_playlist(editor)
    page.check(f'input[data-key="{fx.MEADOW_S1_KEY}"]')

    boxes = item_boxes(page, fx.MEADOW_S1_KEY)
    assert boxes.count() == 5
    assert all_checked(boxes)

    page.uncheck(f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    assert not any(boxes.nth(i).is_checked() for i in range(boxes.count()))


def test_master_checkbox_goes_indeterminate(editor, open_playlist):
    """The master has no data-key and no bucket, so it is not in either registry and is easy to
    leave out of the sync loop entirely."""
    page = open_playlist(editor)
    page.locator(f'.season-group[data-key="{fx.MEADOW_S1_KEY}"] input[data-role="item"]').first.check()

    assert indeterminate(page, f'input[data-key="{fx.MEADOW_S1_KEY}"]')
    assert indeterminate(page, f'input[data-key="{fx.MEADOW_KEY}"]')
    assert indeterminate(page, "#select-all")


def test_select_all_then_untick_one_returns_to_fully_checked(editor, open_playlist):
    """Catches a missing `cb.indeterminate =` assignment: the browser keeps indeterminate
    independent of checked, so a stale true survives and paints a permanent dash."""
    page = open_playlist(editor)
    page.check("#select-all")
    assert page.locator("#select-all").is_checked()
    assert not indeterminate(page, "#select-all")

    first = page.locator('input[data-role="item"]').first
    first.uncheck()
    assert indeterminate(page, "#select-all")

    first.check()
    assert page.locator("#select-all").is_checked()
    assert not indeterminate(page, "#select-all"), "stale indeterminate survived re-checking"


def test_upto_selects_the_prefix_within_its_season_only(editor, open_playlist):
    page = open_playlist(editor)
    page.click('button[data-role="upto"][data-id="c3"]')  # S02E03 Ninepin

    s2 = item_boxes(page, fx.MEADOW_S2_KEY)
    checked = [s2.nth(i).get_attribute("data-id") for i in range(s2.count()) if s2.nth(i).is_checked()]
    # c1 appears twice (duplicate) and both rows select; nothing from season 1 is touched.
    assert set(checked) == {"c1", "c2", "c3"}
    assert not any(
        item_boxes(page, fx.MEADOW_S1_KEY).nth(i).is_checked()
        for i in range(item_boxes(page, fx.MEADOW_S1_KEY).count())
    )


def test_upto_again_clears_exactly_that_prefix(editor, open_playlist):
    page = open_playlist(editor)
    page.click('button[data-role="upto"][data-id="c3"]')
    page.click('button[data-role="upto"][data-id="c3"]')
    assert page.locator("#counts").inner_text().startswith("0 of ")


def test_duplicate_rows_move_together(editor, open_playlist):
    """Both rows share an entry_id, so itemBoxes must map to a LIST of elements, not one.

    Selects via the season checkbox, not by clicking either row. Clicking a row checks it natively,
    so a one-element registry would still leave both rows checked — the clicked one by the browser,
    the other by syncCheckboxes — and this test survived exactly that mutation until it drove the
    selection from a path that touches neither row directly.
    """
    page = open_playlist(editor)
    rows = page.locator('input[data-role="item"][data-id="c1"]')
    assert rows.count() == 2

    page.check(f'input[data-key="{fx.MEADOW_S2_KEY}"]')
    assert rows.first.is_checked() and rows.nth(1).is_checked(), "a duplicate row stayed stale"

    page.uncheck(f'input[data-key="{fx.MEADOW_S2_KEY}"]')
    assert not rows.first.is_checked() and not rows.nth(1).is_checked()


def test_unaddressable_entry_is_disabled_and_has_no_upto(editor, open_playlist):
    page = open_playlist(editor)
    broken = page.locator('input[data-role="item"][data-id=""]')
    assert broken.count() == 1 and broken.is_disabled()
    assert page.locator('button[data-role="upto"][data-id=""]').count() == 0


def test_toolbar_hidden_until_a_playlist_is_chosen(editor, page):
    """A `display:` beating the `hidden` attribute is the classic cascade bug here."""
    page.goto(editor.url, wait_until="networkidle")
    assert page.locator("#toolbar").is_hidden()


def test_switching_playlists_clears_the_selection(editor, open_playlist):
    """Exactly the bug class review found in prose: a structure reset on one code path and
    forgotten on another."""
    page = open_playlist(editor)
    page.check("#select-all")
    assert not page.locator("#counts").inner_text().startswith("0 of ")

    page.select_option("#playlist", label=fx.NAMES[fx.BULK_ID])
    page.wait_for_function("() => document.querySelectorAll('li.list-group-item').length > 100")
    assert page.locator("#counts").inner_text() == "0 of 120 selected"
    assert not page.locator("#select-all").is_checked()


def test_selection_never_re_renders_the_tree(editor, page, open_playlist):
    """'Selection never re-renders' is a design guarantee: ticking a box must not rebuild the tree,
    which would reset both scroll position and which groups are expanded."""
    # Short viewport so the page genuinely scrolls; collapsing a group first would make it too
    # short to scroll at all, which silently turns the scroll assertion into 0 == 0.
    page.set_viewport_size({"width": 1280, "height": 400})
    open_playlist(editor)

    collapse = page.locator(f'.show-group[data-key="{fx.MEADOW_KEY}"] > .accordion-collapse')
    assert "show" in (collapse.get_attribute("class") or ""), "expected Meadowlark expanded to start"

    # behavior:'instant' because Bootstrap sets `scroll-behavior: smooth` on :root — a plain
    # scrollTo animates, so reading scrollY on the next line would return 0 and quietly reduce the
    # assertion below to 0 == 0.
    page.evaluate("window.scrollTo({top: 200, behavior: 'instant'})")
    before = page.evaluate("window.scrollY")
    assert before > 0, "page must actually be scrollable for this assertion to mean anything"

    # Click via JS: Playwright's .check() scrolls the element into view, which would itself move
    # the page and make the assertion below meaningless.
    page.locator('input[data-role="item"]').first.evaluate("e => e.click()")

    assert page.evaluate("window.scrollY") == before
    assert "show" in (collapse.get_attribute("class") or ""), "expansion was lost"


def test_titles_render_as_text_not_markup(editor, open_playlist):
    """Episode titles are arbitrary strings from a media library."""
    page = open_playlist(editor)
    assert page.locator("img").count() == 0
    assert page.get_by_text('<img src=x onerror="alert(1)"> & "quotes"').count() == 1


# --- the Bootstrap revision: regressions for what was actually reported --------


def test_range_button_states_the_count_and_has_a_title(editor, open_playlist):
    """The old control was a bare ⤒ glyph with an aria-label and NO title, so a mouse user got
    nothing on hover and had to click an unlabelled button on a page that deletes things."""
    page = open_playlist(editor)
    btn = page.locator('button[data-role="upto"][data-id="b3"]')   # 3rd of Meadowlark season 1
    assert btn.inner_text().strip() == "Select first 3"
    assert (btn.get_attribute("title") or "").startswith("Select this item and every one above it")


def test_range_button_flips_to_clear_when_the_range_is_selected(editor, open_playlist):
    page = open_playlist(editor)
    btn = page.locator('button[data-role="upto"][data-id="b3"]')
    btn.click()
    assert btn.inner_text().strip() == "Clear first 3"
    btn.click()
    assert btn.inner_text().strip() == "Select first 3"


def test_range_label_works_outside_episodes(editor, open_playlist):
    """Movies have no IndexNumber, so an 'E1–E8' label would be meaningless there. A count is not."""
    page = open_playlist(editor)
    btn = page.locator('button[data-role="upto"][data-id="m1"]')   # a movie
    assert btn.inner_text().strip().startswith("Select first ")
    btn.click()
    assert btn.inner_text().strip().startswith("Clear first ")


def test_range_label_counts_rows_not_episode_numbers(editor, open_playlist):
    """Meadowlark season 2 holds E1–E4 plus a number-less 'Loose Episode' that sorts last, and E1 is
    duplicated. An episode-range label would claim 'E1–E4' while ticking a different number of
    rows; the count must match what is actually selected."""
    page = open_playlist(editor)
    loose = page.locator('button[data-role="upto"][data-id="loose"]')
    claimed = int(loose.inner_text().strip().rsplit(" ", 1)[1])
    loose.click()
    selected = int(page.locator("#counts").inner_text().split(" of ")[0])
    assert claimed == selected


@pytest.mark.parametrize("role", ["item", "season", "show"])
def test_checkbox_is_not_oversized_on_desktop(editor, page, open_playlist, role):
    """The reported defect. The previous framework scaled the root font to 125% at this width and
    sized its checkbox at 1.25em, rendering 25px.

    Parametrised over all three roles deliberately: an earlier version of this test only measured
    item checkboxes and passed while the show/season boxes rendered at 32px, because .form-check-input
    is 1em and .accordion-header is an <h2>. Checking one role proved nothing about the others."""
    page.set_viewport_size({"width": 1280, "height": 900})
    open_playlist(editor)
    box = page.locator(f'input[data-role="{role}"]').first.bounding_box()
    assert 14 <= box["width"] <= 18, f"{role} checkbox is {box['width']}px wide"
    assert 14 <= box["height"] <= 18, f"{role} checkbox is {box['height']}px tall"


def test_header_links_to_the_jellyfin_instance(editor, page):
    """The page used to say only 'Playlist editor' — no server, no link, no explanation."""
    page.goto(editor.url, wait_until="networkidle")
    link = page.locator("#server-link")
    link.wait_for(state="visible")
    assert link.get_attribute("href") == "http://jf.test/web/"
    assert "jf.test" in link.inner_text()
    body = page.locator("body").inner_text()
    assert "transcoded" in body and "Originals are never touched" in body


def test_status_line_names_shows_not_generic_groups(editor, open_playlist, page):
    """'154 items in 1 group' leaked the JSON key into the UI, and 'group' meant two different
    things (a TV show, or a type bucket like Movies)."""
    page_ = open_playlist(editor)
    status = page_.locator("#status").inner_text()
    assert "group" not in status.lower(), f"jargon leaked: {status!r}"
    # The case-set fixture holds Northwind + Meadowlark, plus the Movies and no-series buckets.
    assert "2 shows" in status
    assert "Movies" in status


def test_status_line_on_a_single_show_playlist(editor, open_playlist, page):
    page_ = open_playlist(editor, fx.NAMES[fx.BULK_ID])
    assert page_.locator("#status").inner_text() == "120 items in 1 show."


# --- the accordion: newest code, and previously untested entirely -------------


def expanded(page, key):
    cls = page.locator(f'[data-key="{key}"] > .accordion-collapse').get_attribute("class") or ""
    return "show" in cls


def toggle_group(page, key):
    """Click the disclosure button and wait for Bootstrap's transition to settle."""
    collapse = page.locator(f'[data-key="{key}"] > .accordion-collapse')
    was = expanded(page, key)
    page.locator(f'[data-key="{key}"] > .accordion-header > .accordion-button').click()
    collapse.evaluate(
        """(e, was) => new Promise(done => {
             const ev = was ? 'hidden.bs.collapse' : 'shown.bs.collapse';
             e.addEventListener(ev, () => done(), {once: true});
           })""",
        was,
    )


def test_accordion_expands_and_collapses(editor, open_playlist):
    page = open_playlist(editor)
    assert expanded(page, fx.MEADOW_S1_KEY), "case-set fixture is small, seasons start open"

    toggle_group(page, fx.MEADOW_S1_KEY)
    assert not expanded(page, fx.MEADOW_S1_KEY)
    assert item_boxes(page, fx.MEADOW_S1_KEY).first.is_hidden()

    toggle_group(page, fx.MEADOW_S1_KEY)
    assert expanded(page, fx.MEADOW_S1_KEY)
    assert item_boxes(page, fx.MEADOW_S1_KEY).first.is_visible()


def test_header_checkbox_and_disclosure_are_independent(editor, open_playlist):
    """The old markup put a checkbox inside a <summary> and needed stopPropagation(). Bootstrap
    removes that structurally — .accordion-button is a <button>, so the checkbox is a sibling.
    Assert it, because 'structurally impossible' is exactly the kind of claim that rots."""
    page = open_playlist(editor)
    box = page.locator(f'input[data-key="{fx.MEADOW_S1_KEY}"]')

    was_open = expanded(page, fx.MEADOW_S1_KEY)
    box.click()
    assert box.is_checked(), "ticking the box did nothing"
    assert expanded(page, fx.MEADOW_S1_KEY) is was_open, "ticking the box also toggled the accordion"

    toggle_group(page, fx.MEADOW_S1_KEY)
    assert box.is_checked(), "collapsing the group cleared its selection"


def test_expansion_survives_a_removal_refresh(editor, open_playlist):
    """render() re-reads expansion from the DOM instead of tracking Bootstrap collapse events. If
    that broke, every removal would silently collapse the whole tree — very visible on a 154-item
    playlist, and nothing else in the suite would catch it."""
    page = open_playlist(editor)
    page.on("dialog", lambda d: d.accept())

    toggle_group(page, fx.MEADOW_S2_KEY)                 # collapse season 2
    assert expanded(page, fx.MEADOW_S1_KEY) and not expanded(page, fx.MEADOW_S2_KEY)

    # Remove ONE item, not the whole season: emptying a season removes the group from the tree
    # entirely, and then the assertions below would be checking an element that no longer exists.
    item_boxes(page, fx.MEADOW_S1_KEY).first.check()
    page.click("#remove")
    page.wait_for_function("() => /^Removed /.test(document.querySelector('#status').textContent)")

    assert expanded(page, fx.MEADOW_KEY), "show collapsed itself after the refresh"
    assert expanded(page, fx.MEADOW_S1_KEY), "expanded season collapsed after the refresh"
    assert not expanded(page, fx.MEADOW_S2_KEY), "collapsed season re-opened after the refresh"


def test_large_playlist_opens_to_season_headers_only(editor, open_playlist):
    """Tuned against the real 154-episode playlist: opening everything buries the season headers
    you actually act on under 154 rows."""
    page = open_playlist(editor, fx.NAMES[fx.BULK_ID])
    show = page.locator(".show-group").first.get_attribute("data-key")
    season = page.locator(".season-group").first.get_attribute("data-key")
    assert expanded(page, show), "the show should open so its seasons are visible"
    assert not expanded(page, season), "seasons should stay closed above SMALL_PLAYLIST"
    assert page.locator("li.list-group-item:visible").count() == 0


def test_show_checkbox_selects_across_every_season(editor, open_playlist):
    """Only season -> items was covered; idsByShow spans several seasons and had no direct test."""
    page = open_playlist(editor)
    page.check(f'input[data-key="{fx.MEADOW_KEY}"]')

    for key in (fx.MEADOW_S1_KEY, fx.MEADOW_S2_KEY):
        boxes = item_boxes(page, key)
        assert boxes.count() > 0 and all_checked(boxes), f"{key} not fully selected"
    assert page.locator(f'input[data-key="{fx.MEADOW_S1_KEY}"]').is_checked()
    # 12, not the 13 on Meadowlark's badge. The badge counts *rows* (grouping renders the playlist
    # faithfully, duplicates included); selection counts *entries*, and the duplicated episode is
    # one entry Jellyfin removes both copies of. Deliberate, and consistent with the range button.
    assert page.locator("#counts").inner_text().startswith("12 of ")


def test_episode_code_column_handles_missing_numbers(editor, open_playlist):
    """SxxEyy is derived, not sent. Three shapes exist and only the happy one was covered."""
    page = open_playlist(editor)
    row = lambda eid: page.locator(f'li:has(input[data-id="{eid}"])')  # noqa: E731
    assert "S01E01" in row("b1").inner_text()          # both numbers
    assert "—" in row("loose").inner_text()            # episode number missing
    # A movie has no episode code at all rather than a placeholder.
    assert row("m1").locator(".font-monospace").count() == 0


def test_duplicate_row_is_badged(editor, open_playlist):
    """Removing one copy removes both; the row says so rather than surprising you."""
    page = open_playlist(editor)
    badges = page.locator('li:has(input[data-id="c1"]) .badge')
    assert badges.count() == 2, "expected the ×2 badge on both duplicate rows"
    assert badges.first.inner_text() == "×2"


# --- keyboard: the whole flow must work without a mouse -----------------------


def test_space_toggles_an_item_checkbox(editor, open_playlist):
    page = open_playlist(editor)
    box = page.locator('input[data-role="item"][data-id="b1"]')
    box.focus()
    page.keyboard.press("Space")
    assert box.is_checked()
    assert page.locator("#counts").inner_text().startswith("1 of ")
    page.keyboard.press("Space")
    assert not box.is_checked()


def test_space_on_a_group_checkbox_selects_the_whole_group(editor, open_playlist):
    page = open_playlist(editor)
    page.locator(f'input[data-key="{fx.MEADOW_S1_KEY}"]').focus()
    page.keyboard.press("Space")
    assert all_checked(item_boxes(page, fx.MEADOW_S1_KEY))


def test_keyboard_can_expand_and_collapse_a_group(editor, open_playlist):
    """The disclosure is a real <button>, so Enter must work — a div with a click handler would
    look identical to a mouse user and be unreachable without one."""
    page = open_playlist(editor)
    collapse = page.locator(f'[data-key="{fx.MEADOW_S1_KEY}"] > .accordion-collapse')
    page.locator(f'[data-key="{fx.MEADOW_S1_KEY}"] > .accordion-header > .accordion-button').focus()

    page.keyboard.press("Enter")
    collapse.evaluate("e => new Promise(d => e.addEventListener('hidden.bs.collapse', d, {once:true}))")
    assert not expanded(page, fx.MEADOW_S1_KEY)

    page.keyboard.press("Enter")
    collapse.evaluate("e => new Promise(d => e.addEventListener('shown.bs.collapse', d, {once:true}))")
    assert expanded(page, fx.MEADOW_S1_KEY)


def test_keyboard_can_trigger_the_range_button(editor, open_playlist):
    page = open_playlist(editor)
    page.locator('button[data-role="upto"][data-id="b3"]').focus()
    page.keyboard.press("Enter")
    assert page.locator("#counts").inner_text().startswith("3 of ")


def test_tab_reaches_the_range_button_from_its_own_row(editor, open_playlist):
    """Row order must be navigable: checkbox, then that row's own action — not a jump elsewhere."""
    page = open_playlist(editor)
    page.locator('input[data-role="item"][data-id="b1"]').focus()
    page.keyboard.press("Tab")
    focused = page.evaluate("() => [document.activeElement.dataset.role, document.activeElement.dataset.id]")
    assert focused == ["upto", "b1"]


def test_disabled_row_is_not_reachable_by_keyboard(editor, open_playlist):
    """The unaddressable entry cannot be acted on, so it must not be a tab stop that does nothing."""
    page = open_playlist(editor)
    assert page.evaluate(
        """() => {
             const el = document.querySelector('input[data-role="item"][data-id=""]');
             el.focus();
             return document.activeElement !== el;   // disabled inputs refuse focus
           }"""
    )


# --- branches nothing reached before ------------------------------------------


def test_selecting_a_collapsed_season_is_the_primary_use_case(editor, open_playlist):
    """The real playlist is 154 items, so seasons arrive COLLAPSED. Every other season-selection
    test uses the small fixture where they are already open — i.e. never the shape that matters.
    The checkbox lives in the accordion header and is visible regardless; the rows are rendered but
    hidden, so the buckets must be populated anyway."""
    page = open_playlist(editor, fx.NAMES[fx.BULK_ID])
    season = page.locator(".season-group").first
    key = season.get_attribute("data-key")
    assert not expanded(page, key), "expected the season collapsed on a 120-item playlist"
    assert season.locator("li.list-group-item").first.is_hidden()

    page.check(f'input[data-key="{key}"]')
    assert page.locator("#counts").inner_text() == "120 of 120 selected"
    assert page.locator("#remove").inner_text().strip() == "Remove selected (120)"


def test_many_groups_start_collapsed(editor, open_playlist):
    """MANY_GROUPS: above 15 groups even the shows stay shut, so you land on a scannable index
    rather than a wall. No other fixture exceeds 4 groups, so this branch had never executed."""
    page = open_playlist(editor, fx.NAMES[fx.WIDE_ID])
    shows = page.locator(".show-group")
    assert shows.count() == 21
    assert not any(expanded(page, shows.nth(i).get_attribute("data-key")) for i in range(shows.count()))
    assert page.locator("li.list-group-item:visible").count() == 0


def test_status_line_counts_sections_when_there_are_too_many_to_name(editor, open_playlist):
    """summarise() lists type buckets by name at one or two, and counts them beyond that."""
    page = open_playlist(editor, fx.NAMES[fx.WIDE_ID])
    assert page.locator("#status").inner_text() == "21 items in 18 shows and 3 other sections."


def test_case_set_total_is_pinned(editor, open_playlist):
    """20 rows, 18 selectable: one unaddressable entry is excluded and one duplicate collapses.

    Nothing asserted this total, which meant indexing unaddressable entries into the buckets — a
    real bug — would have changed 18 to 19 with every test still green."""
    page = open_playlist(editor)
    assert page.locator("#counts").inner_text() == "0 of 18 selected"
    assert page.locator("li.list-group-item").count() == 20


def test_episode_code_uses_a_placeholder_when_the_season_number_is_missing(editor, open_playlist):
    """S??Exx: the episode number is known, the season is not. Neither existing code test hit it."""
    page = open_playlist(editor)
    row = page.locator('li:has(input[data-id="nosnum"])')
    assert "S??E05" in row.inner_text(), row.inner_text()
    # and the two shapes that already worked, so the three are asserted together
    assert "S03E07" in page.locator('li:has(input[data-id="orphan"])').inner_text()
    assert "—" in page.locator('li:has(input[data-id="loose"])').inner_text()
