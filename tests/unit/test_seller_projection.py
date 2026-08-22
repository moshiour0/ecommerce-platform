"""
Copying a seller's standing onto the products they sell.

§3g scores products on the seller behind them. Until this projection existed,
search-service fetched those signals *per seller, per results page*, over HTTP
-- cached for a minute, capped at 25 lookups, behind a breaker. Correct, and a
round trip inside a search.

The awkward shape of the problem is that signals are a fact about a *seller*
and the read model is keyed by *product*: one new review changes a number
copied onto every product that seller lists. Most of what follows is about the
consequences of that.
"""

import importlib.util
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


projection = _load("seller_projection_under_test",
                   "workers/stream-processor/app/seller_projection.py")
read_model = _load("read_model_under_test",
                   "shared/libs/python-common/read_model.py")

SellerSignals = projection.SellerSignals
build_signal_fields = projection.build_signal_fields
is_refresh_trigger = projection.is_refresh_trigger
parse_coordinates = projection.parse_coordinates
seller_id_from = projection.seller_id_from
signals_from_responses = projection.signals_from_responses


def signals(**overrides):
    base = dict(may_sell=True, rating=4.5, review_count=12,
                on_time_dispatch_rate=0.9,
                cancellation_rate=0.02, return_rate=0.1, confidence=0.6,
                latitude=23.8103, longitude=90.4125)
    base.update(overrides)
    return SellerSignals(**base)


# ---------------------------------------------------------------------------
# what triggers a refresh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event", [
    "ReviewPublished", "ReviewUpdated",
    "SellerOrderDelivered", "SellerOrderReturned", "SellerOrderCancelled",
    "SellerApproved", "SellerReinstated", "SellerLocationUpdated",
])
def test_signal_changing_events_trigger_a_refresh(event):
    assert is_refresh_trigger(event)


@pytest.mark.parametrize("event", [
    "SellerOrderConfirmed", "SellerOrderDispatched", "SellerOrderRtoStarted",
])
def test_mid_flight_order_events_do_not(event):
    """Every trigger costs an update-by-query across a whole catalogue.

    Dispatch timing feeds the on-time rate, but the rate cannot move until an
    order *concludes* -- and each of these is followed by a delivery, return or
    cancellation that already triggers. Including them would roughly double the
    refresh volume to reach the same numbers a few minutes sooner.
    """
    assert not is_refresh_trigger(event)


@pytest.mark.parametrize("event", ["ProductCreated", "PriceUpdated",
                                   "InventoryReserved", "PaymentCharged"])
def test_unrelated_events_do_not_trigger(event):
    assert not is_refresh_trigger(event)


def test_every_trigger_is_an_event_some_service_actually_emits():
    """A trigger set full of plausible names nothing publishes is a projection
    that silently never runs.

    These were checked against the outboxes rather than assumed -- an earlier
    draft of this module listed SellerActivated and SellerProfileUpdated,
    neither of which exists.
    """
    emitted = {
        # reviews-service
        "ReviewPublished", "ReviewUpdated",
        # order-saga
        "SellerOrderDelivered", "SellerOrderReturned", "SellerOrderCancelled",
        "SellerOrderConfirmed", "SellerOrderDispatched",
        # seller-service
        "SellerApproved", "SellerReinstated", "SellerSuspended",
        "SellerBanned", "SellerRegistered", "SellerRejected",
        "SellerDocumentsSubmitted", "SellerReviewStarted",
        "SellerLocationUpdated",
    }
    unknown = set(projection.REFRESH_TRIGGERS) - emitted
    assert not unknown, f"triggers nothing emits: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# whose signals moved
# ---------------------------------------------------------------------------

def test_the_seller_is_read_from_the_payload():
    assert seller_id_from("ReviewPublished", {"seller_id": "s-1"}) == "s-1"


def test_a_seller_event_keys_on_its_own_id():
    assert seller_id_from("SellerApproved", {"id": "s-2"}) == "s-2"


def test_seller_id_wins_over_the_aggregate_id():
    """A SellerOrder event's `id` is the order, not the seller."""
    assert seller_id_from("SellerOrderDelivered",
                          {"id": "order-9", "seller_id": "s-3"}) == "s-3"


def test_an_event_that_cannot_say_returns_none():
    """Rather than guessing and refreshing a stranger's whole catalogue."""
    assert seller_id_from("ReviewPublished", {"product_id": "p-1"}) is None
    assert seller_id_from("ReviewPublished", {}) is None


# ---------------------------------------------------------------------------
# coordinates
# ---------------------------------------------------------------------------

def test_a_valid_pair_parses():
    assert parse_coordinates("23.8103", "90.4125") == (23.8103, 90.4125)


@pytest.mark.parametrize("lat,lon", [
    (None, "90.4"), ("23.8", None), (None, None),
    ("north", "90.4"), ("", ""), ("23.8", "not-a-number"),
])
def test_an_unusable_pair_is_unlocated(lat, lon):
    assert parse_coordinates(lat, lon) == (None, None)


@pytest.mark.parametrize("lat,lon", [
    ("91", "0"), ("-91", "0"), ("0", "181"), ("0", "-181"),
])
def test_coordinates_off_the_planet_are_unlocated(lat, lon):
    assert parse_coordinates(lat, lon) == (None, None)


def test_a_broken_location_is_never_silently_zero_zero():
    """(0, 0) is in the Gulf of Guinea.

    Defaulting broken profiles there would make every one of them each other's
    nearest neighbour, and a buyer in Dhaka would be shown whichever seller
    had the most malformed address.
    """
    assert parse_coordinates("garbage", "garbage") == (None, None)
    fields = build_signal_fields(signals(latitude=None, longitude=None))
    assert fields["seller_location"] is None


# ---------------------------------------------------------------------------
# what gets written
# ---------------------------------------------------------------------------

def test_the_fields_written_are_exactly_the_ones_this_writer_owns():
    """Or the ownership check in read_model rejects the write at runtime."""
    fields = set(build_signal_fields(signals()))
    owned = read_model.fields_owned_by(read_model.SELLER_SIGNALS)
    assert fields <= owned, f"not owned: {sorted(fields - owned)}"


def test_the_write_passes_the_shared_ownership_check():
    read_model.validate_product_write(
        read_model.SELLER_SIGNALS, build_signal_fields(signals()).keys())


def test_this_writer_may_not_touch_anyone_elses_fields():
    """A projection that could write `price_cents` is one bug from erasing it."""
    for foreign in ("price_cents", "quantity_available", "name", "seller_id"):
        with pytest.raises(read_model.OwnershipError):
            read_model.validate_product_write(read_model.SELLER_SIGNALS,
                                              [foreign])


def test_missing_signals_are_written_as_null_not_omitted():
    """The clearing case, and the reason a refresh writes the whole set.

    A seller whose last review is removed must have their rating *cleared*. A
    refresh that only wrote non-null values would leave yesterday's number in
    place forever with nothing to show it was stale.
    """
    fields = build_signal_fields(signals(rating=None, review_count=None))
    assert "seller_rating" in fields
    assert fields["seller_rating"] is None
    assert fields["seller_review_count"] is None


def test_the_location_uses_elasticsearchs_unambiguous_form():
    """The array form is [lon, lat] and the string form is "lat,lon".

    Mixing them up puts shops in the wrong hemisphere without erroring, so the
    object form is the only one used.
    """
    location = build_signal_fields(signals())["seller_location"]
    assert location == {"lat": 23.8103, "lon": 90.4125}


# ---------------------------------------------------------------------------
# assembling three services' answers
# ---------------------------------------------------------------------------

def test_signals_are_assembled_from_all_three():
    result = signals_from_responses(
        metrics={"on_time_dispatch_rate": 0.95, "cancellation_rate": 0.01,
                 "return_rate": 0.05, "confidence": 0.8},
        reviews={"rating": 4.2, "review_count": 30},
        profile={"latitude": "23.8", "longitude": "90.4"})
    assert result.rating == 4.2
    assert result.on_time_dispatch_rate == 0.95
    assert result.latitude == 23.8


@pytest.mark.parametrize("missing", ["metrics", "reviews", "profile"])
def test_one_unreachable_service_does_not_lose_the_others(missing):
    """A failed fetch must not fail the whole refresh.

    The other two still have current values; the missing one becomes None,
    which ranking treats as average. Failing the refresh instead would leave
    *every* signal stale, which is strictly worse.
    """
    responses = {
        "metrics": {"on_time_dispatch_rate": 0.9, "confidence": 0.5},
        "reviews": {"rating": 4.0, "review_count": 10},
        "profile": {"latitude": "23.8", "longitude": "90.4"},
    }
    responses[missing] = None
    result = signals_from_responses(**responses)

    if missing != "reviews":
        assert result.rating == 4.0
    if missing != "metrics":
        assert result.on_time_dispatch_rate == 0.9
    if missing != "profile":
        assert result.latitude == 23.8


def test_everything_unreachable_is_neutral_not_bad():
    """The §3g cold-start rule, applied to an outage.

    A seller whose signals could not be fetched must rank as average, not at
    the bottom -- otherwise a reviews-service blip would bury the whole
    marketplace.
    """
    result = signals_from_responses(None, None, None)
    assert result.rating is None
    assert result.return_rate is None
    assert result.latitude is None

    quality = read_model.quality_from_document(
        build_signal_fields(result))
    assert all(v is None for v in quality.values())


# ---------------------------------------------------------------------------
# the round trip: what is written is what ranking reads
# ---------------------------------------------------------------------------

def test_written_fields_translate_back_into_ranking_inputs():
    """The contract that would otherwise drift silently.

    The projection writes `seller_rating`; ranking wants `rating`. If a rename
    happened on one side only, the field would still be indexed and simply stop
    being read -- no error, just a formula quietly scoring on nothing.
    """
    document = build_signal_fields(signals())
    quality = read_model.quality_from_document(document)

    assert quality["rating"] == 4.5
    assert quality["review_count"] == 12
    assert quality["on_time_dispatch_rate"] == 0.9
    assert quality["cancellation_rate"] == 0.02
    assert quality["return_rate"] == 0.1
    assert quality["confidence"] == 0.6


def test_an_unprojected_document_reads_as_neutral():
    """A product indexed before this projection existed.

    All-None, which quality_boost scores as exactly 1.0 -- the same as the HTTP
    lookup used to return on failure. So old documents are neutral rather than
    broken and need no backfill to stay searchable.
    """
    quality = read_model.quality_from_document({"product_id": "p-1"})
    assert all(v is None for v in quality.values())
    assert not read_model.has_seller_signals({"product_id": "p-1"})


def test_a_projected_document_is_recognised():
    document = dict(build_signal_fields(signals()))
    document["seller_signals_updated_at"] = "2026-08-21T12:00:00+00:00"
    assert read_model.has_seller_signals(document)


# ---------------------------------------------------------------------------
# the two gates
# ---------------------------------------------------------------------------

def test_the_pre_filter_admits_every_trigger():
    """The gap that cost an hour, made loud.

    stream-processor has two gates. INDEXED_EVENTS is checked in main.py before
    the router is ever called; the router then dispatches. Adding a trigger to
    the projection alone is completely silent -- the event is dropped a layer
    earlier with a DEBUG line reading "ignored on purpose", which is exactly
    what it looks like when it is not on purpose.

    SellerLocationUpdated was published, reached the worker, committed its
    offset and did nothing, and every log line about it said that was intended.
    """
    event_rules = _load("event_rules_under_test",
                        "workers/stream-processor/app/consumers/event_rules.py")

    blocked = set(projection.REFRESH_TRIGGERS) - set(event_rules.INDEXED_EVENTS)
    assert not blocked, (
        f"these triggers would be dropped by main.py before the router sees "
        f"them: {sorted(blocked)}")


def test_the_two_lists_agree_exactly():
    """event_rules restates the set to stay dependency-free.

    That makes drift possible in both directions, so it is asserted rather
    than assumed -- the same trade reindex_rules makes with its field list.
    """
    event_rules = _load("event_rules_under_test_2",
                        "workers/stream-processor/app/consumers/event_rules.py")
    assert set(event_rules.SELLER_SIGNAL_EVENTS) == set(
        projection.REFRESH_TRIGGERS)


# ---------------------------------------------------------------------------
# visibility must never be coalesced
# ---------------------------------------------------------------------------

def test_suspension_and_reinstatement_are_never_coalesced():
    """The bug this exists for, and it is not subtle.

    Measured: a suspend and a reinstate arrived 1.3 seconds apart. The suspend
    refreshed; the reinstate was dropped as "refreshed recently". The seller's
    entire catalogue stayed hidden afterwards -- and would have stayed hidden
    indefinitely, because nothing else was ever going to happen to a seller
    nobody could buy from.

    A suspension that cannot be undone is not a suspension. It is a deletion
    with extra steps.
    """
    for event in ("SellerSuspended", "SellerBanned", "SellerRejected",
                  "SellerApproved", "SellerReinstated"):
        assert projection.changes_visibility(event), (
            f"{event} decides whether products can be seen and must refresh "
            f"immediately")


def test_score_only_events_may_still_be_coalesced():
    """The window has to keep earning its place.

    A seller delivering twenty orders in a minute should not trigger twenty
    update-by-queries to arrive at nearly the same rating, and a rating that is
    thirty seconds stale changes nothing anyone can observe.
    """
    for event in ("ReviewPublished", "ReviewUpdated", "SellerOrderDelivered",
                  "SellerOrderReturned", "SellerOrderCancelled",
                  "SellerLocationUpdated"):
        assert not projection.changes_visibility(event)


def test_every_visibility_event_is_also_a_refresh_trigger():
    """Forcing a refresh is useless if the event never reaches the router."""
    assert projection.VISIBILITY_EVENTS <= projection.REFRESH_TRIGGERS
