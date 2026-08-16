"""
Unit tests for the media lifecycle.

The property that matters is negative: there is no sequence of calls that makes
an asset servable without a completed clean scan. Most of these tests exist to
try to find one -- by skipping the scan, by re-sending a verdict, by asking a
scanner for a second opinion after an infected result.
"""

import pytest

from conftest import media_rules

MediaStatus = media_rules.MediaStatus
Outcome = media_rules.Outcome
is_servable = media_rules.is_servable
plan_registration = media_rules.plan_registration
plan_scan_start = media_rules.plan_scan_start
plan_scan_result = media_rules.plan_scan_result
plan_delete = media_rules.plan_delete


# ---------------------------------------------------------------------------
# servability — a whitelist of one
# ---------------------------------------------------------------------------

def test_only_clean_is_servable():
    assert is_servable(MediaStatus.CLEAN) is True
    for status in (MediaStatus.QUARANTINED, MediaStatus.SCANNING,
                   MediaStatus.INFECTED, MediaStatus.DELETED):
        assert is_servable(status) is False, f"{status} must not be servable"


def test_unknown_statuses_are_not_servable():
    # A status this code does not recognise -- written by an older version, or
    # edited by hand -- must fail closed rather than be assumed harmless.
    for status in (None, "", "CLEAN ", "ok", "published", 0, object()):
        assert is_servable(status) is False, f"{status!r} must not be servable"


def test_the_string_value_is_accepted():
    # Rows come back from the database as plain strings, not enum members.
    assert is_servable("clean") is True
    assert is_servable("quarantined") is False


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_registration_quarantines_by_default():
    d = plan_registration("cat.png", "image/png", 1024)
    assert d.ok
    assert d.new_status is MediaStatus.QUARANTINED
    assert d.event == media_rules.EVENT_REGISTERED
    # The whole point: freshly registered media is not servable.
    assert is_servable(d.new_status) is False


def test_disallowed_content_type_is_rejected():
    # Default-deny: the danger is not the extension but everything not on the
    # list, which is why this asserts a rejection rather than a specific type.
    for content_type in ("application/x-msdownload", "text/html",
                         "application/octet-stream", "image/svg+xml", ""):
        d = plan_registration("thing", content_type, 1024)
        assert d.outcome is Outcome.INVALID, f"{content_type} should be refused"


def test_allowed_content_types_pass():
    for content_type in sorted(media_rules.ALLOWED_CONTENT_TYPES):
        assert plan_registration("f", content_type, 10).ok


def test_missing_filename_is_rejected():
    for filename in ("", "   ", None):
        assert plan_registration(filename, "image/png", 10).outcome is Outcome.INVALID


@pytest.mark.parametrize("size", [0, -1, media_rules.MAX_BYTES + 1])
def test_bad_sizes_are_rejected(size):
    assert plan_registration("f.png", "image/png", size).outcome is Outcome.INVALID


def test_the_size_limit_itself_is_allowed():
    assert plan_registration("f.png", "image/png", media_rules.MAX_BYTES).ok


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

def test_scan_can_start_from_quarantine():
    d = plan_scan_start(MediaStatus.QUARANTINED)
    assert d.ok and d.new_status is MediaStatus.SCANNING


def test_starting_a_scan_twice_is_not_an_error():
    # The scanner retrying is ordinary. Answering a conflict would make an
    # honest retry look like a fault, the same mistake the idempotency contract
    # in Rule 4 exists to prevent.
    d = plan_scan_start(MediaStatus.SCANNING)
    assert d.ok and d.new_status is MediaStatus.SCANNING
    assert d.event is None, "a repeated start must not emit a second event"


def test_rescanning_clean_media_revokes_servability():
    # A re-scan implies doubt, so the asset stops being servable until the new
    # verdict lands rather than staying public on the strength of the old one.
    d = plan_scan_start(MediaStatus.CLEAN)
    assert d.ok and d.new_status is MediaStatus.SCANNING
    assert is_servable(d.new_status) is False


def test_infected_media_cannot_be_rescanned():
    assert plan_scan_start(MediaStatus.INFECTED).outcome is Outcome.NOT_ALLOWED


def test_deleted_media_cannot_be_rescanned():
    assert plan_scan_start(MediaStatus.DELETED).outcome is Outcome.NOT_ALLOWED


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

def test_clean_verdict_publishes():
    d = plan_scan_result(MediaStatus.SCANNING, infected=False)
    assert d.ok
    assert d.new_status is MediaStatus.CLEAN
    assert d.event == media_rules.EVENT_PUBLISHED
    assert is_servable(d.new_status) is True


def test_infected_verdict_quarantines():
    d = plan_scan_result(MediaStatus.SCANNING, infected=True)
    assert d.ok
    assert d.new_status is MediaStatus.INFECTED
    assert d.event == media_rules.EVENT_QUARANTINED
    assert is_servable(d.new_status) is False


def test_a_verdict_without_a_scan_is_refused():
    # Otherwise the scanner callback is a way to set status directly: post a
    # clean verdict at a quarantined asset and skip scanning entirely.
    for status in (MediaStatus.QUARANTINED, MediaStatus.CLEAN):
        d = plan_scan_result(status, infected=False)
        assert d.outcome is Outcome.NOT_ALLOWED, f"clean verdict accepted from {status}"


def test_infected_is_terminal_against_a_later_clean_verdict():
    # The attack this blocks: rescan until the scanner says what you want.
    d = plan_scan_result(MediaStatus.INFECTED, infected=False)
    assert d.outcome is Outcome.NOT_ALLOWED
    assert d.new_status is None


def test_repeating_an_infected_verdict_is_a_no_op():
    # Duplicate delivery of the same verdict is not a fault.
    d = plan_scan_result(MediaStatus.INFECTED, infected=True)
    assert d.ok
    assert d.new_status is MediaStatus.INFECTED
    assert d.event is None


def test_a_verdict_cannot_revive_deleted_media():
    for infected in (True, False):
        assert plan_scan_result(MediaStatus.DELETED, infected).outcome is Outcome.NOT_ALLOWED


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------

def test_delete_from_any_live_state():
    for status in (MediaStatus.QUARANTINED, MediaStatus.SCANNING,
                   MediaStatus.CLEAN, MediaStatus.INFECTED):
        d = plan_delete(status)
        assert d.ok and d.new_status is MediaStatus.DELETED


def test_deleting_twice_is_a_no_op():
    d = plan_delete(MediaStatus.DELETED)
    assert d.ok
    assert d.event is None, "a repeated delete must not emit a second event"


# ---------------------------------------------------------------------------
# the property, stated directly
# ---------------------------------------------------------------------------

def test_no_route_to_servable_skips_a_clean_scan():
    """Walk every transition and check what reaches CLEAN.

    A single assertion over the whole table, so a transition added later
    without thinking about servability fails here rather than in production.
    """
    statuses = list(MediaStatus)
    reached_clean = []

    for status in statuses:
        for name, decision in (
            ("scan_start", plan_scan_start(status)),
            ("verdict_clean", plan_scan_result(status, infected=False)),
            ("verdict_infected", plan_scan_result(status, infected=True)),
            ("delete", plan_delete(status)),
        ):
            if decision.ok and decision.new_status is MediaStatus.CLEAN:
                reached_clean.append((status, name))

    assert reached_clean == [(MediaStatus.SCANNING, "verdict_clean")], (
        f"something other than a clean verdict during a scan reached CLEAN: "
        f"{reached_clean}")
