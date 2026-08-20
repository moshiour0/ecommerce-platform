"""
Seller onboarding decisions, as pure functions.

A marketplace's central safety question is not "who is this seller" but "may
this seller sell *yet*". Onboarding is the sequence that answers it, and every
step of it is a state somebody can be stuck in -- waiting on a reviewer,
rejected for a blurry photograph, suspended for late dispatch. Modelling that
as booleans (`is_verified`, `is_active`, `is_banned`) produces combinations
nobody designed: verified and banned, active but not verified. So it is one
explicit state, one transition table, and one whitelist.

The invariant
-------------
`may_list_products` is a whitelist of exactly one state plus one condition.
Every unknown status answers False. This mirrors media_rules.is_servable, and
for the same reason: the inverse design -- allowed to sell until something
objects -- means an un-reviewed seller can list during precisely the window
when an un-reviewed seller is most interesting to whoever registered it.

Why APPROVED and ACTIVE are different states
--------------------------------------------
APPROVED means the documents passed review. ACTIVE additionally means the
seller has accepted the current commission contract. Collapsing them would
make the contract acceptance invisible, and it is the thing that decides what
the platform is owed on every order. Keeping them apart also gives a cheap
answer to "terms changed": raise CURRENT_CONTRACT_VERSION and every seller
must accept again before listing, with no re-run of KYC.

Why REJECTED is not terminal but BANNED is
------------------------------------------
Most rejections are a photograph taken in bad light. Making that terminal
would mean a support ticket for every one of them. Rejection therefore allows
resubmission. A decision that the platform does not want this person at all is
a different decision, and BANNED is where it goes -- terminal, from any state,
and never reachable by a seller's own action.

What this module deliberately does not hold
-------------------------------------------
Payout bank details. A bank *statement* is a KYC document and lives in object
storage like the rest (media-service, purpose=seller_document, confidential).
The account number used to actually move money is a different concern with
different handling -- it belongs wherever payouts execute, encrypted at rest,
not in an onboarding record. Storing it here because onboarding happens to
collect it is how sensitive data ends up in the least protected place.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set, Tuple


class SellerStatus(str, Enum):
    REGISTERED = "registered"                    # account exists, no documents
    DOCUMENTS_SUBMITTED = "documents_submitted"  # KYC attached, queued
    UNDER_REVIEW = "under_review"                # a reviewer holds it
    APPROVED = "approved"                        # KYC cleared, contract pending
    ACTIVE = "active"                            # the only state that may sell
    REJECTED = "rejected"                        # KYC failed -- may resubmit
    SUSPENDED = "suspended"                      # was active, stopped
    BANNED = "banned"                            # terminal, never reversible


TERMINAL: Set[SellerStatus] = {SellerStatus.BANNED}


class DocumentType(str, Enum):
    NATIONAL_ID = "national_id"
    TRADE_LICENCE = "trade_licence"
    TAX_REGISTRATION = "tax_registration"
    BANK_STATEMENT = "bank_statement"


# What a seller must supply before a reviewer will look at them.
#
# NID and trade licence, because those are the two a Bangladeshi marketplace
# can actually verify against a public register. Tax registration (a TIN
# certificate) is collected but not required: it applies above a turnover
# threshold this service has no way to know at registration time, so demanding
# it up front would block small sellers who legitimately do not have one yet.
#
# Named rather than inlined so raising the bar is one edit and one test.
REQUIRED_DOCUMENTS: Set[DocumentType] = {
    DocumentType.NATIONAL_ID,
    DocumentType.TRADE_LICENCE,
}

# The commission contract sellers accept. Bump this when the terms change: the
# accepted version is recorded per seller, and a seller whose accepted version
# is behind this one must accept again before listing anything further.
CURRENT_CONTRACT_VERSION = 1

# Outbox event types. Names are a contract with every consumer, above all
# audit-service, and with catalog-service once it starts refusing listings
# from sellers who may not sell.
EVENT_REGISTERED = "SellerRegistered"
EVENT_DOCUMENTS_SUBMITTED = "SellerDocumentsSubmitted"
EVENT_REVIEW_STARTED = "SellerReviewStarted"
EVENT_APPROVED = "SellerApproved"
EVENT_REJECTED = "SellerRejected"
EVENT_ACTIVATED = "SellerActivated"
EVENT_SUSPENDED = "SellerSuspended"
EVENT_REINSTATED = "SellerReinstated"
EVENT_BANNED = "SellerBanned"


class Outcome(str, Enum):
    OK = "ok"
    INVALID = "invalid"          # the request itself is not acceptable
    NOT_ALLOWED = "not_allowed"  # a real request, but not from this state


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    new_status: Optional[SellerStatus]
    event: Optional[str]
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


def _ok(status: SellerStatus, event: str, detail: str = "") -> Decision:
    return Decision(Outcome.OK, status, event, detail)


def _not_allowed(detail: str) -> Decision:
    return Decision(Outcome.NOT_ALLOWED, None, None, detail)


def _invalid(detail: str) -> Decision:
    return Decision(Outcome.INVALID, None, None, detail)


def _as_status(value) -> Optional[SellerStatus]:
    """Coerce a stored string to a status, or None if it is not one.

    None rather than raising, because every caller here treats an
    unrecognised status as "not allowed" -- which is the safe answer for a row
    written by a migration, a fixture, or a future version of this service.
    """
    if isinstance(value, SellerStatus):
        return value
    try:
        return SellerStatus(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------

def may_list_products(status, accepted_contract_version: Optional[int] = None,
                      current_version: int = CURRENT_CONTRACT_VERSION) -> bool:
    """Whether this seller may put products on the platform.

    A whitelist of exactly one status, plus the contract check. Anything
    unrecognised answers False, so a status this version has never heard of
    cannot become permission by accident.

    The contract half matters as much as the status half: a seller who was
    activated under version 1 has not agreed to version 2's commission rates,
    and continuing to let them list would mean charging them terms they never
    accepted.
    """
    if _as_status(status) is not SellerStatus.ACTIVE:
        return False
    return accepted_contract_version == current_version


def may_receive_orders(status, accepted_contract_version: Optional[int] = None,
                       current_version: int = CURRENT_CONTRACT_VERSION) -> bool:
    """Whether new orders may be routed to this seller.

    The same answer as may_list_products today, and a separate function on
    purpose: they diverge the moment there is a wind-down policy. A seller
    leaving the platform stops receiving new orders well before their existing
    listings come down, and writing that as one function would make it a
    change to the listing rule.
    """
    return may_list_products(status, accepted_contract_version, current_version)


def needs_contract_acceptance(status, accepted_contract_version: Optional[int] = None,
                              current_version: int = CURRENT_CONTRACT_VERSION) -> bool:
    """Whether the seller is otherwise fine but owes an acceptance.

    Distinguished from "may not sell" so the seller dashboard can say *why*.
    "Accept the updated terms" and "your account is suspended" are different
    messages and the difference is the whole of a support ticket.
    """
    if _as_status(status) not in (SellerStatus.ACTIVE, SellerStatus.APPROVED):
        return False
    return accepted_contract_version != current_version


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

def missing_documents(submitted) -> Set[DocumentType]:
    """Which required documents are still absent.

    Unknown document types are ignored rather than rejected: a seller
    uploading something the platform does not ask for is harmless, and a hard
    failure here would make adding a new optional type a breaking change for
    every client mid-upload.
    """
    have = set()
    for value in submitted or []:
        try:
            have.add(DocumentType(value))
        except (ValueError, TypeError):
            continue
    return REQUIRED_DOCUMENTS - have


def plan_registration(legal_name: str, display_name: str,
                      contact_email: str) -> Decision:
    """A new seller account, born unable to sell."""
    if not legal_name or not legal_name.strip():
        return _invalid("legal_name is required")
    if not display_name or not display_name.strip():
        return _invalid("display_name is required")
    if not contact_email or "@" not in contact_email:
        return _invalid("contact_email must be an email address")

    return _ok(SellerStatus.REGISTERED, EVENT_REGISTERED,
               "registered; documents required before review")


def plan_document_submission(status, submitted) -> Decision:
    """Move to the review queue, but only with a complete set.

    Checked here rather than by the reviewer because an incomplete submission
    that reaches a human is a wasted review and a second round trip for the
    seller. The queue should only contain things that can actually be decided.
    """
    current = _as_status(status)
    if current is None:
        return _not_allowed(f"unrecognised seller status {status!r}")

    # REGISTERED is the first submission; REJECTED and DOCUMENTS_SUBMITTED are
    # both resubmissions -- the second because a seller may add a missing
    # document while still queued, and forcing them to wait for a rejection
    # first would be deliberate cruelty.
    if current not in (SellerStatus.REGISTERED, SellerStatus.REJECTED,
                       SellerStatus.DOCUMENTS_SUBMITTED):
        return _not_allowed(
            f"documents cannot be submitted from {current.value} "
            f"(a seller past review resubmits by appealing, not by uploading)")

    missing = missing_documents(submitted)
    if missing:
        names = ", ".join(sorted(d.value for d in missing))
        return _invalid(f"missing required document(s): {names}")

    return _ok(SellerStatus.DOCUMENTS_SUBMITTED, EVENT_DOCUMENTS_SUBMITTED,
               "queued for review")


def plan_review_start(status) -> Decision:
    """A reviewer picks the application up."""
    current = _as_status(status)
    if current is not SellerStatus.DOCUMENTS_SUBMITTED:
        return _not_allowed(
            f"review can only start from documents_submitted, not "
            f"{status!r}")
    return _ok(SellerStatus.UNDER_REVIEW, EVENT_REVIEW_STARTED,
               "under review")


def plan_review_result(status, approved: bool, reason: str = "") -> Decision:
    """The reviewer's verdict.

    Only from UNDER_REVIEW. Deciding straight from the queue would let a
    verdict be recorded against an application nobody opened, and the
    difference between "reviewed and approved" and "approved" is the entire
    audit trail.
    """
    current = _as_status(status)
    if current is not SellerStatus.UNDER_REVIEW:
        return _not_allowed(
            f"a verdict requires a review in progress; status is {status!r}")

    if approved:
        return _ok(SellerStatus.APPROVED, EVENT_APPROVED,
                   "approved; contract acceptance required before selling")

    if not reason or not reason.strip():
        # A rejection a seller cannot act on generates a support ticket that
        # costs more than asking the reviewer for one sentence.
        return _invalid("a rejection must carry a reason the seller can act on")
    return _ok(SellerStatus.REJECTED, EVENT_REJECTED, reason.strip())


def plan_contract_acceptance(status, version: int,
                             current_version: int = CURRENT_CONTRACT_VERSION) -> Decision:
    """The seller accepts the commission terms and goes live.

    Accepted from APPROVED (first time) and from ACTIVE (re-accepting after
    the terms changed). Accepting a version that is not the current one is
    refused rather than recorded: a client that posts a stale version number
    is a client showing the seller stale terms.
    """
    current = _as_status(status)
    if current not in (SellerStatus.APPROVED, SellerStatus.ACTIVE):
        return _not_allowed(
            f"contract cannot be accepted from {status!r}; it requires a "
            f"completed review")

    if version != current_version:
        return _invalid(
            f"contract version {version} is not the current version "
            f"{current_version}; the seller was shown terms that are no "
            f"longer in force")

    return _ok(SellerStatus.ACTIVE, EVENT_ACTIVATED,
               f"active on contract version {version}")


def plan_suspension(status, reason: str = "") -> Decision:
    """Stop an active seller selling, reversibly."""
    current = _as_status(status)
    if current is not SellerStatus.ACTIVE:
        return _not_allowed(f"only an active seller can be suspended, not {status!r}")
    if not reason or not reason.strip():
        return _invalid("a suspension must carry a reason")
    return _ok(SellerStatus.SUSPENDED, EVENT_SUSPENDED, reason.strip())


def plan_reinstatement(status) -> Decision:
    """Undo a suspension.

    Back to ACTIVE rather than to APPROVED: the seller already accepted the
    contract, and a suspension is not a reason to redo that. If the terms
    changed while they were suspended, may_list_products still refuses them
    until they re-accept -- which is the contract check doing its job rather
    than the state machine duplicating it.
    """
    current = _as_status(status)
    if current is not SellerStatus.SUSPENDED:
        return _not_allowed(f"only a suspended seller can be reinstated, not {status!r}")
    return _ok(SellerStatus.ACTIVE, EVENT_REINSTATED, "reinstated")


def plan_ban(status, reason: str = "") -> Decision:
    """Remove a seller permanently.

    Reachable from every state except itself, because the reasons for it --
    fraud, prohibited goods, a court order -- do not wait for an application
    to reach a convenient point in the workflow.
    """
    current = _as_status(status)
    if current is None:
        return _not_allowed(f"unrecognised seller status {status!r}")
    if current is SellerStatus.BANNED:
        return _not_allowed("seller is already banned")
    if not reason or not reason.strip():
        return _invalid("a ban must carry a reason")
    return _ok(SellerStatus.BANNED, EVENT_BANNED, reason.strip())


# ---------------------------------------------------------------------------
# the transition table, as data
# ---------------------------------------------------------------------------
# Kept separate from the plan_* functions and used only by tests and
# documentation. Two descriptions of the same machine would drift, so the test
# that reads this asserts it against the functions rather than trusting it.
TRANSITIONS: Dict[SellerStatus, Set[SellerStatus]] = {
    SellerStatus.REGISTERED: {SellerStatus.DOCUMENTS_SUBMITTED, SellerStatus.BANNED},
    SellerStatus.DOCUMENTS_SUBMITTED: {SellerStatus.DOCUMENTS_SUBMITTED,
                                       SellerStatus.UNDER_REVIEW,
                                       SellerStatus.BANNED},
    SellerStatus.UNDER_REVIEW: {SellerStatus.APPROVED, SellerStatus.REJECTED,
                                SellerStatus.BANNED},
    SellerStatus.APPROVED: {SellerStatus.ACTIVE, SellerStatus.BANNED},
    SellerStatus.ACTIVE: {SellerStatus.ACTIVE, SellerStatus.SUSPENDED,
                          SellerStatus.BANNED},
    SellerStatus.REJECTED: {SellerStatus.DOCUMENTS_SUBMITTED, SellerStatus.BANNED},
    SellerStatus.SUSPENDED: {SellerStatus.ACTIVE, SellerStatus.BANNED},
    SellerStatus.BANNED: set(),
}


def reachable_statuses(status) -> Set[SellerStatus]:
    current = _as_status(status)
    return set(TRANSITIONS.get(current, set()))
