"""
Unit tests for the saga dispatcher's decision rules.

Includes contract tests between the dispatcher and order-saga: the two are
separate services with separate deployments, and every serious bug in this
platform so far has been a disagreement between two components that each
looked correct alone.

Run:  python -m pytest tests/unit -q
"""

import pytest

from conftest import dispatch_rules, saga_transitions

ROUTES = dispatch_rules.ROUTES
SETTLES = dispatch_rules.SETTLES
SagaAck = dispatch_rules.SagaAck
classify_saga_response = dispatch_rules.classify_saga_response
commands_handled = dispatch_rules.commands_handled
events_emitted = dispatch_rules.events_emitted
route_for = dispatch_rules.route_for
settles = dispatch_rules.settles


# ---------------------------------------------------------------------------
# status classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_any_2xx_is_applied(status):
    assert classify_saga_response(status) is SagaAck.APPLIED


def test_409_is_deferred_not_an_error():
    """409 means 'valid, just not yet' -- the command must be retried."""
    assert classify_saga_response(409) is SagaAck.DEFERRED


def test_422_is_dead_lettered():
    """422 means the saga will never accept this event."""
    assert classify_saga_response(422) is SagaAck.DEAD_LETTERED


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503, 504])
def test_other_failures_are_errors(status):
    assert classify_saga_response(status) is SagaAck.ERROR


def test_409_and_422_are_not_treated_alike():
    """Collapsing these is the bug that made a retryable event unretryable."""
    assert classify_saga_response(409) is not classify_saga_response(422)
    assert settles(SagaAck.DEFERRED) != settles(SagaAck.DEAD_LETTERED)


# ---------------------------------------------------------------------------
# settlement -- getting these backwards loses work or spins forever
# ---------------------------------------------------------------------------

def test_deferred_never_settles():
    """Settling a deferred command drops the delivery permanently."""
    assert settles(SagaAck.DEFERRED) is False


def test_error_never_settles():
    """A 500 is transient; the command must remain claimable."""
    assert settles(SagaAck.ERROR) is False


def test_dead_letter_always_settles():
    """Not settling a 422 spins against an event the saga will never accept."""
    assert settles(SagaAck.DEAD_LETTERED) is True


def test_applied_settles():
    assert settles(SagaAck.APPLIED) is True


def test_every_ack_has_a_settlement_rule():
    for ack in SagaAck:
        assert ack in SETTLES, f"{ack} has no settlement rule"


# ---------------------------------------------------------------------------
# the money path
# ---------------------------------------------------------------------------

def test_failed_refund_is_never_settled():
    """An outstanding refund must not be abandoned.

    Every other command reports its failure to the saga and settles. A refund
    does not: if the payment provider is briefly down, the command stays
    claimable and retries. Settling here would leave a customer charged with
    no further attempt to return the money.
    """
    r = route_for("RefundPaymentCommand")
    assert r.settle_on_failure is False
    assert r.on_failure is None, "a failed refund must not be reported as a saga outcome"


def test_refund_success_reports_to_the_saga():
    r = route_for("RefundPaymentCommand")
    assert r.on_success == "PaymentRefunded"
    assert r.success_status == 200


def test_charge_and_refund_use_different_success_codes():
    """201 for a created charge, 200 for a refund. Confusing them silently
    reports every successful refund as a failure."""
    assert route_for("ChargePaymentCommand").success_status == 201
    assert route_for("RefundPaymentCommand").success_status == 200


def test_only_refund_declines_to_settle_on_failure():
    """Any other command that stopped settling would spin against a real
    failure, so this exemption must stay deliberate and narrow."""
    non_settling = {c for c, r in ROUTES.items() if not r.settle_on_failure}
    assert non_settling == {"RefundPaymentCommand"}


# ---------------------------------------------------------------------------
# business outcomes vs transport failures
# ---------------------------------------------------------------------------

def test_inventory_failure_is_a_saga_outcome_not_an_error():
    """Out of stock must reach the saga as an event.

    When inventory raised an exception and emitted nothing, every
    oversubscribed order hung at PENDING for the full reaper window.
    """
    r = route_for("ReserveInventoryCommand")
    assert r.on_failure == "InventoryReservationFailed"
    assert r.settle_on_failure is True


def test_payment_decline_is_a_saga_outcome():
    assert route_for("ChargePaymentCommand").on_failure == "PaymentFailed"


def test_unknown_command_has_no_route():
    """route_for returning None tells the caller to settle rather than spin."""
    assert route_for("SomeCommandNobodyDefined") is None


def test_saga_timeout_settles_without_emitting_anything():
    r = route_for("SagaTimedOut")
    assert r.on_success is None and r.on_failure is None
    assert r.target is None, "SagaTimedOut needs no downstream call"


def test_compensation_acks_need_no_downstream_call():
    for cmd in ("ReleaseInventoryCommand", "CompensateInventoryCommand", "ConfirmOrderCommand"):
        assert route_for(cmd).target is None, f"{cmd} should not call a service"


def test_both_inventory_release_spellings_map_to_one_event():
    """The reaper and the saga historically emitted different names."""
    assert route_for("ReleaseInventoryCommand").on_success == "InventoryReleased"
    assert route_for("CompensateInventoryCommand").on_success == "InventoryReleased"


# ---------------------------------------------------------------------------
# CONTRACT: dispatcher <-> order-saga
# ---------------------------------------------------------------------------

def test_every_event_the_dispatcher_sends_is_known_to_the_saga():
    """An unknown event is answered 422 and dead-lettered -- silently losing
    the step. This is a cross-service contract, invisible to either alone."""
    KNOWN_EVENT_TYPES = saga_transitions.KNOWN_EVENT_TYPES

    unknown = events_emitted() - set(KNOWN_EVENT_TYPES)
    assert not unknown, f"dispatcher emits events the saga rejects: {sorted(unknown)}"


def test_every_command_the_saga_emits_has_a_dispatcher_route():
    """A command with no route is settled and silently dropped, stranding the
    saga until the reaper sweeps it."""
    TRANSITIONS = saga_transitions.TRANSITIONS

    saga_commands = {cmd for (_s, _e), (_d, cmd) in TRANSITIONS.items() if cmd}
    # OrderFailed is a notification event, not a command the dispatcher runs.
    saga_commands.discard("OrderFailed")

    missing = saga_commands - commands_handled()
    assert not missing, f"saga emits commands the dispatcher cannot handle: {sorted(missing)}"


def test_reaper_emitted_commands_are_all_routable():
    """The reaper emits these directly via raw SQL, bypassing the saga's own
    transition table, so nothing else checks that they are handled."""
    for cmd in ("ReleaseInventoryCommand", "RefundPaymentCommand", "SagaTimedOut"):
        assert route_for(cmd) is not None, f"reaper emits {cmd} with no dispatcher route"


def test_routes_reference_only_real_downstream_targets():
    """Target names double as circuit-breaker keys; a typo means a KeyError at
    the exact moment a downstream is already failing."""
    known = {"inventory-service", "payment-service", "order-saga"}
    for cmd, r in ROUTES.items():
        if r.target is not None:
            assert r.target in known, f"{cmd} routes to unknown target {r.target}"


def test_a_command_that_calls_a_service_declares_a_success_status():
    for cmd, r in ROUTES.items():
        if r.target is not None:
            assert r.success_status is not None, f"{cmd} calls {r.target} with no success status"
            assert r.on_success is not None, f"{cmd} calls {r.target} but reports nothing"


# ---------------------------------------------------------------------------
# drift guard: the rules table vs the code that still hand-rolls payloads
# ---------------------------------------------------------------------------

def test_dispatcher_source_emits_no_event_outside_the_table():
    """handle() builds per-command payloads by hand, so the event names live in
    two places. A name that drifts out of the table is answered 422 by the saga
    and dead-lettered -- the step vanishes with no error anywhere.

    This reads the source rather than the table so the two cannot silently
    disagree; a table nobody consults is documentation, not a rule.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "workers" / "saga-dispatcher" / "app" / "main.py").read_text(encoding="utf-8")

    # every literal passed to _advance_saga as the event_type argument
    emitted = set(re.findall(r'_advance_saga\(\s*order_id,\s*"([A-Za-z]+)"', src))
    assert emitted, "no _advance_saga calls found — the guard would pass vacuously"

    known = set(saga_transitions.KNOWN_EVENT_TYPES)
    unknown = emitted - known
    assert not unknown, f"main.py emits events the saga rejects: {sorted(unknown)}"

    untabled = emitted - events_emitted()
    assert not untabled, f"main.py emits events missing from ROUTES: {sorted(untabled)}"


def test_dispatcher_source_handles_every_command_in_the_table():
    """The reverse direction: a routed command that handle() ignores is
    settled and dropped."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "workers" / "saga-dispatcher" / "app" / "main.py").read_text(encoding="utf-8")

    missing = [c for c in commands_handled() if f'"{c}"' not in src]
    assert not missing, f"ROUTES declares commands main.py never checks: {sorted(missing)}"
