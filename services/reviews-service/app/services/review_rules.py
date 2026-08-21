"""
Who may review, what a review may say, and what a pile of them adds up to.

Pure: no I/O, no clock of its own, no database. Everything here answers a
question of the form "given this purchase and this submission, is it allowed,
and what do these ratings mean" -- which is the part worth testing exhaustively
and the part that is expensive to get wrong.

Reviews are the input the ranking formula has been missing since it was
written. ARCHITECTURE §3g scores products as

    text_relevance x quality_boost x distance_decay x personalisation

and `quality_boost` reads a rating and a review count that no service produced,
so they were reported as None and treated as neutral. That was the honest
placeholder -- inventing an average would have put a number into the formula
that looked like evidence -- but it left a third of the scoring inert. This is
the module that ends that.

Two decisions shape everything below.

**Verified purchase only.** A review requires a delivered seller order
containing that product. Not because unverified reviews are always false, but
because under COD delivery is the one fact the platform already knows for
certain, and it is the strongest anti-gaming signal available without a fraud
model. An open review endpoint is a free vote, and free votes get bought.

**Both the product and the seller are rated, separately.** They are different
claims that a single star cannot carry: an excellent product packed badly, or a
mediocre product from a seller who did everything right. Ranking needs the
seller signal (§3g); buyers think in products. One submission, two aggregates.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional, Sequence, Set


# Ratings are whole stars. A 4.5-star submission is a UI affordance, not a
# measurement, and allowing fractions here would mean the stored distribution
# could not be reasoned about as counts.
MIN_RATING = 1
MAX_RATING = 5

# How long after delivery a review may still be left.
#
# Not forever, for two reasons. A review left a year later is describing a
# product that may have changed hands, formula or factory, and an account with
# an indefinitely open write is an asset worth buying. Ninety days is long
# enough for a slow parcel, a holiday, and a change of mind.
REVIEW_WINDOW_DAYS = 90

# How long the author may revise what they wrote.
#
# Shorter than the window on purpose: editing exists to fix a mistake made in
# the moment, not to let a five-star review be quietly rewritten weeks later
# after a seller offers something for it.
EDIT_WINDOW_DAYS = 14


class Verdict(str, Enum):
    """Why a review was or was not accepted."""

    ALLOWED = "allowed"
    NOT_DELIVERED = "not_delivered"          # the goods never arrived
    NOT_IN_ORDER = "not_in_order"            # that product was not bought here
    ALREADY_REVIEWED = "already_reviewed"    # one review per purchase
    WINDOW_CLOSED = "window_closed"          # too long after delivery
    NOT_THE_BUYER = "not_the_buyer"          # someone else's purchase


# Seller-order states in which the buyer actually received the goods.
#
# SETTLED is included because it is DELIVERED plus the courier having remitted;
# the buyer's experience is identical and the money moving later is not their
# business.
#
# RETURNED and RTO_IN_TRANSIT are deliberately excluded, and this is the one
# that is easy to get wrong. Under COD a refusal happens at the door: the buyer
# never opened the box, never used the product, and has nothing to say about it
# that a rating can carry. Their complaint is about the seller or the courier,
# and routing it through a product rating would put a one-star review on an
# item nobody has tried. That signal belongs in the fulfilment metrics, where
# a return already counts against the seller -- and counting it twice, once as
# a return rate and once as a rating, would be double-penalising one event.
DELIVERED_STATES: Set[str] = {"DELIVERED", "SETTLED"}


@dataclass(frozen=True)
class Eligibility:
    verdict: Verdict
    detail: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOWED

    @property
    def http_status(self) -> int:
        """What the API should answer.

        403 for "not yours" and "not delivered" -- the caller is asking about
        something real that they may not do. 404 for a product that was not in
        the order, so a caller cannot probe which orders contain what. 409 for
        a duplicate, which is a state conflict rather than a permission
        problem, and 422 for a closed window.
        """
        return {
            Verdict.ALLOWED: 200,
            Verdict.NOT_THE_BUYER: 403,
            Verdict.NOT_DELIVERED: 403,
            Verdict.NOT_IN_ORDER: 404,
            Verdict.ALREADY_REVIEWED: 409,
            Verdict.WINDOW_CLOSED: 422,
        }[self.verdict]


def may_review(*, buyer_id: Any, order_buyer_id: Any, seller_order_status: Any,
               product_ids_in_order: Sequence[Any], product_id: Any,
               delivered_at: Optional[datetime], now: datetime,
               already_reviewed: bool,
               window_days: int = REVIEW_WINDOW_DAYS) -> Eligibility:
    """Whether this buyer may review this product from this purchase.

    Checked in the order a person would: is this yours, did it arrive, was it
    even in the box, have you already said something, and is it still recent
    enough to matter. The order matters for what the caller learns -- ownership
    is settled before anything about the order's contents is revealed.
    """
    if str(buyer_id) != str(order_buyer_id):
        return Eligibility(Verdict.NOT_THE_BUYER,
                           "a purchase may only be reviewed by its buyer")

    status = str(seller_order_status)
    if status not in DELIVERED_STATES:
        return Eligibility(
            Verdict.NOT_DELIVERED,
            f"this order is {status}; a product can only be reviewed once it "
            f"has been delivered")

    if str(product_id) not in {str(p) for p in product_ids_in_order}:
        return Eligibility(Verdict.NOT_IN_ORDER,
                           "that product was not part of this order")

    if already_reviewed:
        return Eligibility(
            Verdict.ALREADY_REVIEWED,
            "this purchase has already been reviewed; edit that review "
            "instead of adding another")

    # A delivered order with no delivery timestamp is a data fault, not a
    # buyer's problem. Allowing it is the kinder failure: the alternative
    # silently denies a legitimate review because a column was never set.
    if delivered_at is not None:
        if now - delivered_at > timedelta(days=window_days):
            return Eligibility(
                Verdict.WINDOW_CLOSED,
                f"reviews close {window_days} days after delivery")

    return Eligibility(Verdict.ALLOWED)


def may_edit(*, authored_at: datetime, now: datetime,
             window_days: int = EDIT_WINDOW_DAYS) -> bool:
    """Whether the author may still revise what they wrote.

    Deliberately shorter than the review window. Editing is for fixing what you
    meant to say, not for reopening the rating weeks later when a seller has
    made it worth your while.
    """
    return now - authored_at <= timedelta(days=window_days)


def validate_rating(value: Any, field: str) -> int:
    """A whole number of stars, or a clear error naming the field."""
    if isinstance(value, bool) or not isinstance(value, int):
        return _reject(f"{field} must be a whole number of stars, "
                       f"got {value!r}")
    if not MIN_RATING <= value <= MAX_RATING:
        return _reject(f"{field} must be between {MIN_RATING} and "
                       f"{MAX_RATING}, got {value}")
    return value


def _reject(message: str):
    raise ValueError(message)


@dataclass(frozen=True)
class Aggregate:
    """What a pile of ratings adds up to."""

    average: Optional[float]
    count: int

    @property
    def known(self) -> bool:
        return self.count > 0


def aggregate_ratings(ratings: Sequence[Optional[int]]) -> Aggregate:
    """The plain mean and the count. Deliberately not shrunk.

    This is the decision most likely to be "corrected" later into a bug, so it
    is worth being explicit: **the shrinking happens in ranking, not here.**

    `ranking_rules.rating_component` already pulls a rating toward neutral in
    proportion to how few reviews back it, using REVIEW_CONFIDENCE_AT. If this
    module also shrank, a seller with three reviews would be pulled toward
    average twice -- once here and once there -- and the ranking would be
    quietly flatter than either module claims. Neither would look wrong on its
    own.

    So this reports what is actually true: the mean of the ratings given, and
    how many there were. The count is what lets the consumer decide how much to
    believe the mean, and it is reported precisely so that decision is made in
    one place.

    None entries are skipped rather than counted as zero -- a buyer who rated
    the product but not the seller has not given the seller nought stars.
    """
    present = [r for r in ratings if r is not None]
    if not present:
        return Aggregate(None, 0)
    return Aggregate(sum(present) / len(present), len(present))


def distribution(ratings: Sequence[Optional[int]]) -> dict:
    """How many of each star value.

    Shown to sellers and buyers rather than used in scoring. An average of 3.0
    from twenty threes and an average of 3.0 from ten fives and ten ones are
    completely different products, and only the distribution says so.
    """
    counts = {star: 0 for star in range(MIN_RATING, MAX_RATING + 1)}
    for rating in ratings:
        if rating is None:
            continue
        if rating in counts:
            counts[rating] += 1
    return counts


def quality_signal(seller_aggregate: Aggregate) -> dict:
    """The shape `seller_metrics.quality_inputs` expects for its two arguments.

    Kept here so the contract between reviews and ranking is written down once.
    A seller nobody has rated reports None rather than a neutral number: §3g's
    cold-start rule is that unknown must be treated as *average* by the
    consumer, and it can only do that if it can tell unknown from average.
    """
    return {
        "rating": seller_aggregate.average,
        "review_count": seller_aggregate.count,
    }
