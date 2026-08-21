"""
Unit tests for the one ranking pipeline.

The headline is the rule the whole design exists to satisfy, stated in the
roadmap's D4 and in the user's own words: *first priority to products and
reviews, then the nearest shop*. As arithmetic that means a 40 km shop with
excellent reviews must outrank a mediocre one next door.

That test failed on the first implementation. D4 illustrates the distance decay
with a floor of 0.6, and 0.6 produces the opposite result -- the mediocre
neighbour wins by 24%. The floor is now derived from the requirement instead of
copied from the illustration, and `test_the_floor_is_above_the_derived_threshold`
pins the derivation so a later change to the quality weights cannot quietly
break it again.

The other theme is cold start. Every "unknown" here means *average*, never
*bad*: a new seller penalised for having no reviews is never seen, therefore
never earns a review, and stays unseen -- the ranking enforcing its own prior.
"""

import pytest

from conftest import ranking_rules

distance_decay = ranking_rules.distance_decay
rating_component = ranking_rules.rating_component
quality_boost = ranking_rules.quality_boost
personalisation_boost = ranking_rules.personalisation_boost
final_score = ranking_rules.final_score
rank = ranking_rules.rank
DISTANCE_FLOOR = ranking_rules.DISTANCE_FLOOR
QUALITY_MIN = ranking_rules.QUALITY_MIN
QUALITY_MAX = ranking_rules.QUALITY_MAX

EXCELLENT = {"rating": 4.8, "review_count": 300, "confidence": 0.9,
             "on_time_dispatch_rate": 0.98, "cancellation_rate": 0.01,
             "return_rate": 0.02}
MEDIOCRE = {"rating": 3.0, "review_count": 300, "confidence": 0.9,
            "on_time_dispatch_rate": 0.70, "cancellation_rate": 0.15,
            "return_rate": 0.25}


# ---------------------------------------------------------------------------
# the rule the design exists for
# ---------------------------------------------------------------------------

def test_a_well_reviewed_distant_shop_beats_a_mediocre_near_one():
    """THE test. Quality first, then proximity.

    Failed on the first implementation with the roadmap's illustrative floor
    of 0.6, which is why the floor is now derived.
    """
    far = final_score(1.0, EXCELLENT, distance_km=40)
    near = final_score(1.0, MEDIOCRE, distance_km=1)
    assert far > near, (
        f"a mediocre shop next door ({near:.4f}) outranked an excellent one "
        f"40km away ({far:.4f}); proximity is beating quality")


def test_the_floor_is_above_the_derived_threshold():
    """Pins the arithmetic the floor was chosen from.

    At 40 km the decay is effectively zero, so the far result scores
    `quality_far x floor` and the near one `quality_near x ~1`. Quality wins
    only when the quality ratio exceeds `1 / floor`. This asserts the floor
    still clears that threshold with margin, so a later change to the quality
    weights fails here rather than silently inverting the ranking.
    """
    ratio = quality_boost(EXCELLENT) / quality_boost(MEDIOCRE)
    required_floor = 1.0 / ratio
    assert DISTANCE_FLOOR > required_floor, (
        f"the quality range gives a ratio of only {ratio:.4f}, which needs a "
        f"floor above {required_floor:.4f}; it is {DISTANCE_FLOOR}")
    # Margin, not a coincidence: 0.75 clears it by 0.4% and would be undone by
    # any nudge to the weights.
    assert DISTANCE_FLOOR - required_floor > 0.02


def test_proximity_still_decides_between_equals():
    # It is a tiebreaker, not a no-op. Two identical products should order by
    # distance.
    near = final_score(1.0, EXCELLENT, distance_km=1)
    far = final_score(1.0, EXCELLENT, distance_km=30)
    assert near > far


def test_relevance_cannot_be_bought_with_proximity_or_quality():
    # A search for "keyboard" must not return a nearby grocer, however good.
    irrelevant_perfect_nearby = final_score(
        0.1, {"rating": 5.0, "review_count": 999, "confidence": 1.0,
              "on_time_dispatch_rate": 1.0, "cancellation_rate": 0.0,
              "return_rate": 0.0}, distance_km=0)
    relevant_unknown_far = final_score(1.0, None, distance_km=200)
    assert relevant_unknown_far > irrelevant_perfect_nearby


# ---------------------------------------------------------------------------
# distance is a decay, never a filter
# ---------------------------------------------------------------------------

def test_distance_never_zeroes_a_result():
    # Hard-filtering by radius is what makes a marketplace feel empty in
    # low-density areas.
    for km in (0, 1, 10, 50, 500, 5000):
        assert distance_decay(km) >= DISTANCE_FLOOR


def test_the_decay_falls_monotonically():
    previous = distance_decay(0)
    for km in (1, 5, 10, 20, 50, 200):
        current = distance_decay(km)
        assert current <= previous, f"decay rose between {km}km and the last"
        previous = current


def test_zero_distance_is_no_penalty():
    assert distance_decay(0) == pytest.approx(1.0)


def test_unknown_distance_is_not_a_penalty():
    # A seller who has not set coordinates is unlocated, not far away.
    # Penalising them would rank shops by how completely they filled in a form.
    assert distance_decay(None) == 1.0


def test_a_negative_distance_is_refused():
    with pytest.raises(ValueError, match="must not be negative"):
        distance_decay(-1)


# ---------------------------------------------------------------------------
# cold start: unknown is average, never bad
# ---------------------------------------------------------------------------

def test_no_reviews_is_exactly_neutral():
    assert rating_component(None, None) == 1.0
    assert rating_component(None, 0) == 1.0


def test_no_seller_record_is_exactly_neutral():
    assert quality_boost(None) == 1.0
    assert quality_boost({}) == 1.0


def test_a_new_seller_is_not_buried():
    # The whole cold-start argument in one assertion: a brand new seller with
    # a matching product must be competitive with an established average one.
    new_seller = final_score(1.0, None, distance_km=5)
    average = final_score(1.0, {"rating": 3.5, "review_count": 200,
                                "confidence": 0.9,
                                "on_time_dispatch_rate": 0.90,
                                "cancellation_rate": 0.05,
                                "return_rate": 0.10}, distance_km=5)
    assert new_seller == pytest.approx(average, rel=0.02)


def test_few_reviews_are_pulled_toward_neutral():
    # Three five-star reviews are an opinion, not a measurement.
    three = rating_component(5.0, 3)
    many = rating_component(5.0, 500)
    assert 1.0 < three < many


def test_a_rating_at_the_midpoint_moves_nothing():
    assert rating_component(3.5, 1000) == pytest.approx(1.0)


def test_a_rating_outside_the_scale_is_refused():
    for bad in (-0.1, 5.1, 11):
        with pytest.raises(ValueError, match="within"):
            rating_component(bad, 10)


# ---------------------------------------------------------------------------
# quality sinks a bad seller without erasing them
# ---------------------------------------------------------------------------

def test_a_terrible_seller_sinks():
    terrible = {"rating": 1.0, "review_count": 400, "confidence": 1.0,
                "on_time_dispatch_rate": 0.3, "cancellation_rate": 0.4,
                "return_rate": 0.5}
    assert quality_boost(terrible) < 1.0
    assert quality_boost(terrible) < quality_boost(MEDIOCRE)


def test_a_terrible_seller_does_not_vanish():
    # Sinking is the intent; vanishing removes the evidence that anything is
    # wrong from every screen an operator looks at.
    terrible = {"rating": 0.0, "review_count": 10000, "confidence": 1.0,
                "on_time_dispatch_rate": 0.0, "cancellation_rate": 1.0,
                "return_rate": 1.0}
    assert quality_boost(terrible) >= QUALITY_MIN
    assert final_score(1.0, terrible, distance_km=1) > 0


def test_quality_is_clamped_at_both_ends():
    absurd = {"rating": 5.0, "review_count": 10 ** 9, "confidence": 1.0,
              "on_time_dispatch_rate": 1.0, "cancellation_rate": 0.0,
              "return_rate": 0.0}
    assert QUALITY_MIN <= quality_boost(absurd) <= QUALITY_MAX


def test_returns_weigh_more_than_cancellations():
    # Under COD a return is the loss vector: shipping paid twice and the goods
    # come back. A cancellation before dispatch costs almost nothing.
    base = {"rating": None, "review_count": None, "confidence": 1.0,
            "on_time_dispatch_rate": 0.90, "cancellation_rate": 0.05,
            "return_rate": 0.10}
    high_returns = {**base, "return_rate": 0.40}
    high_cancels = {**base, "cancellation_rate": 0.40}
    assert quality_boost(high_returns) < quality_boost(high_cancels)


def test_a_seller_with_no_history_is_not_moved_by_their_rates():
    # confidence 0 means the rates are entirely prior. They must not move the
    # score at all, or a seller's first order decides their ranking.
    no_history = {"confidence": 0.0, "on_time_dispatch_rate": 0.0,
                  "cancellation_rate": 1.0, "return_rate": 1.0}
    assert quality_boost(no_history) == 1.0


# ---------------------------------------------------------------------------
# personalisation is the narrowest term
# ---------------------------------------------------------------------------

def test_personalisation_cannot_dominate():
    # Personalisation that can double a score stops being a search engine and
    # becomes a filter bubble.
    span = personalisation_boost(1.0) / personalisation_boost(0.0)
    quality_span = QUALITY_MAX / QUALITY_MIN
    assert span < quality_span


def test_no_affinity_is_neutral():
    assert personalisation_boost(None) == 1.0


def test_affinity_is_clamped():
    assert personalisation_boost(-5) == personalisation_boost(0.0)
    assert personalisation_boost(99) == personalisation_boost(1.0)


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def test_ranking_is_deterministic_for_equal_scores():
    # Without a stable tiebreak two equal results swap between requests,
    # pagination duplicates and drops items, and it reproduces half the time.
    candidates = [{"id": "b", "relevance": 1.0}, {"id": "a", "relevance": 1.0},
                  {"id": "c", "relevance": 1.0}]
    first = [c["id"] for c in rank(candidates)]
    second = [c["id"] for c in rank(list(reversed(candidates)))]
    assert first == second == ["a", "b", "c"]


def test_ranking_orders_by_score_descending():
    ordered = rank([
        {"id": "far-mediocre", "relevance": 1.0, "quality": MEDIOCRE,
         "distance_km": 50},
        {"id": "near-excellent", "relevance": 1.0, "quality": EXCELLENT,
         "distance_km": 2},
        {"id": "irrelevant", "relevance": 0.2, "quality": EXCELLENT,
         "distance_km": 0},
    ])
    assert [c["id"] for c in ordered] == \
        ["near-excellent", "far-mediocre", "irrelevant"]


def test_ranking_an_empty_set_is_empty():
    assert rank([]) == []
    assert rank(None) == []


def test_every_ranked_candidate_carries_its_score():
    for candidate in rank([{"id": "a", "relevance": 1.0}]):
        assert "score" in candidate and candidate["score"] > 0


def test_a_negative_relevance_is_refused():
    with pytest.raises(ValueError, match="must not be negative"):
        final_score(-1.0)
