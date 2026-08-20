"""
Unit tests for seller onboarding.

The property that matters is negative: there is no sequence of calls that makes
a seller able to list products without a completed review and an accepted
contract. Most of these tests exist to look for one -- by skipping the queue,
by deciding a review nobody started, by accepting a contract before approval,
by resubmitting documents after a ban.

The second theme is that the refusals have to be *usable*. A rejection with no
reason, or a suspension with no reason, is a support ticket rather than a
decision, so those are invalid rather than allowed.
"""

import pytest

from conftest import seller_rules

SellerStatus = seller_rules.SellerStatus
DocumentType = seller_rules.DocumentType
Outcome = seller_rules.Outcome

may_list_products = seller_rules.may_list_products
may_receive_orders = seller_rules.may_receive_orders
needs_contract_acceptance = seller_rules.needs_contract_acceptance
missing_documents = seller_rules.missing_documents
plan_registration = seller_rules.plan_registration
plan_document_submission = seller_rules.plan_document_submission
plan_review_start = seller_rules.plan_review_start
plan_review_result = seller_rules.plan_review_result
plan_contract_acceptance = seller_rules.plan_contract_acceptance
plan_suspension = seller_rules.plan_suspension
plan_reinstatement = seller_rules.plan_reinstatement
plan_ban = seller_rules.plan_ban
reachable_statuses = seller_rules.reachable_statuses

VERSION = seller_rules.CURRENT_CONTRACT_VERSION
COMPLETE_DOCS = ["national_id", "trade_licence"]


def drive_to_active():
    """The full happy path, as a helper. Returns the final decision."""
    assert plan_registration("Karim Traders Ltd", "Karim Traders",
                             "karim@example.com").ok
    assert plan_document_submission(SellerStatus.REGISTERED, COMPLETE_DOCS).ok
    assert plan_review_start(SellerStatus.DOCUMENTS_SUBMITTED).ok
    assert plan_review_result(SellerStatus.UNDER_REVIEW, approved=True).ok
    return plan_contract_acceptance(SellerStatus.APPROVED, VERSION)


# ---------------------------------------------------------------------------
# the invariant: who may sell
# ---------------------------------------------------------------------------

def test_only_an_active_seller_on_the_current_contract_may_list():
    assert may_list_products(SellerStatus.ACTIVE, VERSION)


def test_no_other_status_may_list_however_far_along():
    # Including APPROVED. Passing KYC is not permission to sell; accepting the
    # commission terms is.
    for status in SellerStatus:
        if status is SellerStatus.ACTIVE:
            continue
        assert not may_list_products(status, VERSION), \
            f"a {status.value} seller was allowed to list products"


def test_an_unknown_status_may_not_list():
    # A row written by a migration, a fixture, or a future version of this
    # service must not become permission by being unrecognised.
    for status in (None, "", "verified", "ACTIVE", 1, object()):
        assert not may_list_products(status, VERSION)


def test_an_active_seller_on_stale_terms_may_not_list():
    # They were activated under version 1 and the terms are now version 2.
    # Continuing to let them list means charging commission they never agreed
    # to.
    assert not may_list_products(SellerStatus.ACTIVE, VERSION,
                                 current_version=VERSION + 1)


def test_a_seller_who_never_accepted_anything_may_not_list():
    assert not may_list_products(SellerStatus.ACTIVE, None)


def test_order_routing_follows_the_same_rule_today():
    for status in SellerStatus:
        assert (may_receive_orders(status, VERSION)
                == may_list_products(status, VERSION))


def test_stale_terms_are_distinguishable_from_a_suspension():
    # The dashboard has to say which, because "accept the updated terms" and
    # "your account is suspended" are different messages.
    assert needs_contract_acceptance(SellerStatus.ACTIVE, VERSION,
                                     current_version=VERSION + 1)
    assert not needs_contract_acceptance(SellerStatus.ACTIVE, VERSION)
    assert not needs_contract_acceptance(SellerStatus.SUSPENDED, None)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_a_new_seller_is_born_unable_to_sell():
    decision = plan_registration("Karim Traders Ltd", "Karim Traders",
                                 "karim@example.com")
    assert decision.ok
    assert decision.new_status is SellerStatus.REGISTERED
    assert not may_list_products(decision.new_status, VERSION)


def test_registration_requires_a_legal_name_and_a_contact():
    assert not plan_registration("", "Karim Traders", "karim@example.com").ok
    assert not plan_registration("Karim Traders Ltd", "  ", "karim@example.com").ok
    assert not plan_registration("Karim Traders Ltd", "Karim Traders", "karim").ok


def test_registration_failures_are_invalid_not_not_allowed():
    # The distinction matters downstream: INVALID is the caller's request being
    # wrong (422) and is not worth retrying; NOT_ALLOWED is a legitimate
    # request against the wrong state (409).
    assert plan_registration("", "x", "a@b.c").outcome is Outcome.INVALID


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

def test_an_incomplete_submission_does_not_reach_the_queue():
    decision = plan_document_submission(SellerStatus.REGISTERED, ["national_id"])
    assert not decision.ok
    assert "trade_licence" in decision.detail


def test_a_complete_submission_is_queued():
    decision = plan_document_submission(SellerStatus.REGISTERED, COMPLETE_DOCS)
    assert decision.ok
    assert decision.new_status is SellerStatus.DOCUMENTS_SUBMITTED


def test_optional_documents_neither_help_nor_hurt():
    # tax_registration is collected but not required: it applies above a
    # turnover threshold this service cannot know at registration.
    assert plan_document_submission(SellerStatus.REGISTERED,
                                    COMPLETE_DOCS + ["tax_registration"]).ok
    assert not plan_document_submission(SellerStatus.REGISTERED,
                                        ["tax_registration"]).ok


def test_an_unrecognised_document_type_is_ignored_rather_than_fatal():
    # Adding a new optional type must not break clients mid-upload.
    assert missing_documents(COMPLETE_DOCS + ["selfie_with_cat"]) == set()


def test_a_rejected_seller_may_resubmit():
    # Most rejections are a photograph taken in bad light. Making that
    # terminal would mean a support ticket for each one.
    assert plan_document_submission(SellerStatus.REJECTED, COMPLETE_DOCS).ok


def test_a_queued_seller_may_add_a_document_without_waiting_to_be_rejected():
    assert plan_document_submission(SellerStatus.DOCUMENTS_SUBMITTED,
                                    COMPLETE_DOCS).ok


def test_an_active_seller_cannot_re_enter_onboarding_by_uploading():
    # Otherwise the route out of a suspension is to upload a document.
    for status in (SellerStatus.APPROVED, SellerStatus.ACTIVE,
                   SellerStatus.SUSPENDED, SellerStatus.UNDER_REVIEW):
        assert not plan_document_submission(status, COMPLETE_DOCS).ok


def test_a_banned_seller_cannot_resubmit_anything():
    assert not plan_document_submission(SellerStatus.BANNED, COMPLETE_DOCS).ok


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def test_review_starts_only_from_the_queue():
    assert plan_review_start(SellerStatus.DOCUMENTS_SUBMITTED).ok
    for status in SellerStatus:
        if status is SellerStatus.DOCUMENTS_SUBMITTED:
            continue
        assert not plan_review_start(status).ok


def test_a_verdict_requires_a_review_that_was_actually_started():
    # "reviewed and approved" and "approved" differ by the whole audit trail.
    assert not plan_review_result(SellerStatus.DOCUMENTS_SUBMITTED, approved=True).ok
    assert plan_review_result(SellerStatus.UNDER_REVIEW, approved=True).ok


def test_approval_does_not_by_itself_permit_selling():
    decision = plan_review_result(SellerStatus.UNDER_REVIEW, approved=True)
    assert decision.new_status is SellerStatus.APPROVED
    assert not may_list_products(decision.new_status, VERSION)


def test_a_rejection_must_say_why():
    assert not plan_review_result(SellerStatus.UNDER_REVIEW, approved=False).ok
    assert not plan_review_result(SellerStatus.UNDER_REVIEW, approved=False,
                                  reason="   ").ok
    decision = plan_review_result(SellerStatus.UNDER_REVIEW, approved=False,
                                  reason="trade licence is expired")
    assert decision.ok
    assert decision.detail == "trade licence is expired"


def test_a_seller_cannot_be_approved_twice_to_skip_the_contract():
    assert not plan_review_result(SellerStatus.APPROVED, approved=True).ok


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_accepting_the_contract_is_what_makes_a_seller_live():
    decision = drive_to_active()
    assert decision.ok
    assert decision.new_status is SellerStatus.ACTIVE
    assert may_list_products(decision.new_status, VERSION)


def test_the_contract_cannot_be_accepted_before_review_completes():
    for status in (SellerStatus.REGISTERED, SellerStatus.DOCUMENTS_SUBMITTED,
                   SellerStatus.UNDER_REVIEW, SellerStatus.REJECTED,
                   SellerStatus.SUSPENDED, SellerStatus.BANNED):
        assert not plan_contract_acceptance(status, VERSION).ok, \
            f"a {status.value} seller accepted the contract"


def test_accepting_a_stale_version_is_refused_rather_than_recorded():
    # A client posting an old version number is a client showing the seller
    # terms that are no longer in force.
    decision = plan_contract_acceptance(SellerStatus.APPROVED, VERSION,
                                        current_version=VERSION + 1)
    assert not decision.ok
    assert decision.outcome is Outcome.INVALID


def test_an_active_seller_can_re_accept_when_the_terms_change():
    # The route back for a seller whose contract went stale: re-accept, no
    # second KYC.
    decision = plan_contract_acceptance(SellerStatus.ACTIVE, VERSION + 1,
                                        current_version=VERSION + 1)
    assert decision.ok
    assert decision.new_status is SellerStatus.ACTIVE


# ---------------------------------------------------------------------------
# suspension, reinstatement, bans
# ---------------------------------------------------------------------------

def test_only_an_active_seller_can_be_suspended():
    assert plan_suspension(SellerStatus.ACTIVE, "late dispatch rate 40%").ok
    for status in SellerStatus:
        if status is SellerStatus.ACTIVE:
            continue
        assert not plan_suspension(status, "reason").ok


def test_a_suspension_must_say_why():
    assert not plan_suspension(SellerStatus.ACTIVE, "").ok


def test_a_suspended_seller_cannot_sell():
    decision = plan_suspension(SellerStatus.ACTIVE, "counterfeit report")
    assert not may_list_products(decision.new_status, VERSION)


def test_reinstatement_returns_to_active_without_a_second_review():
    decision = plan_reinstatement(SellerStatus.SUSPENDED)
    assert decision.ok
    assert decision.new_status is SellerStatus.ACTIVE
    assert may_list_products(decision.new_status, VERSION)


def test_only_a_suspended_seller_can_be_reinstated():
    for status in SellerStatus:
        if status is SellerStatus.SUSPENDED:
            continue
        assert not plan_reinstatement(status).ok


def test_reinstatement_still_defers_to_the_contract_check():
    # Terms changed while they were suspended: back to ACTIVE, still unable to
    # list until they re-accept. The contract check does that job, not the
    # state machine.
    decision = plan_reinstatement(SellerStatus.SUSPENDED)
    assert not may_list_products(decision.new_status, VERSION,
                                 current_version=VERSION + 1)


def test_a_ban_reaches_every_state_except_itself():
    for status in SellerStatus:
        decision = plan_ban(status, "prohibited goods")
        if status is SellerStatus.BANNED:
            assert not decision.ok
        else:
            assert decision.ok, f"a {status.value} seller could not be banned"


def test_a_ban_must_say_why():
    assert not plan_ban(SellerStatus.ACTIVE, "").ok


def test_a_ban_is_terminal():
    assert reachable_statuses(SellerStatus.BANNED) == set()
    for status in SellerStatus:
        if status is SellerStatus.BANNED:
            continue
        assert SellerStatus.BANNED not in reachable_statuses(SellerStatus.BANNED)


# ---------------------------------------------------------------------------
# the transition table must describe the same machine as the functions
# ---------------------------------------------------------------------------

def test_the_documented_table_matches_what_the_functions_actually_do():
    """Two descriptions of one machine drift; this asserts they have not.

    For every state, drive every transition function and collect the statuses
    that were actually reachable, then compare with TRANSITIONS. A entry added
    to the table without a code path, or a code path added without the table,
    fails here.
    """
    for status in SellerStatus:
        actual = set()
        for decision in (
            plan_document_submission(status, COMPLETE_DOCS),
            plan_review_start(status),
            plan_review_result(status, approved=True),
            plan_review_result(status, approved=False, reason="blurry"),
            plan_contract_acceptance(status, VERSION),
            plan_suspension(status, "reason"),
            plan_reinstatement(status),
            plan_ban(status, "reason"),
        ):
            if decision.ok:
                actual.add(decision.new_status)

        assert actual == reachable_statuses(status), (
            f"from {status.value}: functions reach "
            f"{sorted(s.value for s in actual)} but TRANSITIONS documents "
            f"{sorted(s.value for s in reachable_statuses(status))}")


def test_every_transition_carries_an_event():
    # Rule 3: a state change nothing can observe is a state change that did
    # not happen as far as the rest of the platform is concerned.
    for status in SellerStatus:
        for decision in (
            plan_document_submission(status, COMPLETE_DOCS),
            plan_review_start(status),
            plan_review_result(status, approved=True),
            plan_review_result(status, approved=False, reason="blurry"),
            plan_contract_acceptance(status, VERSION),
            plan_suspension(status, "reason"),
            plan_reinstatement(status),
            plan_ban(status, "reason"),
        ):
            if decision.ok:
                assert decision.event, \
                    f"transition to {decision.new_status} emits no event"


def test_no_path_reaches_active_without_passing_through_review():
    """Breadth-first search of the whole machine, looking for a shortcut.

    Every route to ACTIVE must contain UNDER_REVIEW. This is the invariant
    stated as a graph property rather than as a sequence of asserts, so a
    transition added later cannot open a bypass without failing here.
    """
    paths = [[SellerStatus.REGISTERED]]
    routes_to_active = []

    while paths:
        path = paths.pop()
        if len(path) > 8:            # the machine is small; this is a guard
            continue
        for nxt in reachable_statuses(path[-1]):
            if nxt in path:          # do not loop
                continue
            extended = path + [nxt]
            if nxt is SellerStatus.ACTIVE:
                routes_to_active.append(extended)
            else:
                paths.append(extended)

    assert routes_to_active, "no route to ACTIVE at all -- the machine is broken"
    for route in routes_to_active:
        assert SellerStatus.UNDER_REVIEW in route, (
            f"a seller reaches ACTIVE without review via "
            f"{' -> '.join(s.value for s in route)}")
