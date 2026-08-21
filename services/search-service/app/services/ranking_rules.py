"""
One ranking pipeline, as pure functions.

The roadmap's D4 is explicit that personalised results, nearest-shop preference
and location search are not three features. Built separately they fight each
other and produce incoherent results: a "near me" toggle that contradicts the
default sort, a personalisation layer that reorders what proximity just
ordered. They are one scoring function.

    final_score = text_relevance
                × quality_boost      (rating, reviews, dispatch, low RTO)
                × distance_decay     (gauss decay, ~10km scale, floored)
                × personalisation    (affinity to category / brand / seller)

**Multiplicative, not additive.** An additive score lets a large distance bonus
compensate for irrelevance, which is how a search for "keyboard" returns a
nearby grocer. Multiplying means every factor is a proportion of what the
relevance already established: nothing can rescue a product the query did not
match.

**Distance is a decay, never a filter.** The stated rule -- quality first, then
proximity -- is a scoring weight rather than a sort order. A shop 40 km away
with excellent reviews still outranks a mediocre shop next door, and that is
the behaviour asked for. It is also the behaviour the constants below were
*derived* from rather than decorated with: see DISTANCE_FLOOR, where the
roadmap's illustrative 0.6 turns out to produce the opposite result.

Hard-filtering by radius is the other mistake -- the one that makes a
marketplace feel empty in low-density areas, where the nearest seller of
anything specific is routinely an hour away.

The floor is what makes it a decay rather than an exclusion: distance can cost
a result at most `1 - DISTANCE_FLOOR` of its score, so a genuinely better
product from far away is never buried, only nudged.

Cold start
----------
Unrated is treated as **average**, not as bad. A new seller with no reviews
whose score was multiplied by a low quality boost would never be seen, would
therefore never earn a review, and would stay unseen -- the ranking would be
enforcing its own prior. So a missing rating contributes exactly 1.0.
"""

import math
from typing import Any, Dict, Optional

# The gauss decay's scale: at this distance the decay has fallen to about half
# of its range above the floor. Ten kilometres is a city, which is the unit
# buyers actually think in.
DISTANCE_SCALE_KM = 10.0

# The most distance can cost a result. A shop on the other side of the country
# keeps this fraction of its score.
#
# **Derived, not chosen.** The roadmap's D4 illustrates this with 0.6, and 0.6
# does not deliver the behaviour D4 promises in the same paragraph -- that a
# 40 km shop with excellent reviews outranks a mediocre one next door. The
# arithmetic:
#
#   at 40 km with a 10 km scale the decay is ~0, so the far result scores
#   quality_far x floor, and the near one scores quality_near x ~1.0.
#   Quality wins only when  quality_far / quality_near > 1 / floor.
#
# A realistic excellent-versus-mediocre gap, with the quality range below, is
# about 1.34 -- an excellent seller reaches ~1.15 and a poor one ~0.86, because
# neither extreme of the clamp is reachable on plausible inputs. That requires
# a floor above 0.748. At 0.6 the mediocre neighbour wins by 24%, which is the
# opposite of the stated rule.
#
# 0.80 rather than 0.75 for margin: 0.75 satisfies the inequality by 0.4%,
# which is not a design, it is a coincidence that the next change to the
# quality weights would silently undo.
#
# The consequence is deliberate and worth stating plainly: proximity is a
# tiebreaker between comparable results, not a major factor. That is what
# "first priority to products and reviews, then the nearest shop" means when
# it is written as arithmetic.
DISTANCE_FLOOR = 0.80

# How far quality can move a result, either way. A range of 0.7 to 1.3 means
# quality is a strong tiebreaker between comparable matches and cannot rescue
# an irrelevant one.
QUALITY_MIN = 0.7
QUALITY_MAX = 1.3

# The same, for personalisation, and deliberately narrower. Personalisation
# that can double a score stops being a search engine and becomes a filter
# bubble, and the first complaint is always "I cannot find the thing I know
# you sell".
PERSONALISATION_MIN = 0.9
PERSONALISATION_MAX = 1.15

# Ratings are out of five. A rating exactly at the midpoint is neutral, so an
# average seller is neither boosted nor penalised.
RATING_SCALE = 5.0
RATING_NEUTRAL = 3.5

# Reviews below this count are discounted toward neutral, for the same reason
# seller metrics are shrunk: three five-star reviews are not evidence.
REVIEW_CONFIDENCE_AT = 20.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def distance_decay(distance_km: Optional[float],
                   scale_km: float = DISTANCE_SCALE_KM,
                   floor: float = DISTANCE_FLOOR) -> float:
    """A gaussian decay from 1.0 at zero distance down to `floor`.

    Unknown distance returns 1.0 rather than the floor. A seller who has not
    set coordinates is not far away -- they are unlocated, and penalising them
    for it would rank shops by how completely they filled in their profile.
    """
    if distance_km is None:
        return 1.0
    if distance_km < 0:
        raise ValueError(f"distance must not be negative, got {distance_km}")
    if scale_km <= 0:
        raise ValueError("scale must be positive")

    decay = math.exp(-0.5 * (distance_km / scale_km) ** 2)
    return floor + (1.0 - floor) * decay


def rating_component(rating: Optional[float],
                     review_count: Optional[int]) -> float:
    """A multiplier from reviews, centred on 1.0.

    Absent reviews are exactly neutral. That is the cold-start decision: a new
    seller whose score was cut for having no reviews would never be seen,
    never earn one, and stay unseen -- the ranking enforcing its own prior.

    Few reviews are pulled toward neutral for the same reason seller metrics
    are shrunk. Three five-star reviews are an opinion, not a measurement.
    """
    if rating is None:
        return 1.0

    if not 0 <= rating <= RATING_SCALE:
        raise ValueError(f"rating must be within 0..{RATING_SCALE}, got {rating}")

    count = max(0, int(review_count or 0))
    confidence = count / (count + REVIEW_CONFIDENCE_AT)

    # -1 at zero stars, 0 at neutral, +1 at five.
    raw = (rating - RATING_NEUTRAL) / max(RATING_NEUTRAL,
                                          RATING_SCALE - RATING_NEUTRAL)
    return 1.0 + raw * confidence * 0.3


def quality_boost(inputs: Optional[Dict[str, Any]]) -> float:
    """How much this seller's record should move a result.

    Combines review sentiment with fulfilment behaviour -- on-time dispatch and
    the two rates that mean a buyer did not get what they ordered. Returns 1.0
    for a seller nothing is known about, which is the same cold-start rule as
    ratings and for the same reason.

    Clamped, so no single catastrophic input can zero a result. A seller with a
    100% return rate should sink, not vanish: vanishing removes the evidence
    that anything is wrong from every screen an operator looks at.
    """
    if not inputs:
        return 1.0

    boost = rating_component(inputs.get("rating"), inputs.get("review_count"))

    # Fulfilment moves the score only as far as the data justifies. A seller
    # with three orders barely moves; one with three hundred moves fully.
    confidence = _clamp(float(inputs.get("confidence") or 0.0), 0.0, 1.0)
    if confidence > 0:
        on_time = _clamp(float(inputs.get("on_time_dispatch_rate") or 0.0), 0.0, 1.0)
        cancellation = _clamp(float(inputs.get("cancellation_rate") or 0.0), 0.0, 1.0)
        returns = _clamp(float(inputs.get("return_rate") or 0.0), 0.0, 1.0)

        # Centred so an average seller contributes nothing. Returns weigh most:
        # under COD a return is the loss vector, and a seller who generates
        # them is costing the platform shipping twice.
        fulfilment = ((on_time - 0.90) * 0.10
                      - (cancellation - 0.05) * 0.30
                      - (returns - 0.10) * 0.40)
        boost += fulfilment * confidence

    return _clamp(boost, QUALITY_MIN, QUALITY_MAX)


def personalisation_boost(affinity: Optional[float]) -> float:
    """Affinity to this category, brand or seller, from 0 to 1.

    Deliberately the narrowest term. Personalisation that can double a score
    stops being a search engine and becomes a filter bubble, and the first
    complaint is always "I cannot find the thing I know you sell".
    """
    if affinity is None:
        return 1.0
    affinity = _clamp(float(affinity), 0.0, 1.0)
    return PERSONALISATION_MIN + (PERSONALISATION_MAX - PERSONALISATION_MIN) * affinity


def final_score(text_relevance: float,
                quality_inputs: Optional[Dict[str, Any]] = None,
                distance_km: Optional[float] = None,
                affinity: Optional[float] = None) -> float:
    """The one score every surface sorts by.

    Multiplicative on purpose: every factor is a proportion of what relevance
    already established, so nothing can rescue a product the query did not
    match. An additive score would let a large proximity bonus compensate for
    irrelevance, which is how a search for "keyboard" returns a nearby grocer.
    """
    if text_relevance < 0:
        raise ValueError(f"relevance must not be negative, got {text_relevance}")

    return (text_relevance
            * quality_boost(quality_inputs)
            * distance_decay(distance_km)
            * personalisation_boost(affinity))


def rank(candidates) -> list:
    """Score and order a candidate set, highest first.

    The tiebreak is the product id, so an identical set always comes back in an
    identical order. Without it two products with the same score swap places
    between requests, pagination duplicates and drops results, and the bug
    reproduces roughly half the time.
    """
    scored = []
    for candidate in candidates or []:
        score = final_score(
            candidate.get("relevance", 1.0),
            candidate.get("quality"),
            candidate.get("distance_km"),
            candidate.get("affinity"),
        )
        scored.append({**candidate, "score": score})

    scored.sort(key=lambda c: (-c["score"], str(c.get("id", ""))))
    return scored
