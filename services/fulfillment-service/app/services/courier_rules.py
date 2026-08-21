"""
One internal contract for many couriers, as pure functions.

Bangladesh has no single carrier. Pathao, Steadfast, RedX, Sundarban and a
dozen smaller operators each have their own API, their own status words, and
their own settlement file. §3d says they belong behind one contract here and
never as per-provider branches in the saga -- because the alternative is that
`if courier == "pathao"` appears in the order state machine, then in the
notification templates, then in the payout run, and the tenth courier becomes a
month of work instead of a config file.

So this module owns three things:

  1. **A canonical status vocabulary.** What the platform means, independent of
     what any courier says.
  2. **The mapping in.** Each provider's words translated to canonical ones,
     from data rather than from code.
  3. **What a status means for the order.** Which canonical statuses drive a
     seller order, and which are merely informative.

An unknown status is never guessed
----------------------------------
`map_status` returns None for a word it has not been taught, and None is not
"ignore" -- the caller must park it for a human. Both alternatives are worse:
defaulting to IN_TRANSIT hides a delivery, and defaulting to DELIVERED consumes
stock on a word nobody has read. Couriers add statuses without telling anyone,
and the first sign is usually a parcel stuck in a state the platform does not
recognise.

Where the provider vocabularies come from
-----------------------------------------
`config/couriers/*.json`, not this file. The status strings a courier emits are
*their* documentation, not something to be inferred: a mapping written from
memory is a guess that looks like configuration. The shipped files carry the
canonical side in full and the provider side as whatever has actually been read
from that provider's docs, and `validate_mapping` refuses one that leaves the
platform unable to see a delivery or a return.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set


class CourierStatus(str, Enum):
    """What the platform means by a parcel's state.

    Deliberately smaller than any individual courier's vocabulary. A courier
    that distinguishes "at sorting hub" from "in line haul" is describing its
    own operation; the platform only needs to know whether the parcel is
    moving, arrived, failed, or coming back.
    """

    PICKUP_PENDING = "PICKUP_PENDING"        # assigned, not yet collected
    PICKED_UP = "PICKED_UP"                  # the seller has handed it over
    IN_TRANSIT = "IN_TRANSIT"                # moving; nothing to decide
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"    # with a rider; nothing to decide
    DELIVERED = "DELIVERED"                  # buyer took it and paid
    DELIVERY_FAILED = "DELIVERY_FAILED"      # an attempt failed; will retry
    RETURNING = "RETURNING"                  # given up; coming back
    RETURNED = "RETURNED"                    # back with the seller
    CANCELLED = "CANCELLED"                  # called off before pickup


# Which canonical statuses move a seller order, and to which action
# (cod_rules.ACTIONS). Everything absent from this map is informational: it is
# recorded on the shipment and changes nothing about the order.
#
# DELIVERY_FAILED is deliberately absent. A failed attempt is not a return --
# couriers retry two or three times before giving up, and treating the first
# failure as an RTO would send stock back to a seller for a buyer who was
# simply out. RETURNING is the courier saying it has given up.
STATUS_ACTIONS: Dict[CourierStatus, str] = {
    CourierStatus.PICKED_UP: "dispatch",
    CourierStatus.DELIVERED: "deliver",
    CourierStatus.RETURNING: "mark_rto",
    CourierStatus.RETURNED: "complete_return",
    CourierStatus.CANCELLED: "cancel",
}

# A mapping that cannot express these leaves the platform blind to the events
# that move money and stock. A courier whose file omits DELIVERED would have
# every parcel sit dispatched forever while the seller waits to be paid.
REQUIRED_CANONICAL: Set[CourierStatus] = {
    CourierStatus.PICKED_UP,
    CourierStatus.DELIVERED,
    CourierStatus.RETURNING,
    CourierStatus.RETURNED,
}


class Outcome(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MappingProblem:
    provider: str
    detail: str


def normalise(raw: Optional[str]) -> str:
    """Courier status words arrive spelled inconsistently.

    The same provider sends `Delivered`, `delivered` and `DELIVERED` from
    different endpoints, and `delivery_success` / `delivery-success` from
    different API versions. Comparing raw strings makes the mapping a list of
    every spelling somebody happened to observe.
    """
    if raw is None:
        return ""
    return "".join(
        ch for ch in str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    )


def build_index(provider_mapping: Dict[str, List[str]]) -> Dict[str, CourierStatus]:
    """Invert {canonical: [provider words]} into {normalised word: canonical}.

    Inverted rather than searched on every callback: a courier pushing status
    updates for every parcel it carries is the highest-volume caller this
    service has.

    A word claimed by two canonical statuses raises. Silently letting the last
    one win would make the meaning of a status depend on dictionary ordering,
    and the symptom would be parcels occasionally taking the wrong branch.
    """
    index: Dict[str, CourierStatus] = {}
    for canonical_name, words in (provider_mapping or {}).items():
        try:
            canonical = CourierStatus(canonical_name)
        except ValueError:
            raise ValueError(
                f"{canonical_name!r} is not a canonical courier status; the "
                f"platform vocabulary is fixed and providers map onto it")
        for word in words or []:
            key = normalise(word)
            if not key:
                continue
            if key in index and index[key] is not canonical:
                raise ValueError(
                    f"provider word {word!r} is mapped to both "
                    f"{index[key].value} and {canonical.value}")
            index[key] = canonical
    return index


def map_status(index: Dict[str, CourierStatus],
               raw_status: Optional[str]) -> Optional[CourierStatus]:
    """The canonical status for a provider's word, or None.

    None means "this provider said something we have not been taught", and the
    caller must park it for a human rather than ignore it. A courier adding a
    status without telling anyone is normal, and the first symptom is a parcel
    stuck in a state nobody recognises -- which is a support ticket, where
    guessing is a consumed unit of stock or an unpaid seller.
    """
    return (index or {}).get(normalise(raw_status))


def action_for(status: Optional[CourierStatus]) -> Optional[str]:
    """Which seller-order action this status drives, if any."""
    if status is None:
        return None
    return STATUS_ACTIONS.get(status)


def validate_mapping(provider: str,
                     provider_mapping: Dict[str, List[str]]) -> List[MappingProblem]:
    """Everything wrong with one provider's configuration.

    A list rather than a raise, so a bring-up can report every broken courier
    file at once instead of one per restart.
    """
    problems: List[MappingProblem] = []

    try:
        index = build_index(provider_mapping)
    except ValueError as e:
        return [MappingProblem(provider, str(e))]

    covered = set(index.values())
    missing = REQUIRED_CANONICAL - covered
    if missing:
        problems.append(MappingProblem(
            provider,
            f"maps nothing to {', '.join(sorted(s.value for s in missing))}; "
            f"the platform would never see those events from this courier"))

    if not index:
        problems.append(MappingProblem(provider, "maps no statuses at all"))

    return problems


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------
# Under COD the courier collects the cash and remits it later, in a batch, as a
# file. That file is the only evidence the platform has that it was paid, and
# reconciling it is what turns a delivery into money a seller can be given.
#
# §3d: a mismatch is an operational alert, never a silent adjustment. Writing
# down whatever the courier says would make the courier the authority on what
# it owes.

class ReconcileOutcome(str, Enum):
    MATCHED = "matched"                # remitted exactly what was expected
    SHORT = "short"                    # remitted less
    OVER = "over"                      # remitted more
    UNKNOWN_ORDER = "unknown_order"    # a reference the platform does not have
    NOT_DELIVERED = "not_delivered"    # cash for an order that never arrived
    DUPLICATE = "duplicate"            # this row was already reconciled
    INVALID = "invalid"                # unusable row


@dataclass(frozen=True)
class Reconciliation:
    outcome: ReconcileOutcome
    seller_order_id: Optional[str]
    expected_cents: int
    remitted_cents: int
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is ReconcileOutcome.MATCHED

    @property
    def needs_attention(self) -> bool:
        """Anything a person has to look at.

        Deliberately not `not ok`: a duplicate row is benign and expected --
        couriers resend files -- while a short payment is somebody's money
        missing.
        """
        return self.outcome in {
            ReconcileOutcome.SHORT, ReconcileOutcome.OVER,
            ReconcileOutcome.UNKNOWN_ORDER, ReconcileOutcome.NOT_DELIVERED,
            ReconcileOutcome.INVALID,
        }

    @property
    def variance_cents(self) -> int:
        return self.remitted_cents - self.expected_cents


def reconcile_row(row: dict, known: Optional[dict],
                  already_settled: bool = False) -> Reconciliation:
    """Match one settlement row against what the platform expected.

    `known` is the seller order the row's reference points at, or None when
    there is no such order. `already_settled` says this row has been applied
    before -- couriers resend whole files after a correction, so a repeat is
    normal and must be a no-op rather than paying a seller twice.

    Nothing here adjusts anything. It classifies, and a caller decides; the
    only outcome that leads to money moving is MATCHED.
    """
    reference = str(row.get("reference") or "").strip()
    if not reference:
        return Reconciliation(ReconcileOutcome.INVALID, None, 0, 0,
                              "settlement row carries no order reference")

    try:
        remitted = int(row["collected_cents"])
    except (KeyError, TypeError, ValueError):
        return Reconciliation(ReconcileOutcome.INVALID, reference, 0, 0,
                              f"row {reference} has no usable collected amount")

    if remitted < 0:
        return Reconciliation(ReconcileOutcome.INVALID, reference, 0, remitted,
                              f"row {reference} remits a negative amount")

    if known is None:
        # Not an error on the courier's part necessarily -- it may be another
        # merchant's reference in a shared file -- but the platform must never
        # book money against an order it cannot find.
        return Reconciliation(ReconcileOutcome.UNKNOWN_ORDER, reference,
                              0, remitted,
                              f"no seller order matches reference {reference}")

    expected = int(known.get("expected_cents") or 0)

    if already_settled:
        return Reconciliation(ReconcileOutcome.DUPLICATE, reference,
                              expected, remitted,
                              f"{reference} was already settled; ignoring")

    if known.get("status") != "DELIVERED":
        # Cash for an order the platform does not believe arrived. Either the
        # courier's delivery callback was lost or the money is misattributed,
        # and both need a person.
        return Reconciliation(ReconcileOutcome.NOT_DELIVERED, reference,
                              expected, remitted,
                              f"{reference} is {known.get('status')}, not "
                              f"DELIVERED; cash arrived for an order the "
                              f"platform does not believe was delivered")

    if remitted == expected:
        return Reconciliation(ReconcileOutcome.MATCHED, reference,
                              expected, remitted, "matched")

    outcome = (ReconcileOutcome.SHORT if remitted < expected
               else ReconcileOutcome.OVER)
    return Reconciliation(outcome, reference, expected, remitted,
                          f"{reference} expected {expected}, remitted "
                          f"{remitted} ({remitted - expected:+d})")


def summarise_settlement(results: List[Reconciliation]) -> dict:
    """The one-line answer an operator needs from a settlement run."""
    counts: Dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1

    attention = [r for r in results if r.needs_attention]
    return {
        "rows": len(results),
        "matched": counts.get(ReconcileOutcome.MATCHED.value, 0),
        "needs_attention": len(attention),
        "outcomes": counts,
        # Signed, and summed over the rows that are actually wrong. A file that
        # is short on one order and over on another is not "balanced" -- both
        # are errors and netting them hides two problems.
        "variance_cents": sum(r.variance_cents for r in attention),
    }
