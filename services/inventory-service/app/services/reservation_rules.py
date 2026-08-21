"""
Inventory reservation decision, as pure functions.

Extracted from reserve_inventory so the arithmetic can be tested without a
database. What stays in the service is the pessimistic row lock and the
transaction; what lives here is the decision and the resulting quantities.

Stock is the one quantity in this platform that must never go negative, and
the failure is asymmetric: overselling five units in a flash sale means five
orders that cannot be fulfilled and five refunds, while under-selling merely
disappoints. So the rules fail closed.

Two things are deliberately explicit:

  * total stock is conserved. Reserving moves units from available to
    reserved; it never creates or destroys them. A test can assert the sum is
    invariant, which catches a whole class of arithmetic slip.
  * insufficient stock is an outcome, not an exception. When this raised
    instead of returning, inventory emitted nothing and every oversubscribed
    saga sat at PENDING until the reaper swept it fifteen minutes later.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ReservationOutcome(str, Enum):
    RESERVED = "reserved"          # stock held
    INSUFFICIENT = "insufficient"  # a business outcome, not an error
    NOT_FOUND = "not_found"        # no inventory record for this product
    INVALID = "invalid"            # non-positive quantity requested


# Events this service emits. Both must be known to order-saga, or the step is
# answered 422 and dead-lettered.
EVENT_RESERVED = "InventoryReserved"
EVENT_FAILED = "InventoryReservationFailed"


@dataclass(frozen=True)
class StockLevel:
    """The mutable part of an inventory row."""

    quantity_available: int
    quantity_reserved: int

    @property
    def total(self) -> int:
        return self.quantity_available + self.quantity_reserved


@dataclass(frozen=True)
class ReservationPlan:
    outcome: ReservationOutcome
    new_level: Optional[StockLevel]  # None when nothing changes
    event: Optional[str]             # outbox event to emit, if any
    detail: str

    @property
    def reserved(self) -> bool:
        return self.outcome is ReservationOutcome.RESERVED


def plan_reservation(level: Optional[StockLevel], requested: int) -> ReservationPlan:
    """Decide whether a reservation can be honoured, and what it leaves behind.

    `level` is None when no inventory row exists for the product. That is a
    404 rather than a zero-stock outcome: auto-creating stock for an unknown
    product is exactly the backdoor that made a flash sale unsellable-out, and
    it is not this function's job to invent inventory.
    """
    if requested <= 0:
        return ReservationPlan(
            ReservationOutcome.INVALID, None, None,
            f"quantity must be positive, got {requested}")

    if level is None:
        return ReservationPlan(
            ReservationOutcome.NOT_FOUND, None, None,
            "no inventory record for this product")

    if level.quantity_available < requested:
        # Emit, do not raise. Silence here is what stranded oversubscribed
        # orders at PENDING for the full reaper window.
        return ReservationPlan(
            ReservationOutcome.INSUFFICIENT, None, EVENT_FAILED,
            f"insufficient stock: {level.quantity_available} available, "
            f"{requested} requested")

    # Units move between columns; the total is conserved.
    return ReservationPlan(
        ReservationOutcome.RESERVED,
        StockLevel(
            quantity_available=level.quantity_available - requested,
            quantity_reserved=level.quantity_reserved + requested,
        ),
        EVENT_RESERVED,
        f"reserved {requested}",
    )


# ---------------------------------------------------------------------------
# release -- the inverse of a reservation (§5)
# ---------------------------------------------------------------------------

class ReleaseOutcome(str, Enum):
    RELEASED = "released"      # units moved back to available
    NOTHING_HELD = "nothing"   # no live reservation: a no-op, and a success
    INVALID = "invalid"        # non-positive quantity recorded


@dataclass(frozen=True)
class ReleasePlan:
    outcome: ReleaseOutcome
    new_level: Optional[StockLevel]
    event: Optional[str]
    detail: str

    @property
    def released(self) -> bool:
        return self.outcome is ReleaseOutcome.RELEASED


EVENT_RELEASED = "InventoryReleased"


def plan_release(level: Optional[StockLevel], held: int) -> ReleasePlan:
    """Return `held` units from reserved to available.

    `held` is what the reservation ledger says this order is holding, not what
    a caller asked to release. A compensation must not be able to invent stock
    by asking for more than was taken.

    Nothing held is success, not failure. §5 requires a compensating command to
    succeed as a no-op when the forward step never took effect, because after a
    timeout at INVENTORY_RESERVED it is unknowable whether the reservation
    landed, and both legs are compensated unconditionally. A release that
    errored on "no reservation" would strand every saga that timed out before
    reserving.
    """
    if held == 0:
        return ReleasePlan(ReleaseOutcome.NOTHING_HELD, None, EVENT_RELEASED,
                           "no live reservation; nothing to return")

    if held < 0:
        return ReleasePlan(ReleaseOutcome.INVALID, None, None,
                           f"held quantity must not be negative, got {held}")

    if level is None:
        # The ledger says units are held against a product row that no longer
        # exists. Nothing can be credited back to a row that is gone, and
        # inventing one is exactly the backdoor plan_reservation refuses.
        return ReleasePlan(ReleaseOutcome.NOTHING_HELD, None, EVENT_RELEASED,
                           "no inventory record for this product")

    # Clamp rather than trust. If the ledger claims more than the row has
    # reserved, something has already drifted; returning the excess would
    # create stock that was never taken, and an oversell is worse than an
    # under-release. The discrepancy is reported rather than hidden.
    releasable = min(held, level.quantity_reserved)
    detail = f"released {releasable}"
    if releasable < held:
        detail += (f" (ledger claimed {held} but only "
                   f"{level.quantity_reserved} was reserved)")

    return ReleasePlan(
        ReleaseOutcome.RELEASED,
        StockLevel(
            quantity_available=level.quantity_available + releasable,
            quantity_reserved=level.quantity_reserved - releasable,
        ),
        EVENT_RELEASED,
        detail,
    )


# PostgreSQL SQLSTATE 55P03: lock_not_available. Raised when a
# SELECT ... FOR UPDATE waits longer than lock_timeout (Rule 11 sets 3s).
SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"


def is_lock_contention(exc: BaseException) -> bool:
    """True when a database error is lock contention rather than a fault.

    Under flash-sale load every buyer contends for one row, so waiters past
    lock_timeout are ordinary and expected. Letting them fall into a generic
    handler returns HTTP 500, which is wrong in three ways: it tells the
    dispatcher to retry and adds load to an already-contended row, it is
    indistinguishable from a crash on a dashboard, and it hides the fact that
    the item is simply busy.

    Detection prefers the SQLSTATE, which is stable, and falls back to the
    driver's exception name for wrappers that do not surface it.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate == SQLSTATE_LOCK_NOT_AVAILABLE:
            return True
        if type(current).__name__ == "LockNotAvailableError":
            return True
        current = getattr(current, "orig", None) or current.__cause__
    return False


# ---------------------------------------------------------------------------
# consuming: the units actually left
# ---------------------------------------------------------------------------
# Until now nothing ever moved units *out* of reserved. Reserve took them from
# available and held them; release put them back. A delivered order left its
# units held forever, so `quantity_reserved` grew without bound and the number
# stopped meaning "committed to open orders" -- 41 units across the platform
# were held by orders that had long since completed.
#
# Under COD there is an obvious moment for this and it is not checkout: the
# goods leave when the buyer takes them at the door and pays the courier. A
# parcel that is dispatched is still returnable, so it stays reserved.
# Delivery is the point of no return, and delivery is what consumes.

class ConsumeOutcome(str, Enum):
    CONSUMED = "consumed"      # units left the building
    NOTHING_HELD = "nothing"   # no live reservation: a no-op, and a success
    INVALID = "invalid"        # non-positive quantity recorded


@dataclass(frozen=True)
class ConsumePlan:
    outcome: ConsumeOutcome
    new_level: Optional[StockLevel]
    event: Optional[str]
    detail: str

    @property
    def consumed(self) -> bool:
        return self.outcome is ConsumeOutcome.CONSUMED


EVENT_CONSUMED = "InventoryConsumed"


def plan_consume(level: Optional[StockLevel], held: int) -> ConsumePlan:
    """Retire `held` units from reserved without returning them to available.

    The one asymmetry with plan_release, and the whole point of this function:
    a release conserves the total because the goods are still on the shelf, and
    a consume does not because they are in a customer's hands. A consume
    implemented as a release would put delivered goods back on sale.

    Like release, nothing held is a success. Delivery confirmations arrive from
    couriers and are retried, so the second one must be a no-op rather than an
    error that makes a courier integration look broken.
    """
    if held == 0:
        return ConsumePlan(ConsumeOutcome.NOTHING_HELD, None, EVENT_CONSUMED,
                           "no live reservation; nothing to consume")

    if held < 0:
        return ConsumePlan(ConsumeOutcome.INVALID, None, None,
                           f"held quantity must not be negative, got {held}")

    if level is None:
        return ConsumePlan(ConsumeOutcome.INVALID, None, None,
                           "no inventory record for this product")

    if level.quantity_reserved < held:
        # The ledger says this order holds more than the item says is reserved.
        # Consuming anyway would drive quantity_reserved negative and hide the
        # discrepancy; refusing surfaces it while the numbers still add up.
        return ConsumePlan(
            ConsumeOutcome.INVALID, None, None,
            f"ledger says {held} held but the item shows only "
            f"{level.quantity_reserved} reserved")

    return ConsumePlan(
        ConsumeOutcome.CONSUMED,
        StockLevel(
            quantity_available=level.quantity_available,
            quantity_reserved=level.quantity_reserved - held,
        ),
        EVENT_CONSUMED,
        f"consumed {held}",
    )
