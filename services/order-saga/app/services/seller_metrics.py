"""
Seller performance, as pure functions.

The roadmap names these as ranking's inputs (§3.1, D4): on-time dispatch,
cancellation rate, return rate. They are computed from seller orders, which
this service owns, and they exist to be a *quality boost* rather than a
scoreboard — nothing here decides anything on its own.

The rule that matters: small samples must not produce extreme scores
---------------------------------------------------------------------
A seller with one order that was returned has a 100% return rate. Ranking them
last forever on one data point is both unfair and wrong — it is not evidence,
it is noise, and it makes a new seller's first bad day permanent.

So every rate is shrunk toward a prior in proportion to how little is known.
With `PRIOR_WEIGHT` pseudo-observations of average behaviour mixed in, one
returned order out of one moves the seller a little; forty returned out of
forty moves them a lot. The arithmetic is the same one-liner used for rating
averages everywhere, and it is the difference between a metric and a rumour.

What is deliberately absent
---------------------------
`rating` and `review_count`. There is no reviews service, and inventing an
average from nothing would put a number into the ranking formula that looks
like evidence. `quality_inputs` therefore reports them as None, and
ranking_rules treats unrated as *average* rather than as bad — the cold-start
choice that lets a new seller be found at all.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

# How long a confirmed order may sit before dispatch counts as late.
#
# 24 hours because that is what a Bangladeshi marketplace's couriers actually
# collect on, and because a shorter window would mark a seller late for
# ordering a pickup the same evening. It is a constant rather than a config
# value until somebody has data to argue with.
ON_TIME_DISPATCH_HOURS = 24

# Pseudo-observations of average behaviour mixed into every rate.
#
# Twenty is a judgement: it means a seller's first handful of orders barely
# move their score, and by fifty orders the prior is mostly washed out. Too
# small and one bad week is permanent; too large and a genuinely bad seller
# stays hidden behind the prior for months.
PRIOR_WEIGHT = 20

# What "average" is assumed to be before a seller has a record. Chosen to be
# unremarkable rather than optimistic: a new seller should rank like a typical
# one, not better.
PRIOR_ON_TIME_RATE = 0.90
PRIOR_CANCELLATION_RATE = 0.05
PRIOR_RETURN_RATE = 0.10

# Statuses that mean the seller committed to the order at all. An order
# cancelled before the seller ever confirmed is not their failure.
COMMITTED = {"CONFIRMED", "DISPATCHED", "DELIVERED", "SETTLED",
             "RTO_IN_TRANSIT", "RETURNED"}

# Statuses where the parcel reached a conclusion, one way or the other.
CONCLUDED = {"DELIVERED", "SETTLED", "RETURNED"}

RETURNED = {"RETURNED"}
DELIVERED = {"DELIVERED", "SETTLED"}


@dataclass(frozen=True)
class SellerMetrics:
    seller_order_count: int
    committed_count: int
    concluded_count: int

    on_time_dispatch_rate: float
    cancellation_rate: float
    return_rate: float

    # How much the numbers above are actually the seller's own record rather
    # than the prior. Reported so a caller can say "not enough data yet"
    # instead of presenting a shrunk estimate as a measurement.
    confidence: float

    def as_dict(self) -> dict:
        return {
            "seller_order_count": self.seller_order_count,
            "committed_count": self.committed_count,
            "concluded_count": self.concluded_count,
            "on_time_dispatch_rate": round(self.on_time_dispatch_rate, 4),
            "cancellation_rate": round(self.cancellation_rate, 4),
            "return_rate": round(self.return_rate, 4),
            "confidence": round(self.confidence, 4),
        }


def shrink(successes: float, observations: float, prior_rate: float,
           prior_weight: float = PRIOR_WEIGHT) -> float:
    """A rate pulled toward `prior_rate` in proportion to how little is known.

    With no observations this is exactly the prior. With many it is almost
    entirely the observed rate. The point is the middle: one returned order out
    of one is not a 100% return rate, it is one returned order.
    """
    if observations < 0 or prior_weight < 0:
        raise ValueError("observations and prior weight must not be negative")
    total = observations + prior_weight
    if total == 0:
        return prior_rate
    return (successes + prior_rate * prior_weight) / total


def hours_between(earlier, later) -> Optional[float]:
    """Hours from one timestamp to another, or None if either is missing.

    None rather than zero: a missing dispatch time means "not dispatched yet",
    and counting that as instant would make every undispatched order perfectly
    on time.
    """
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 3600.0


def compute_metrics(seller_orders: Iterable[dict]) -> SellerMetrics:
    """Everything ranking needs about one seller's fulfilment record.

    Each entry needs `status`, and optionally `confirmed_at` and
    `dispatched_at`. Orders the seller never confirmed are counted in the total
    but excluded from the rates: a buyer cancelling before the seller saw the
    order is not the seller's failure, and counting it would punish sellers for
    being in a category buyers browse indecisively.
    """
    orders = [o for o in (seller_orders or []) if isinstance(o, dict)]
    total = len(orders)

    committed = [o for o in orders if o.get("status") in COMMITTED]
    concluded = [o for o in orders if o.get("status") in CONCLUDED]

    # Cancellations, over orders the seller had actually accepted plus the
    # cancellations themselves -- the denominator is "orders that reached the
    # seller", not "orders that finished".
    cancelled_after_commit = [
        o for o in orders
        if o.get("status") == "CANCELLED" and o.get("confirmed_at") is not None]
    cancellation_base = len(committed) + len(cancelled_after_commit)
    cancellation_rate = shrink(
        len(cancelled_after_commit), cancellation_base, PRIOR_CANCELLATION_RATE)

    # Returns, over parcels that reached a conclusion. An order still in
    # transit is not evidence either way.
    returned = [o for o in concluded if o.get("status") in RETURNED]
    return_rate = shrink(len(returned), len(concluded), PRIOR_RETURN_RATE)

    # On-time dispatch, over orders that were actually dispatched. An order
    # confirmed an hour ago and not yet dispatched is not late; it is pending,
    # and treating pending as late would punish a seller for the clock.
    dispatched = [o for o in committed if o.get("dispatched_at") is not None]
    on_time = 0
    for order in dispatched:
        elapsed = hours_between(order.get("confirmed_at"),
                                order.get("dispatched_at"))
        # A dispatch with no confirmation time cannot be judged, so it counts
        # as on time rather than as a failure invented by missing data.
        if elapsed is None or elapsed <= ON_TIME_DISPATCH_HOURS:
            on_time += 1
    on_time_rate = shrink(on_time, len(dispatched), PRIOR_ON_TIME_RATE)

    # How much of the above is the seller's own record. Uses the largest
    # denominator, because a seller with fifty concluded orders is well known
    # even if only two were ever cancelled.
    observations = max(len(committed), len(concluded), len(dispatched))
    confidence = observations / (observations + PRIOR_WEIGHT) if observations else 0.0

    return SellerMetrics(
        seller_order_count=total,
        committed_count=len(committed),
        concluded_count=len(concluded),
        on_time_dispatch_rate=on_time_rate,
        cancellation_rate=cancellation_rate,
        return_rate=return_rate,
        confidence=confidence,
    )


def quality_inputs(metrics: SellerMetrics,
                   rating: Optional[float] = None,
                   review_count: Optional[int] = None) -> Dict[str, Optional[float]]:
    """The shape ranking_rules.quality_boost consumes.

    `rating` and `review_count` are passed through as None because there is no
    reviews service. Reporting an invented average would put a number into the
    ranking formula that looks like evidence; None lets ranking treat the
    seller as *average* rather than as bad, which is the cold-start behaviour
    that lets a new seller be found at all.
    """
    return {
        "on_time_dispatch_rate": metrics.on_time_dispatch_rate,
        "cancellation_rate": metrics.cancellation_rate,
        "return_rate": metrics.return_rate,
        "confidence": metrics.confidence,
        "rating": rating,
        "review_count": review_count,
    }
