"""
What a buyer has shown interest in, and how much that should move a result.

The last inert term of the ranking formula: `affinity` has been passed as None
since §3g was written, because nothing recorded what a buyer had looked at.

Personalisation is the term where being wrong is least visible and most
harmful. A ranking that over-fits to history stops being a search engine and
becomes a filter bubble -- the first complaint is always "I cannot find the
thing I know you sell", and by then the behaviour that caused it is months old.
So most of what follows is about the brakes rather than the boost.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Through conftest: several services expose a package called `app`, and
# putting one of them on sys.path decides that name for the whole session.
from conftest import ranking_rules

personalisation_boost = ranking_rules.personalisation_boost
PERSONALISATION_MIN = ranking_rules.PERSONALISATION_MIN
PERSONALISATION_MAX = ranking_rules.PERSONALISATION_MAX
QUALITY_MIN = ranking_rules.QUALITY_MIN
QUALITY_MAX = ranking_rules.QUALITY_MAX

_spec = importlib.util.spec_from_file_location(
    "affinity_rules_under_test",
    Path(__file__).resolve().parents[2] / "shared" / "libs" / "python-common"
    / "affinity_rules.py")
affinity_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(affinity_rules)

BehaviourEvent = affinity_rules.BehaviourEvent
AffinityProfile = affinity_rules.AffinityProfile
affinity_for = affinity_rules.affinity_for
build_profile = affinity_rules.build_profile
decay_factor = affinity_rules.decay_factor
profile_as_dict = affinity_rules.profile_as_dict
profile_from_dict = affinity_rules.profile_from_dict

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
ELECTRONICS = "cat-electronics"
GROCERY = "cat-grocery"
SHOP_A = "seller-a"
SHOP_B = "seller-b"


def event(kind="view", category=ELECTRONICS, seller=SHOP_A, days_ago=0):
    return BehaviourEvent(kind=kind, category_id=category, seller_id=seller,
                          occurred_at=NOW - timedelta(days=days_ago))


def many(n, **kwargs):
    return [event(**kwargs) for _ in range(n)]


# ---------------------------------------------------------------------------
# recency
# ---------------------------------------------------------------------------

def test_something_that_just_happened_counts_fully():
    assert decay_factor(NOW, NOW) == 1.0


def test_one_half_life_halves_it():
    assert decay_factor(NOW - timedelta(days=30), NOW) == pytest.approx(0.5)


def test_interest_keeps_fading():
    assert decay_factor(NOW - timedelta(days=60), NOW) == pytest.approx(0.25)
    assert decay_factor(NOW - timedelta(days=120), NOW) == pytest.approx(0.0625)


def test_a_buyer_can_move_on():
    """A long half-life is what makes a profile impossible to escape.

    What someone wanted last spring is a worse predictor than what they wanted
    last week, and a year-old interest should not still be steering results.
    """
    assert decay_factor(NOW - timedelta(days=365), NOW) < 0.001


def test_a_future_timestamp_is_not_amplified():
    """Clock skew between services is ordinary.

    Letting a future timestamp count for more than 1.0 would make a mis-set
    clock into a ranking exploit.
    """
    assert decay_factor(NOW + timedelta(days=10), NOW) == 1.0


def test_a_nonsensical_half_life_is_rejected():
    with pytest.raises(ValueError):
        decay_factor(NOW, NOW, half_life_days=0)


# ---------------------------------------------------------------------------
# building a profile
# ---------------------------------------------------------------------------

def test_a_purchase_outweighs_a_view():
    """It cost the buyer something. Views are noisy; purchases are not."""
    assert (affinity_rules.EVENT_WEIGHTS["purchase"]
            > affinity_rules.EVENT_WEIGHTS["cart_add"]
            > affinity_rules.EVENT_WEIGHTS["view"])


def test_the_strongest_interest_normalises_to_one():
    profile = build_profile(many(6, category=ELECTRONICS), NOW)
    assert profile.categories[ELECTRONICS] == 1.0


def test_weights_are_relative_not_absolute():
    """Or a heavy buyer would outrank a light one on identical preferences."""
    light = build_profile(many(6, category=ELECTRONICS), NOW)
    heavy = build_profile(many(600, category=ELECTRONICS), NOW)
    assert light.categories == heavy.categories


def test_two_interests_are_ranked_against_each_other():
    events = many(8, category=ELECTRONICS) + many(2, category=GROCERY)
    profile = build_profile(events, NOW)
    assert profile.categories[ELECTRONICS] == 1.0
    assert 0 < profile.categories[GROCERY] < 1.0


def test_recent_interest_outweighs_old_interest():
    """The same count, months apart."""
    events = (many(5, category=GROCERY, days_ago=0)
              + many(5, category=ELECTRONICS, days_ago=120))
    profile = build_profile(events, NOW)
    assert profile.categories[GROCERY] > profile.categories[ELECTRONICS]


def test_an_unknown_event_kind_is_skipped_not_guessed():
    profile = build_profile([event(kind="teleported")], NOW)
    assert profile.total_weight == 0


def test_a_missing_category_does_not_become_a_phantom_bucket():
    """Or "None" would compete with real categories."""
    profile = build_profile(many(6, category=None), NOW)
    assert profile.categories == {}
    assert profile.sellers[SHOP_A] == 1.0


def test_no_history_is_an_empty_profile():
    profile = build_profile([], NOW)
    assert not profile.known
    assert profile.categories == {}


# ---------------------------------------------------------------------------
# the brakes
# ---------------------------------------------------------------------------

def test_three_clicks_is_not_a_profile():
    """Below the threshold there is no opinion, not a confident wrong one."""
    profile = build_profile(many(2, kind="view"), NOW)
    assert not profile.known
    assert affinity_for(profile, ELECTRONICS, SHOP_A) is None


def test_enough_history_becomes_an_opinion():
    profile = build_profile(many(6, kind="view"), NOW)
    assert profile.known
    assert affinity_for(profile, ELECTRONICS, SHOP_A) is not None


def test_a_single_interest_cannot_take_over_the_marketplace():
    """A buyer whose entire history is one category still tops out.

    Without the cap, someone who has only bought electronics gets a
    marketplace made entirely of electronics, and never discovers it sells
    anything else.
    """
    profile = build_profile(many(50, kind="purchase", category=ELECTRONICS), NOW)
    score = affinity_for(profile, ELECTRONICS, SHOP_A)
    assert score <= affinity_rules.MAX_SINGLE_AFFINITY
    assert score < 1.0


def test_a_product_matching_nothing_is_neutral_not_penalised():
    """The line that keeps the bubble open.

    A buyer's history is evidence about what they like. It is not evidence
    about what they dislike, and treating absence as dislike is exactly how a
    catalogue closes around someone.

    Asserted on the multiplier rather than the affinity, because that is the
    thing that actually reaches the score -- affinity 0 would be a ten percent
    penalty, and only the neutral value comes out as exactly 1.0.
    """

    profile = build_profile(many(10, category=ELECTRONICS, seller=SHOP_A), NOW)
    unmatched = affinity_for(profile, GROCERY, SHOP_B)
    assert personalisation_boost(unmatched) == pytest.approx(1.0)


def test_the_neutral_affinity_is_derived_from_ranking_not_guessed():
    """NEUTRAL_AFFINITY is restated in this module because ranking_rules lives
    in another service. This is the check that keeps the two honest.

    If the personalisation range ever moves, this fails rather than letting
    "no opinion" quietly become a penalty or a boost.
    """

    derived = ((1.0 - PERSONALISATION_MIN)
               / (PERSONALISATION_MAX - PERSONALISATION_MIN))
    assert affinity_rules.NEUTRAL_AFFINITY == pytest.approx(derived)
    assert personalisation_boost(
        affinity_rules.NEUTRAL_AFFINITY) == pytest.approx(1.0)


def test_zero_affinity_would_be_a_penalty_which_is_why_it_is_never_returned():
    """The mistake this design exists to avoid.

    An unfamiliar seller must not cost a product ten percent of its score.
    """
    assert personalisation_boost(0.0) < 1.0


def test_affinity_is_never_zero():
    """0 would mean actively suppressed. Unknown must never mean disliked."""
    profile = build_profile(many(10, category=ELECTRONICS), NOW)
    for category, seller in [(GROCERY, SHOP_B), (None, None), ("x", "y")]:
        score = affinity_for(profile, category, seller)
        assert score is None or score >= affinity_rules.NEUTRAL_AFFINITY


def test_category_outweighs_seller():
    """Someone who buys electronics likes electronics.

    Someone who bought once from a shop may simply have wanted that item, and
    weighting the seller heavily is how a marketplace turns into a
    single-seller storefront for its best customers.
    """
    profile = build_profile(
        many(5, category=ELECTRONICS, seller=SHOP_A)
        + many(5, category=GROCERY, seller=SHOP_B), NOW)
    category_only = affinity_for(profile, ELECTRONICS, "unknown-seller")
    seller_only = affinity_for(profile, "unknown-category", SHOP_A)
    assert category_only > seller_only


def test_the_personalisation_range_stays_narrow():
    """Cross-checked against ranking_rules, which owns the multiplier.

    Personalisation that can double a score stops being a search engine. Its
    range is deliberately tighter than quality's.
    """
    personalisation_span = PERSONALISATION_MAX - PERSONALISATION_MIN
    quality_span = QUALITY_MAX - QUALITY_MIN
    assert personalisation_span < quality_span


# ---------------------------------------------------------------------------
# scoring a candidate
# ---------------------------------------------------------------------------

def test_a_strong_match_scores_higher_than_a_weak_one():
    profile = build_profile(
        many(8, category=ELECTRONICS) + many(2, category=GROCERY), NOW)
    assert (affinity_for(profile, ELECTRONICS, SHOP_A)
            > affinity_for(profile, GROCERY, SHOP_A))


def test_one_missing_dimension_does_not_penalise_the_other():
    """A perfect category match with an unknown seller is still a boost.

    The missing dimension contributes the neutral value, so the result lands
    between neutral and a full match -- partly known, which is what it is.
    """

    profile = build_profile(many(10, category=ELECTRONICS, seller=None), NOW)
    score = affinity_for(profile, ELECTRONICS, None)
    assert score > affinity_rules.NEUTRAL_AFFINITY
    assert personalisation_boost(score) > 1.0


def test_a_product_matching_nothing_scores_exactly_neutral():
    profile = build_profile(many(10, category=ELECTRONICS), NOW)
    assert affinity_for(profile, None, None) == pytest.approx(
        affinity_rules.NEUTRAL_AFFINITY)


def test_no_profile_is_no_opinion():
    assert affinity_for(None, ELECTRONICS, SHOP_A) is None


def test_a_product_with_no_dimensions_is_no_opinion():
    """Neutral rather than None, but the same 1.0 multiplier either way."""
    profile = build_profile(many(10), NOW)
    assert affinity_for(profile, None, None) == pytest.approx(
        affinity_rules.NEUTRAL_AFFINITY)


# ---------------------------------------------------------------------------
# the wire form
# ---------------------------------------------------------------------------

def test_the_wire_form_round_trips():
    profile = build_profile(
        many(6, category=ELECTRONICS) + many(3, category=GROCERY), NOW)
    restored = profile_from_dict(profile_as_dict(profile))
    assert affinity_for(restored, ELECTRONICS, SHOP_A) == pytest.approx(
        affinity_for(profile, ELECTRONICS, SHOP_A))


def test_how_much_someone_has_bought_is_not_sent():
    """It is not a ranking input, and shipping it copies a fact nobody needs."""
    profile = build_profile(many(200, kind="purchase"), NOW)
    wire = profile_as_dict(profile)
    assert "total_weight" not in wire
    assert wire["known"] is True


def test_the_tail_is_trimmed():
    """A long history has a long tail of weights that cannot change ordering."""
    events = []
    for i in range(100):
        events += many(1, category=f"cat-{i}")
    wire = profile_as_dict(build_profile(events, NOW), limit=20)
    assert len(wire["categories"]) == 20


def test_an_unknown_profile_does_not_reconstruct():
    assert profile_from_dict(None) is None
    assert profile_from_dict({}) is None
    assert profile_from_dict({"known": False, "categories": {"a": 1.0}}) is None


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def test_behaviour_is_not_kept_forever():
    """Past four half-lives an event contributes about 3% and cannot reorder.

    Keeping it buys nothing and holds a record of what somebody looked at a
    year ago.
    """
    cutoff = affinity_rules.retention_cutoff(NOW)
    assert cutoff < NOW
    assert decay_factor(cutoff, NOW) < 0.001
