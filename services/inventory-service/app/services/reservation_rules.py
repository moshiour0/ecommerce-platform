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
