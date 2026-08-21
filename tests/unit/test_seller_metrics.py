"""
Unit tests for seller performance metrics.

One rule carries this file: **small samples must not produce extreme scores.**

A seller with one order that was returned has a 100% return rate. Ranking them
last forever on one data point is not evidence, it is noise, and it makes a new
seller's first bad day permanent. Every rate is therefore shrunk toward a prior
in proportion to how little is known, and most of these tests exist to check
that the shrinkage actually bites where it should and gets out of the way where
it should not.

The second theme is what counts as the seller's fault. An order a buyer
cancelled before the seller ever saw it is not the seller's failure, and
counting it would punish sellers for being in categories buyers browse
indecisively.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import seller_metrics

compute_metrics = seller_metrics.compute_metrics
shrink = seller_metrics.shrink
hours_between = seller_metrics.hours_between
quality_inputs = seller_metrics.quality_inputs
PRIOR_WEIGHT = seller_metrics.PRIOR_WEIGHT
PRIOR_RETURN_RATE = seller_metrics.PRIOR_RETURN_RATE
PRIOR_ON_TIME_RATE = seller_metrics.PRIOR_ON_TIME_RATE
ON_TIME_DISPATCH_HOURS = seller_metrics.ON_TIME_DISPATCH_HOURS

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def order(status, confirmed_ago_h=None, dispatch_delay_h=None):
    """One seller order, with optional timing."""
    entry = {"status": status}
    if confirmed_ago_h is not None:
        confirmed = NOW - timedelta(hours=confirmed_ago_h)
        entry["confirmed_at"] = confirmed
        if dispatch_delay_h is not None:
            entry["dispatched_at"] = confirmed + timedelta(hours=dispatch_delay_h)
    return entry


# ---------------------------------------------------------------------------
# shrinkage: the rule the file exists for
# ---------------------------------------------------------------------------

def test_one_bad_order_is_not_a_hundred_percent_return_rate():
    """THE test. One returned order out of one is one returned order."""
    metrics = compute_metrics([order("RETURNED", 48, 1)])
    assert metrics.return_rate < 0.25, (
        f"a single return produced a {metrics.return_rate:.0%} return rate; "
        f"that is noise being treated as evidence")


def test_a_long_bad_record_does_show_through():
    # Shrinkage must not become an excuse. Forty returns out of forty is a
    # seller with a problem.
    metrics = compute_metrics([order("RETURNED", 48, 1) for _ in range(40)])
    assert metrics.return_rate > 0.6


def test_shrinkage_moves_from_prior_to_observed_as_evidence_arrives():
    rates = [compute_metrics([order("RETURNED", 48, 1) for _ in range(n)]).return_rate
             for n in (1, 5, 20, 100)]
    assert rates == sorted(rates), "the rate should rise as returns accumulate"
    assert rates[0] < 0.25 and rates[-1] > 0.75


def test_no_history_is_exactly_the_prior():
    metrics = compute_metrics([])
    assert metrics.return_rate == pytest.approx(PRIOR_RETURN_RATE)
    assert metrics.on_time_dispatch_rate == pytest.approx(PRIOR_ON_TIME_RATE)
    assert metrics.confidence == 0.0


def test_shrink_is_the_prior_with_no_observations():
    assert shrink(0, 0, 0.3) == pytest.approx(0.3)


def test_shrink_approaches_the_observed_rate_with_many():
    assert shrink(1000, 1000, 0.1) > 0.95


def test_shrink_refuses_negative_input():
    with pytest.raises(ValueError, match="must not be negative"):
        shrink(1, -1, 0.1)


def test_confidence_rises_with_evidence_and_never_reaches_one():
    low = compute_metrics([order("DELIVERED", 48, 1) for _ in range(2)])
    high = compute_metrics([order("DELIVERED", 48, 1) for _ in range(500)])
    assert low.confidence < 0.2 < high.confidence < 1.0


# ---------------------------------------------------------------------------
# what counts as the seller's fault
# ---------------------------------------------------------------------------

def test_an_order_cancelled_before_the_seller_saw_it_is_not_counted():
    # A buyer changing their mind in the first minute is not a seller failure,
    # and counting it would punish sellers in categories buyers browse
    # indecisively.
    never_seen = [{"status": "CANCELLED"} for _ in range(10)]
    baseline = compute_metrics([])
    metrics = compute_metrics(never_seen)
    assert metrics.cancellation_rate == pytest.approx(baseline.cancellation_rate)


def test_an_order_cancelled_after_confirmation_does_count():
    after = [order("CANCELLED", 10) for _ in range(30)]
    assert compute_metrics(after).cancellation_rate > 0.5


def test_an_order_still_in_transit_is_not_evidence_either_way():
    # DISPATCHED has not concluded, so it must not move the return rate.
    in_flight = [order("DISPATCHED", 5, 1) for _ in range(20)]
    baseline = compute_metrics([])
    assert compute_metrics(in_flight).return_rate == \
        pytest.approx(baseline.return_rate)


def test_a_pending_order_is_not_late():
    # Confirmed an hour ago and not dispatched is pending, not late. Treating
    # it as late would punish a seller for the clock.
    pending = [order("CONFIRMED", 1) for _ in range(20)]
    baseline = compute_metrics([])
    assert compute_metrics(pending).on_time_dispatch_rate == \
        pytest.approx(baseline.on_time_dispatch_rate)


# ---------------------------------------------------------------------------
# on-time dispatch
# ---------------------------------------------------------------------------

def test_dispatching_within_the_window_is_on_time():
    quick = [order("DELIVERED", 72, ON_TIME_DISPATCH_HOURS - 1)
             for _ in range(50)]
    assert compute_metrics(quick).on_time_dispatch_rate > 0.95


def test_dispatching_after_the_window_is_late():
    slow = [order("DELIVERED", 200, ON_TIME_DISPATCH_HOURS + 24)
            for _ in range(50)]
    assert compute_metrics(slow).on_time_dispatch_rate < 0.35


def test_exactly_at_the_boundary_counts_as_on_time():
    at_limit = [order("DELIVERED", 72, ON_TIME_DISPATCH_HOURS)
                for _ in range(50)]
    assert compute_metrics(at_limit).on_time_dispatch_rate > 0.95


def test_a_dispatch_with_no_confirmation_time_is_not_a_failure():
    # Missing data must not invent a failure.
    entry = {"status": "DELIVERED", "dispatched_at": NOW}
    assert compute_metrics([entry] * 50).on_time_dispatch_rate > 0.95


def test_hours_between_is_none_when_either_end_is_missing():
    # None rather than zero: a missing dispatch time means "not dispatched",
    # and zero would make every undispatched order perfectly on time.
    assert hours_between(None, NOW) is None
    assert hours_between(NOW, None) is None
    assert hours_between(NOW - timedelta(hours=3), NOW) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# shape and robustness
# ---------------------------------------------------------------------------

def test_counts_are_reported_alongside_the_rates():
    # So a caller can say "not enough data yet" instead of presenting a shrunk
    # estimate as a measurement.
    metrics = compute_metrics([order("DELIVERED", 48, 1), order("CANCELLED"),
                               order("RETURNED", 48, 1)])
    assert metrics.seller_order_count == 3
    assert metrics.concluded_count == 2


def test_every_rate_stays_within_zero_and_one():
    for orders in ([], [order("RETURNED", 5, 1)] * 200,
                   [order("DELIVERED", 5, 1)] * 200,
                   [order("CANCELLED", 5)] * 200):
        metrics = compute_metrics(orders)
        for rate in (metrics.on_time_dispatch_rate, metrics.cancellation_rate,
                     metrics.return_rate, metrics.confidence):
            assert 0.0 <= rate <= 1.0


def test_junk_entries_are_ignored_rather_than_fatal():
    metrics = compute_metrics([order("DELIVERED", 48, 1), None, "nonsense", 42])
    assert metrics.seller_order_count == 1


def test_an_unknown_status_is_counted_but_moves_nothing():
    # A status a later version writes must not be read as a failure.
    metrics = compute_metrics([{"status": "TELEPORTED"} for _ in range(20)])
    baseline = compute_metrics([])
    assert metrics.seller_order_count == 20
    assert metrics.return_rate == pytest.approx(baseline.return_rate)


def test_as_dict_rounds_for_transport():
    values = compute_metrics([order("DELIVERED", 48, 1)]).as_dict()
    assert set(values) >= {"on_time_dispatch_rate", "cancellation_rate",
                           "return_rate", "confidence"}


# ---------------------------------------------------------------------------
# the handoff to ranking
# ---------------------------------------------------------------------------

def test_quality_inputs_report_reviews_as_absent():
    # There is no reviews service. Reporting an invented average would put a
    # number into the ranking formula that looks like evidence.
    values = quality_inputs(compute_metrics([order("DELIVERED", 48, 1)]))
    assert values["rating"] is None
    assert values["review_count"] is None


def test_quality_inputs_pass_reviews_through_when_they_exist():
    values = quality_inputs(compute_metrics([]), rating=4.5, review_count=12)
    assert values["rating"] == 4.5 and values["review_count"] == 12


def test_quality_inputs_match_what_ranking_consumes():
    from conftest import ranking_rules
    values = quality_inputs(compute_metrics([order("DELIVERED", 48, 1)]))
    # The contract between the two modules: whatever compute_metrics produces
    # must be something quality_boost accepts without a KeyError.
    assert ranking_rules.quality_boost(values) > 0
