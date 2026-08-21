"""
Splitting one buyer's order into one order per seller, as pure functions.

A marketplace order is one thing to the buyer and several things to everybody
else. The buyer pays once, gets one confirmation and one order number. But
stock comes from a particular seller, a courier collects from that seller's
address, the money is owed to that seller minus commission, and a refused
delivery is that seller's return. Every step after checkout is per-seller, and
an order that cannot express that has to grow a `seller_id` filter into each
of those steps instead.

So checkout produces one `Order` and N `SellerOrder`s, and this module decides
how.

The parent status is derived, never stored
------------------------------------------
`derive_order_status` computes the buyer-facing status from the children. It
is not a column somebody updates. Two sources for "is this order delivered" is
two answers the first time one seller is late, and the one that gets shown is
whichever the query happened to read.

The rule is **least advanced wins**: an order is only as complete as its
slowest part. A buyer whose electronics arrived and whose clothes are still in
a van has an order that is in transit, not an order that is delivered.

Money
-----
`subtotal_cents` on a SellerOrder is the sum of that seller's line totals --
goods only. The parent's `total_cents` additionally carries tax, shipping and
promotions, and this module deliberately does **not** allocate those across
sellers. That allocation decides what each seller is paid and what commission
is charged on, so it is a finance decision rather than an arithmetic one, and
guessing at it here would bury the guess in a place nobody looks. What is
guaranteed is conservation: the seller subtotals sum to the order's goods
subtotal exactly, with no rounding drift, because they are integer minor units
summed rather than percentages applied.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


class SellerOrderStatus(str, Enum):
    """The COD lifecycle, per ARCHITECTURE_STATE_FINAL.md §3d."""

    PENDING = "PENDING"                    # created; reservation requested
    INVENTORY_RESERVED = "INVENTORY_RESERVED"
    CONFIRMED = "CONFIRMED"                # seller accepted; awaiting dispatch
    DISPATCHED = "DISPATCHED"              # handed to a courier
    RTO_IN_TRANSIT = "RTO_IN_TRANSIT"      # refused; coming back
    DELIVERED = "DELIVERED"                # buyer took it and paid the courier
    SETTLED = "SETTLED"                    # courier remitted; seller payable
    RETURNED = "RETURNED"                  # back with the seller; stock restored
    CANCELLED = "CANCELLED"                # ended before dispatch


# How far along each status is. Used only to pick the least advanced child, so
# what matters is the ordering, not the numbers.
#
# RTO_IN_TRANSIT sits above DISPATCHED and below DELIVERED on purpose: a parcel
# coming back has left the seller (so it is past DISPATCHED) and will never be
# delivered (so it must not outrank it). An order with one delivered line and
# one in RTO reads as RTO_IN_TRANSIT, which is true -- part of it is coming
# back.
_PROGRESS = {
    SellerOrderStatus.PENDING: 0,
    SellerOrderStatus.INVENTORY_RESERVED: 1,
    SellerOrderStatus.CONFIRMED: 2,
    SellerOrderStatus.DISPATCHED: 3,
    SellerOrderStatus.RTO_IN_TRANSIT: 4,
    SellerOrderStatus.DELIVERED: 5,
    SellerOrderStatus.SETTLED: 6,
}

# Ended, and not on the happy path. Excluded from "least advanced" so that one
# cancelled seller does not pin the whole order at CANCELLED forever while the
# other seller is still delivering.
ENDED_UNFAVOURABLY = {SellerOrderStatus.CANCELLED, SellerOrderStatus.RETURNED}

# The states from which nothing further happens.
TERMINAL = {SellerOrderStatus.SETTLED, SellerOrderStatus.RETURNED,
            SellerOrderStatus.CANCELLED}

EVENT_SELLER_ORDER_CREATED = "SellerOrderCreated"


class SplitError(ValueError):
    """The order cannot be split, and must not be partially split."""


@dataclass(frozen=True)
class SellerOrderPlan:
    seller_id: str
    lines: List[Dict[str, Any]]
    subtotal_cents: int

    @property
    def item_count(self) -> int:
        return sum(int(line["quantity"]) for line in self.lines)


def _as_status(value) -> Optional[SellerOrderStatus]:
    if isinstance(value, SellerOrderStatus):
        return value
    try:
        return SellerOrderStatus(value)
    except (ValueError, TypeError):
        return None


def normalise_items(items_payload: Any) -> List[Dict[str, Any]]:
    """Coerce the several shapes an items payload arrives in into a list.

    Same tolerance as dispatch_rules.plan_item_reservations, and deliberately
    so: the two read the same field from the same event, and a payload one
    accepts and the other rejects is an order that reserves stock and never
    splits, or splits and never reserves.
    """
    items = items_payload
    if isinstance(items, dict):
        items = items.get("items", items)
    if isinstance(items, dict):
        # {product_id: quantity} -- the oldest shape, with no price or seller.
        items = [{"product_id": k, "quantity": v} for k, v in items.items()]
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def split_by_seller(items_payload: Any) -> List[SellerOrderPlan]:
    """One plan per seller, or raise.

    Rejects the whole order rather than splitting the part that parses. A line
    with no seller is a line nobody can be paid for and nobody can be asked to
    ship; creating seller orders for the rest would leave that line in an order
    that can never complete, with stock reserved against it.

    Sorted by seller id, and lines sorted by product id within a seller, so the
    same cart always produces the same split. A retried checkout then writes
    the same rows in the same order, which is what makes the idempotency keys
    line up.
    """
    items = normalise_items(items_payload)
    if not items:
        raise SplitError("order has no items to split")

    by_seller: Dict[str, List[Dict[str, Any]]] = {}
    for line in items:
        seller_id = line.get("seller_id")
        if not seller_id:
            raise SplitError(
                f"line for product {line.get('product_id')!r} carries no "
                f"seller_id; a line nobody can be paid for cannot be shipped")

        product_id = line.get("product_id")
        if not product_id:
            raise SplitError("line carries no product_id")

        try:
            quantity = int(line["quantity"])
            line_total = int(line["line_total_cents"])
        except (KeyError, TypeError, ValueError):
            raise SplitError(
                f"line for product {product_id!r} has no usable quantity or "
                f"line_total_cents")

        if quantity <= 0:
            raise SplitError(
                f"line for product {product_id!r} has quantity {quantity}")
        if line_total < 0:
            raise SplitError(
                f"line for product {product_id!r} has a negative total")

        by_seller.setdefault(str(seller_id), []).append({
            "product_id": str(product_id),
            "quantity": quantity,
            "price_cents": int(line.get("price_cents", 0)),
            "line_total_cents": line_total,
        })

    plans = []
    for seller_id in sorted(by_seller):
        lines = sorted(by_seller[seller_id], key=lambda l: l["product_id"])
        plans.append(SellerOrderPlan(
            seller_id=seller_id,
            lines=lines,
            subtotal_cents=sum(l["line_total_cents"] for l in lines),
        ))
    return plans


def goods_subtotal_cents(plans: Sequence[SellerOrderPlan]) -> int:
    """What the goods across every seller order come to.

    Not the order total: the parent additionally carries tax, shipping and
    promotions, which this module does not allocate. Exposed so a caller can
    assert conservation rather than trusting it.
    """
    return sum(plan.subtotal_cents for plan in plans)


def derive_order_status(child_statuses: Sequence[Any]) -> Optional[str]:
    """The buyer-facing status, computed from the seller orders.

    Least advanced wins. An order is only as complete as its slowest part, so a
    buyer with one parcel delivered and one still out has an order in transit.

    Children that ended unfavourably are set aside, so one cancelled seller
    does not pin the order at CANCELLED while another seller is still
    delivering. When *every* child ended that way the order takes that ending:
    all cancelled is CANCELLED, anything else in that set is RETURNED, because
    "goods went out and came back" is the more informative of the two.

    Returns None for no children or for statuses this version does not
    recognise, so a caller can tell "cannot say" apart from any real status
    rather than being handed a plausible-looking wrong one.
    """
    statuses = [_as_status(s) for s in child_statuses]
    if not statuses or any(s is None for s in statuses):
        return None

    active = [s for s in statuses if s not in ENDED_UNFAVOURABLY]

    if not active:
        if all(s is SellerOrderStatus.CANCELLED for s in statuses):
            return SellerOrderStatus.CANCELLED.value
        return SellerOrderStatus.RETURNED.value

    return min(active, key=lambda s: _PROGRESS[s]).value


def is_order_complete(child_statuses: Sequence[Any]) -> bool:
    """Whether nothing further will happen to this order.

    Every child terminal. Distinct from "delivered": an order whose seller
    orders are all DELIVERED is finished for the buyer and unfinished for the
    platform, because the cash is still with a courier (§3d).
    """
    statuses = [_as_status(s) for s in child_statuses]
    if not statuses or any(s is None for s in statuses):
        return False
    return all(s in TERMINAL for s in statuses)


def build_seller_order_event(order_id: str, seller_order_id: str,
                             user_id: str, plan: SellerOrderPlan,
                             status: str) -> Dict[str, Any]:
    """The SellerOrderCreated payload.

    Carries the seller id and the lines, because the consumers that will act on
    this -- seller notifications, the seller dashboard, courier assignment --
    each need to know what they are shipping without asking order-saga for it.
    """
    return {
        "order_id": order_id,
        "seller_order_id": seller_order_id,
        "user_id": user_id,
        "seller_id": plan.seller_id,
        "status": status,
        "subtotal_cents": plan.subtotal_cents,
        "item_count": plan.item_count,
        "lines": plan.lines,
    }
