"""
Who may review, and what the ratings add up to.

Reviews are the input the ranking formula has been missing since it was
written. §3g scores on a rating and a review count that no service produced, so
they were reported as None and treated as neutral -- honest, but it left a
third of the scoring inert.

Two decisions shape these tests: verified purchase only, and product and seller
rated separately. Most of what follows is about the edges of the first, because
"verified" is the whole anti-gaming story and every hole in it is a free vote.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "review_rules_under_test",
    Path(__file__).resolve().parents[2] / "services" / "reviews-service"
    / "app" / "services" / "review_rules.py")
review_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_rules)

may_review = review_rules.may_review
may_edit = review_rules.may_edit
validate_rating = review_rules.validate_rating
aggregate_ratings = review_rules.aggregate_ratings
distribution = review_rules.distribution
quality_signal = review_rules.quality_signal
Verdict = review_rules.Verdict
Aggregate = review_rules.Aggregate

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
BUYER = "buyer-1"
PRODUCT = "prod-1"


def eligibility(**overrides):
    kwargs = dict(
        buyer_id=BUYER, order_buyer_id=BUYER,
        seller_order_status="DELIVERED",
        product_ids_in_order=[PRODUCT, "prod-2"],
        product_id=PRODUCT,
        delivered_at=NOW - timedelta(days=3),
        now=NOW,
        already_reviewed=False,
    )
    kwargs.update(overrides)
    return may_review(**kwargs)


# ---------------------------------------------------------------------------
# verified purchase -- the whole anti-gaming story
# ---------------------------------------------------------------------------

def test_a_delivered_purchase_may_be_reviewed():
    assert eligibility().allowed


@pytest.mark.parametrize("status", ["DELIVERED", "SETTLED"])
def test_settled_counts_as_delivered(status):
    """SETTLED is DELIVERED plus the courier having remitted.

    The buyer's experience is identical; the money moving later is not their
    business and must not gate their review.
    """
    assert eligibility(seller_order_status=status).allowed


@pytest.mark.parametrize("status", ["PENDING", "INVENTORY_RESERVED",
                                    "CONFIRMED", "DISPATCHED"])
def test_a_product_still_in_transit_cannot_be_reviewed(status):
    result = eligibility(seller_order_status=status)
    assert result.verdict is Verdict.NOT_DELIVERED
    assert result.http_status == 403


@pytest.mark.parametrize("status", ["RETURNED", "RTO_IN_TRANSIT", "CANCELLED"])
def test_a_refused_parcel_cannot_be_reviewed(status):
    """The one that is easy to get wrong.

    Under COD a refusal happens at the door: the buyer never opened the box and
    has nothing a product rating can carry. Their complaint is about the seller
    or the courier, and a return already counts against the seller in the
    fulfilment metrics -- letting it also land as a one-star product review
    would penalise one event twice and put a rating on an item nobody tried.
    """
    assert eligibility(seller_order_status=status).verdict is Verdict.NOT_DELIVERED


def test_someone_elses_purchase_cannot_be_reviewed():
    result = eligibility(order_buyer_id="buyer-2")
    assert result.verdict is Verdict.NOT_THE_BUYER
    assert result.http_status == 403


def test_ownership_is_checked_before_anything_about_the_order():
    """So a caller cannot learn about orders that are not theirs.

    Checking contents first would let someone probe which products are in a
    stranger's order by watching whether the answer changes.
    """
    result = eligibility(order_buyer_id="buyer-2",
                         seller_order_status="PENDING",
                         product_id="not-in-this-order")
    assert result.verdict is Verdict.NOT_THE_BUYER


def test_a_product_not_in_the_order_cannot_be_reviewed():
    result = eligibility(product_id="prod-99")
    assert result.verdict is Verdict.NOT_IN_ORDER
    assert result.http_status == 404, (
        "404 rather than 403 so a caller cannot map which orders contain what")


def test_ids_compare_as_strings():
    """uuid.UUID from the database, str from JSON. They must match."""
    import uuid
    pid = uuid.uuid4()
    assert eligibility(product_id=str(pid), product_ids_in_order=[pid]).allowed
    assert eligibility(buyer_id=str(pid), order_buyer_id=pid,
                       ).allowed


# ---------------------------------------------------------------------------
# one review per purchase
# ---------------------------------------------------------------------------

def test_a_purchase_can_only_be_reviewed_once():
    result = eligibility(already_reviewed=True)
    assert result.verdict is Verdict.ALREADY_REVIEWED
    assert result.http_status == 409


def test_the_same_product_bought_twice_may_be_reviewed_twice():
    """Keyed on the purchase, not the product.

    A buyer who orders the same thing again has a second, genuine experience of
    it -- and blocking that would also silently block anyone restocking a
    consumable, who is exactly the buyer whose opinion is worth most.
    """
    first = eligibility(already_reviewed=False)
    assert first.allowed
    # A different seller order, same product: the caller passes
    # already_reviewed for *that* purchase, which is false.
    second = eligibility(already_reviewed=False)
    assert second.allowed


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

def test_reviews_close_after_the_window():
    result = eligibility(delivered_at=NOW - timedelta(days=91))
    assert result.verdict is Verdict.WINDOW_CLOSED
    assert result.http_status == 422


def test_the_window_boundary_is_inclusive():
    assert eligibility(delivered_at=NOW - timedelta(days=90)).allowed


def test_a_slow_parcel_still_leaves_time_to_review():
    assert eligibility(delivered_at=NOW - timedelta(days=45)).allowed


def test_a_missing_delivery_timestamp_does_not_deny_a_real_buyer():
    """A delivered order with no timestamp is a data fault, not their problem.

    Denying is the crueller failure: a legitimate review is silently refused
    because a column was never set, and the buyer has no way to tell why.
    """
    assert eligibility(delivered_at=None).allowed


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

def test_a_review_can_be_fixed_soon_after_writing():
    assert may_edit(authored_at=NOW - timedelta(days=2), now=NOW)


def test_editing_closes_before_the_review_window_does():
    """Editing is for fixing what you meant, not for reopening the rating.

    A five-star review that stays editable for ninety days is an asset a seller
    can offer something for.
    """
    assert not may_edit(authored_at=NOW - timedelta(days=15), now=NOW)
    assert review_rules.EDIT_WINDOW_DAYS < review_rules.REVIEW_WINDOW_DAYS


# ---------------------------------------------------------------------------
# ratings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stars", [1, 2, 3, 4, 5])
def test_whole_stars_are_accepted(stars):
    assert validate_rating(stars, "product_rating") == stars


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_ratings_outside_the_scale_are_rejected(bad):
    with pytest.raises(ValueError, match="between 1 and 5"):
        validate_rating(bad, "product_rating")


@pytest.mark.parametrize("bad", [4.5, "5", None, True])
def test_only_whole_numbers_are_accepted(bad):
    """A 4.5-star submission is a UI affordance, not a measurement.

    True is rejected explicitly: bool is an int in Python, so `True` would
    otherwise validate as one star.
    """
    with pytest.raises(ValueError):
        validate_rating(bad, "product_rating")


def test_the_error_names_the_field():
    """So a caller with two ratings knows which one it is complaining about."""
    with pytest.raises(ValueError, match="seller_rating"):
        validate_rating(9, "seller_rating")


# ---------------------------------------------------------------------------
# aggregation -- and the shrinking that must NOT happen here
# ---------------------------------------------------------------------------

def test_the_average_is_the_plain_mean():
    assert aggregate_ratings([5, 4, 3]).average == 4.0
    assert aggregate_ratings([5, 4, 3]).count == 3


def test_a_small_sample_is_not_shrunk_here():
    """The decision most likely to be "corrected" later into a bug.

    ranking_rules.rating_component already pulls a rating toward neutral in
    proportion to how few reviews back it. If this shrank too, a seller with
    three reviews would be pulled toward average twice -- once here, once there
    -- and the ranking would be quietly flatter than either module claims.
    Neither would look wrong on its own, which is why this is pinned.
    """
    assert aggregate_ratings([5]).average == 5.0, (
        "a single five-star review must report 5.0, not a value pulled "
        "toward neutral; the consumer shrinks, using the count")
    assert aggregate_ratings([1]).average == 1.0


def test_the_count_is_reported_so_the_consumer_can_decide():
    """The count is what makes not shrinking here safe."""
    assert aggregate_ratings([5]).count == 1
    assert aggregate_ratings([5] * 200).count == 200


def test_nothing_rated_is_unknown_not_zero():
    result = aggregate_ratings([])
    assert result.average is None
    assert result.count == 0
    assert not result.known


def test_a_missing_seller_rating_is_skipped_not_counted_as_zero():
    """A buyer who rated the product but not the seller has not given nought."""
    assert aggregate_ratings([5, None, 3]).average == 4.0
    assert aggregate_ratings([5, None, 3]).count == 2


def test_all_missing_is_unknown():
    assert aggregate_ratings([None, None]).average is None


# ---------------------------------------------------------------------------
# distribution
# ---------------------------------------------------------------------------

def test_the_distribution_separates_products_the_average_cannot():
    """3.0 from twenty threes and 3.0 from ten fives and ten ones.

    Completely different products, identical average, and only the
    distribution says so.
    """
    consistent = distribution([3] * 20)
    polarised = distribution([5] * 10 + [1] * 10)
    assert aggregate_ratings([3] * 20).average == aggregate_ratings(
        [5] * 10 + [1] * 10).average
    assert consistent != polarised
    assert consistent[3] == 20
    assert polarised[5] == 10 and polarised[1] == 10


def test_every_star_appears_even_at_zero():
    """So a UI can render five bars without inventing the empty ones."""
    assert set(distribution([5]).keys()) == {1, 2, 3, 4, 5}
    assert distribution([5])[2] == 0


# ---------------------------------------------------------------------------
# the contract with ranking
# ---------------------------------------------------------------------------

def test_the_quality_signal_matches_what_ranking_consumes():
    signal = quality_signal(Aggregate(4.5, 12))
    assert signal == {"rating": 4.5, "review_count": 12}


def test_an_unrated_seller_reports_unknown_rather_than_neutral():
    """§3g's cold-start rule needs unknown distinguishable from average.

    A new seller scored as *bad* for having no reviews would never be seen,
    never earn one, and stay unseen -- the ranking enforcing its own prior. The
    consumer can only treat unknown as average if it can tell the difference,
    so this must be None and never 3.0.
    """
    signal = quality_signal(Aggregate(None, 0))
    assert signal["rating"] is None
    assert signal["review_count"] == 0
