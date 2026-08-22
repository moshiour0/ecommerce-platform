"""
What a buyer has shown interest in, and how much that should move a result.

Pure: no database, no clock of its own, no I/O. Everything takes the times it
needs as arguments so the decay is testable without waiting a month.

This is the last inert term of the ranking formula (ARCHITECTURE §3g):

    final_score = text_relevance x quality_boost x distance_decay x personalisation

`affinity` has been passed as None everywhere since the formula was written,
because nothing recorded what a buyer had looked at. None means exactly 1.0,
so the term has been correct and idle. This module ends that.

The thing to be careful about
-----------------------------
Personalisation is the term where being wrong is least visible and most
harmful. A ranking that over-fits to history stops being a search engine and
becomes a filter bubble: the first complaint is always "I cannot find the thing
I know you sell", and by then the behaviour that caused it is months old.

Three deliberate brakes:

* The multiplier range in ranking_rules is 0.9 to 1.15 -- narrower than
  quality's 0.7 to 1.3. Personalisation may nudge a comparable result; it may
  never rescue an irrelevant one.
* Affinity is capped below 1.0 for a single dominant interest, so a buyer who
  has only ever bought one category does not get a marketplace consisting of
  that category.
* Below MIN_EVENTS_FOR_AFFINITY the answer is None -- neutral -- rather than a
  confident profile built from three clicks.

And the whole thing is opt-in by construction: affinity requires an identified
buyer, and an anonymous search gets None, which scores exactly as it did
yesterday.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Sequence


# What kind of behaviour a row records, and what it is worth.
#
# A purchase is worth far more than a view because it cost the buyer something.
# Views are noisy -- a mis-click, a price check, someone else on the same
# account -- but there are many more of them, which is why they are not simply
# ignored.
#
# A cart add sits between: real intent, no commitment. Abandonment is common
# enough that treating it as a purchase would over-weight window shopping.
EVENT_WEIGHTS: Dict[str, float] = {
    "view": 1.0,
    "cart_add": 3.0,
    "purchase": 8.0,
}

# How quickly interest goes stale. At one half-life an event counts half as
# much as it did when it happened.
#
# Thirty days rather than a year: what someone wanted last spring is a worse
# predictor than what they wanted last week, and a long half-life is what makes
# a profile impossible to escape. A buyer who has moved on should be able to
# move on.
HALF_LIFE_DAYS = 30.0

# Below this many weighted events, the profile is not evidence and affinity is
# None. Three clicks is a coincidence.
MIN_EVENTS_FOR_AFFINITY = 5

# The most any single interest can contribute. A buyer whose entire history is
# one category still tops out here, so the marketplace does not collapse to
# that category.
MAX_SINGLE_AFFINITY = 0.85

# How much each dimension is worth when combining them.
#
# Category outweighs seller on purpose. Someone who buys electronics likes
# electronics; someone who bought once from a particular shop may simply have
# wanted that item. Weighting the seller heavily is also how a marketplace
# quietly turns into a single-seller storefront for its best customers.
CATEGORY_WEIGHT = 0.7
SELLER_WEIGHT = 0.3

# The affinity that means "no opinion".
#
# This is not 0, and the difference matters more than it looks.
# `personalisation_boost` maps affinity 0 to 0.9 -- a ten percent *penalty* --
# and only `None` to exactly 1.0. So a dimension we know nothing about cannot
# contribute 0: that would actively demote a product for the crime of having an
# unfamiliar seller.
#
# Derived from ranking's own constants, so the two cannot drift:
#
#     boost(a) = MIN + (MAX - MIN) * a          = 1.0
#     a        = (1.0 - MIN) / (MAX - MIN)      = (1.0 - 0.9) / 0.25 = 0.4
#
# Restated here rather than imported because that module lives in another
# service; a test asserts the arithmetic against the real constants.
NEUTRAL_AFFINITY = 0.4


@dataclass(frozen=True)
class BehaviourEvent:
    """One thing a buyer did."""

    kind: str
    category_id: Optional[str]
    seller_id: Optional[str]
    occurred_at: datetime


@dataclass(frozen=True)
class AffinityProfile:
    """What a buyer appears to be interested in.

    Weights are normalised so the strongest interest is 1.0 and everything else
    is a fraction of it. Absolute totals are deliberately not exposed: they
    encode how much a person has bought, which is not something a ranking needs
    and not something worth copying into another service.
    """

    categories: Dict[str, float] = field(default_factory=dict)
    sellers: Dict[str, float] = field(default_factory=dict)
    total_weight: float = 0.0

    @property
    def known(self) -> bool:
        """Whether there is enough here to be evidence rather than noise."""
        return self.total_weight >= MIN_EVENTS_FOR_AFFINITY


def decay_factor(occurred_at: datetime, now: datetime,
                 half_life_days: float = HALF_LIFE_DAYS) -> float:
    """How much an event that happened at `occurred_at` still counts.

    1.0 at the moment it happened, 0.5 one half-life later, and never negative.
    A future timestamp counts as fully current rather than being amplified --
    clock skew between services is ordinary, and letting it inflate a weight
    would make a mis-set clock a ranking exploit.
    """
    if half_life_days <= 0:
        raise ValueError("half life must be positive")

    age_days = (now - occurred_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / half_life_days)


def build_profile(events: Sequence[BehaviourEvent], now: datetime,
                  half_life_days: float = HALF_LIFE_DAYS) -> AffinityProfile:
    """Turn a buyer's history into normalised interests.

    Unknown dimensions are skipped rather than bucketed. An event with no
    category contributes to the seller weights and nothing else, instead of
    creating a phantom "None" category that would then compete with real ones.
    """
    categories: Dict[str, float] = {}
    sellers: Dict[str, float] = {}
    total = 0.0

    for event in events:
        weight = EVENT_WEIGHTS.get(event.kind)
        if weight is None:
            continue  # an event kind this version does not understand
        weight *= decay_factor(event.occurred_at, now, half_life_days)
        total += weight

        if event.category_id:
            categories[str(event.category_id)] = categories.get(
                str(event.category_id), 0.0) + weight
        if event.seller_id:
            sellers[str(event.seller_id)] = sellers.get(
                str(event.seller_id), 0.0) + weight

    return AffinityProfile(categories=_normalise(categories),
                           sellers=_normalise(sellers),
                           total_weight=total)


def _normalise(weights: Dict[str, float]) -> Dict[str, float]:
    """Scale so the strongest interest is 1.0.

    Relative rather than absolute, because what matters is which interests are
    stronger than which -- and because absolute weights would let a heavy buyer
    outrank a light one on identical preferences.
    """
    if not weights:
        return {}
    strongest = max(weights.values())
    if strongest <= 0:
        return {}
    return {key: value / strongest for key, value in weights.items()}


def affinity_for(profile: Optional[AffinityProfile],
                 category_id: Optional[str],
                 seller_id: Optional[str]) -> Optional[float]:
    """How much this buyer looks interested in this product, or None.

    None means "no opinion", and `personalisation_boost` turns that into
    exactly 1.0. It is returned whenever the honest answer is that we do not
    know: no profile, too little history, or a product whose category and
    seller are both absent.

    Never returns 0. A product matching nothing in the profile scores
    NEUTRAL_AFFINITY, which is exactly a 1.0 multiplier -- a buyer's history is
    evidence about what they like, not evidence about what they dislike, and
    treating absence as dislike is precisely how the bubble closes.

    An unmatched or absent dimension contributes NEUTRAL_AFFINITY rather than
    being dropped from the average. Dropping it and rescaling was the first
    attempt, and it silently erased the point of having two weights: rescaling
    a lone category match back up to full strength made it score identically to
    a lone seller match, so CATEGORY_WEIGHT and SELLER_WEIGHT had no effect
    whenever one dimension was missing -- which is most of the time.
    """
    if profile is None or not profile.known:
        return None

    category_score = (profile.categories.get(str(category_id))
                      if category_id else None)
    seller_score = (profile.sellers.get(str(seller_id))
                    if seller_id else None)

    if category_score is None and seller_score is None:
        # Nothing known about either dimension. Explicitly neutral rather than
        # None, so the two cases stay indistinguishable downstream where they
        # should be -- both mean "no opinion" and both must score 1.0.
        return NEUTRAL_AFFINITY

    combined = (CATEGORY_WEIGHT * (category_score if category_score is not None
                                   else NEUTRAL_AFFINITY)
                + SELLER_WEIGHT * (seller_score if seller_score is not None
                                   else NEUTRAL_AFFINITY))

    return min(MAX_SINGLE_AFFINITY, combined)


def profile_as_dict(profile: AffinityProfile, limit: int = 20) -> Dict[str, Any]:
    """The wire form, trimmed to the interests that matter.

    Capped because a profile is fetched once per search and a buyer with a long
    history has a long tail of near-zero weights that cannot change any
    ordering. `known` is reported rather than the raw total: how much someone
    has bought is not a ranking input, and shipping it to another service is
    copying a fact nobody needs.
    """
    return {
        "categories": _top(profile.categories, limit),
        "sellers": _top(profile.sellers, limit),
        "known": profile.known,
    }


def profile_from_dict(data: Optional[Dict[str, Any]]) -> Optional[AffinityProfile]:
    """The inverse, for the consumer side. None for anything unusable."""
    if not data or not data.get("known"):
        return None
    return AffinityProfile(
        categories={str(k): float(v)
                    for k, v in (data.get("categories") or {}).items()},
        sellers={str(k): float(v)
                 for k, v in (data.get("sellers") or {}).items()},
        # Reconstructed as "enough", because `known` already carried that
        # answer and the real total was deliberately not sent.
        total_weight=float(MIN_EVENTS_FOR_AFFINITY),
    )


def _top(weights: Dict[str, float], limit: int) -> Dict[str, float]:
    return dict(sorted(weights.items(), key=lambda kv: kv[1],
                       reverse=True)[:limit])


def retention_cutoff(now: datetime, days: int = 365) -> datetime:
    """The oldest behaviour worth keeping.

    Past four half-lives an event contributes about 3% of its original weight
    and cannot change an ordering, so keeping it buys nothing and holds a
    record of what somebody looked at a year ago. Deleting it is both cheaper
    and less to lose.
    """
    return now - timedelta(days=days)
