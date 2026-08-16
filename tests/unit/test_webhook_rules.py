"""
Unit tests for PSP webhook admission.

Layer 1 is the only one of the four that can reject a request before it costs
anything, so most of these are attempts to get past it: no signature, a
signature for a different body, a signature for a different secret, a genuine
signature replayed later, a header shaped to confuse the parser.
"""

from conftest import webhook_rules

Admission = webhook_rules.Admission
verify_signature = webhook_rules.verify_signature
parse_signature_header = webhook_rules.parse_signature_header
expected_signature = webhook_rules.expected_signature
dedup_key = webhook_rules.dedup_key

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_123","type":"payment_intent.succeeded","amount_cents":1500}'
NOW = 1_755_000_000


def header_for(body=BODY, secret=SECRET, timestamp=NOW):
    return f"t={timestamp},{webhook_rules.SIGNATURE_SCHEME}=" \
           f"{expected_signature(secret, timestamp, body)}"


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_a_genuine_webhook_is_accepted():
    check = verify_signature(SECRET, BODY, header_for(), NOW)
    assert check.accepted
    assert check.timestamp == NOW


def test_acceptance_survives_clock_skew_within_tolerance():
    for skew in (-299, -1, 0, 1, 299):
        check = verify_signature(SECRET, BODY, header_for(timestamp=NOW - skew), NOW)
        assert check.accepted, f"skew of {skew}s should be tolerated"


# ---------------------------------------------------------------------------
# forgery
# ---------------------------------------------------------------------------

def test_missing_header_is_rejected():
    for header in (None, ""):
        assert verify_signature(SECRET, BODY, header, NOW).admission \
            is Admission.MISSING_SIGNATURE


def test_a_signature_from_a_different_secret_is_rejected():
    forged = header_for(secret="whsec_the_wrong_secret")
    assert verify_signature(SECRET, BODY, forged, NOW).admission \
        is Admission.BAD_SIGNATURE


def test_a_signature_for_a_different_body_is_rejected():
    # The interesting attack: take a real signed webhook and change the amount.
    header = header_for(body=BODY)
    tampered = BODY.replace(b'1500', b'150000')
    assert verify_signature(SECRET, tampered, header, NOW).admission \
        is Admission.BAD_SIGNATURE


def test_changing_the_timestamp_invalidates_the_signature():
    # The timestamp is inside the signed material, which is what stops an
    # attacker from moving an old capture into the tolerance window.
    genuine = expected_signature(SECRET, NOW - 10_000, BODY)
    moved = f"t={NOW},v1={genuine}"
    assert verify_signature(SECRET, BODY, moved, NOW).admission \
        is Admission.BAD_SIGNATURE


def test_an_empty_signature_value_is_malformed_not_accepted():
    assert verify_signature(SECRET, BODY, f"t={NOW},v1=", NOW).admission \
        is Admission.MALFORMED_SIGNATURE


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def test_an_old_but_genuine_webhook_is_stale():
    # Signature verifies; that is exactly why the freshness check has to exist
    # separately. A captured request stays valid forever without it.
    old = header_for(timestamp=NOW - 3600)
    check = verify_signature(SECRET, BODY, old, NOW)
    assert check.admission is Admission.STALE


def test_a_future_timestamp_beyond_tolerance_is_stale():
    # Either a broken clock or an attempt to mint something replayable later.
    future = header_for(timestamp=NOW + 3600)
    assert verify_signature(SECRET, BODY, future, NOW).admission is Admission.STALE


def test_the_tolerance_boundary_is_inclusive():
    at_edge = header_for(timestamp=NOW - webhook_rules.DEFAULT_TOLERANCE_SECONDS)
    assert verify_signature(SECRET, BODY, at_edge, NOW).accepted

    past_edge = header_for(timestamp=NOW - webhook_rules.DEFAULT_TOLERANCE_SECONDS - 1)
    assert verify_signature(SECRET, BODY, past_edge, NOW).admission is Admission.STALE


def test_tolerance_is_configurable():
    old = header_for(timestamp=NOW - 3600)
    assert verify_signature(SECRET, BODY, old, NOW, tolerance=7200).accepted


# ---------------------------------------------------------------------------
# header parsing
# ---------------------------------------------------------------------------

def test_header_parses_timestamp_and_signature():
    ts, sig = parse_signature_header("t=123,v1=abcdef")
    assert (ts, sig) == (123, "abcdef")


def test_unknown_scheme_versions_are_ignored_not_fatal():
    # A PSP adding v2 alongside v1 must not break this handler.
    ts, sig = parse_signature_header("t=123,v1=abcdef,v2=ffffff")
    assert (ts, sig) == (123, "abcdef")


def test_whitespace_is_tolerated():
    ts, sig = parse_signature_header(" t=123 , v1=abcdef ")
    assert (ts, sig) == (123, "abcdef")


def test_a_non_numeric_timestamp_is_none_not_zero():
    # Zero would be a timestamp in 1970 and would be rejected as stale, which
    # reports the wrong reason and sends whoever debugs it after a clock.
    ts, sig = parse_signature_header("t=not-a-number,v1=abcdef")
    assert ts is None


def test_garbage_headers_are_malformed():
    for header in ("garbage", "v1=abcdef", f"t={NOW}", "t=,v1=", "=,="):
        assert verify_signature(SECRET, BODY, header, NOW).admission in (
            Admission.MALFORMED_SIGNATURE, Admission.MISSING_SIGNATURE
        ), f"{header!r} was not refused"


def test_only_the_v1_signature_is_trusted():
    # A v2-only header must not be accepted by falling back to something else.
    header = f"t={NOW},v2={expected_signature(SECRET, NOW, BODY)}"
    assert verify_signature(SECRET, BODY, header, NOW).admission \
        is Admission.MALFORMED_SIGNATURE


# ---------------------------------------------------------------------------
# dedup key
# ---------------------------------------------------------------------------

def test_dedup_keys_are_namespaced_and_distinct():
    assert dedup_key("evt_1").startswith("webhook:seen:")
    assert dedup_key("evt_1") != dedup_key("evt_2")


def test_the_dedup_window_outlasts_a_psp_retry_schedule():
    # Retries are typically spread over three days; remembering for seven means
    # a retry always lands while the original is still known.
    assert webhook_rules.DEDUP_TTL_SECONDS >= 3 * 24 * 60 * 60
