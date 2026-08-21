"""
Unit tests for the cash-on-delivery lifecycle of a seller order.

Two properties carry this file.

**No compensation after dispatch.** A saga cannot undo a van. Once a parcel is
with a courier the only route back is a physical return, and a `cancel` that
succeeded there would mark an order cancelled while the goods were still
moving -- releasing stock that is not on the shelf and telling the buyer their
order was called off while a courier knocks on their door.

**Stock leaves exactly once, at delivery.** Reserve holds units; a dispatched
parcel is still returnable so it stays held; delivery is the point of no
return. Getting this wrong is visible in a warehouse: consuming at dispatch
loses everything that comes back, and releasing on delivery puts sold goods
back on sale. So every path through the machine is walked and its inventory
effects counted.
"""

import pytest

from conftest import cod_rules, split_rules

S = split_rules.SellerOrderStatus
InventoryEffect = cod_rules.InventoryEffect
Outcome = cod_rules.Outcome
plan_transition = cod_rules.plan_transition
may_cancel = cod_rules.may_cancel
is_terminal = cod_rules.is_terminal
actions_from = cod_rules.actions_from
ACTIONS = cod_rules.ACTIONS

# The happy path, as (action, from-state) pairs.
HAPPY_PATH = [
    ("confirm", S.INVENTORY_RESERVED, S.CONFIRMED),
    ("dispatch", S.CONFIRMED, S.DISPATCHED),
    ("deliver", S.DISPATCHED, S.DELIVERED),
    ("settle", S.DELIVERED, S.SETTLED),
]


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,frm,to", HAPPY_PATH)
def test_each_step_of_the_happy_path_is_allowed(action, frm, to):
    decision = plan_transition(action, frm, reason="ok")
    assert decision.ok, decision.detail
    assert decision.to is to
    assert decision.event


def test_the_happy_path_ends_settled_and_terminal():
    assert is_terminal(S.SETTLED)


def test_delivered_is_not_terminal():
    # The buyer is finished and the platform is not: the cash is with a
    # courier and the seller cannot be paid until it is remitted (§3d).
    assert not is_terminal(S.DELIVERED)


# ---------------------------------------------------------------------------
# after dispatch there is no compensation, only a return
# ---------------------------------------------------------------------------

def test_cancel_is_refused_once_dispatched():
    for status in (S.DISPATCHED, S.RTO_IN_TRANSIT, S.DELIVERED, S.SETTLED,
                   S.RETURNED, S.CANCELLED):
        decision = plan_transition("cancel", status, reason="buyer changed mind")
        assert not decision.ok, \
            f"a {status.value} seller order was cancelled; a saga cannot undo a van"


def test_cancel_is_allowed_everywhere_before_dispatch():
    for status in (S.PENDING, S.INVENTORY_RESERVED, S.CONFIRMED):
        assert plan_transition("cancel", status, reason="out of stock").ok
        assert may_cancel(status)


def test_may_cancel_answers_before_the_button_is_drawn():
    # The buyer-facing question. A 409 after the click is a worse answer than
    # not offering the button.
    assert may_cancel(S.CONFIRMED)
    assert not may_cancel(S.DISPATCHED)


def test_the_route_back_after_dispatch_is_a_return():
    rto = plan_transition("mark_rto", S.DISPATCHED, reason="refused at door")
    assert rto.ok and rto.to is S.RTO_IN_TRANSIT
    returned = plan_transition("complete_return", S.RTO_IN_TRANSIT)
    assert returned.ok and returned.to is S.RETURNED


def test_an_rto_must_say_why():
    # Refusal reasons are the input to refusal-risk scoring. An RTO with no
    # reason is a data point thrown away.
    assert not plan_transition("mark_rto", S.DISPATCHED).ok
    assert not plan_transition("mark_rto", S.DISPATCHED, reason="  ").ok


def test_a_cancellation_must_say_why():
    assert not plan_transition("cancel", S.CONFIRMED).ok


# ---------------------------------------------------------------------------
# stock leaves exactly once, at delivery
# ---------------------------------------------------------------------------

def test_only_delivery_consumes():
    consuming = [(action, frm) for action, table in ACTIONS.items()
                 for frm, t in table.items()
                 if t.effect is InventoryEffect.CONSUME]
    assert consuming == [("deliver", S.DISPATCHED)], \
        f"stock is consumed by {consuming}; it must leave only at delivery"


def test_dispatch_does_not_consume():
    # A dispatched parcel is still returnable. Consuming here loses every unit
    # that comes back.
    assert plan_transition("dispatch", S.CONFIRMED).effect is InventoryEffect.NONE


def test_delivery_does_not_release():
    # Releasing would put goods the buyer is holding back on sale.
    assert plan_transition("deliver", S.DISPATCHED).effect is not \
        InventoryEffect.RELEASE


def test_every_way_of_ending_without_delivery_puts_the_stock_back():
    # Cancelled before dispatch, or returned after: either way the goods are on
    # a shelf and must be sellable again.
    assert plan_transition("cancel", S.CONFIRMED, reason="x").effect is \
        InventoryEffect.RELEASE
    assert plan_transition("complete_return", S.RTO_IN_TRANSIT).effect is \
        InventoryEffect.RELEASE


def test_a_cancel_racing_a_reservation_is_still_safe():
    # PENDING is cancellable and releases, even though nothing may be held
    # yet. plan_release treats nothing-held as success, so the blind
    # compensation contract (Rule 5) holds here too.
    decision = plan_transition("cancel", S.PENDING, reason="race")
    assert decision.ok
    assert decision.effect is InventoryEffect.RELEASE


def test_no_path_both_consumes_and_releases_the_same_units():
    """Walk every route through the machine and count what it did to stock.

    A route that both consumed and released has sold the goods and put them
    back; a route that did neither and ended DELIVERED has left them held
    forever, which is the leak this lifecycle was written to close.
    """
    routes = []

    def walk(status, effects, depth=0):
        if depth > 8:
            return
        moves = actions_from(status)
        if not moves:
            routes.append((status, tuple(effects)))
            return
        for action in sorted(moves):
            decision = plan_transition(action, status, reason="r")
            if not decision.ok:
                continue
            walk(decision.to,
                 effects + ([decision.effect] if decision.effect is not
                            InventoryEffect.NONE else []),
                 depth + 1)

    walk(S.INVENTORY_RESERVED, [])
    assert routes, "no route through the machine at all"

    for end_state, effects in routes:
        assert not (InventoryEffect.CONSUME in effects and
                    InventoryEffect.RELEASE in effects), \
            f"a route ending {end_state.value} both consumed and released"
        if end_state is S.SETTLED:
            assert effects.count(InventoryEffect.CONSUME) == 1, \
                f"a settled route consumed {effects.count(InventoryEffect.CONSUME)} times"
        if end_state in (S.CANCELLED, S.RETURNED):
            assert effects.count(InventoryEffect.RELEASE) == 1, \
                f"a {end_state.value} route released " \
                f"{effects.count(InventoryEffect.RELEASE)} times"


def test_every_terminal_route_settled_the_stock_somehow():
    # No route may end terminal with the units still held: that is the leak
    # that existed before this module, where a delivered order's reservation
    # stayed 'held' forever.
    for status in (S.SETTLED, S.RETURNED, S.CANCELLED):
        assert is_terminal(status)
    assert not is_terminal(S.DISPATCHED)
    assert not is_terminal(S.RTO_IN_TRANSIT)


# ---------------------------------------------------------------------------
# refusals are usable
# ---------------------------------------------------------------------------

def test_an_unknown_action_is_invalid_not_not_allowed():
    # A bug in the client, which no retry fixes.
    decision = plan_transition("teleport", S.CONFIRMED)
    assert decision.outcome is Outcome.INVALID
    assert decision.http_status == 422


def test_a_wrong_state_is_not_allowed_and_names_what_would_work():
    decision = plan_transition("settle", S.CONFIRMED)
    assert decision.outcome is Outcome.NOT_ALLOWED
    assert decision.http_status == 409
    assert "DELIVERED" in decision.detail


def test_an_unrecognised_status_is_refused_rather_than_guessed():
    for status in (None, "", "SHIPPED", 1):
        assert not plan_transition("deliver", status).ok


def test_nothing_leaves_a_terminal_state():
    for status in (S.SETTLED, S.RETURNED, S.CANCELLED):
        assert actions_from(status) == set(), \
            f"{status.value} has an exit; it is supposed to be terminal"


def test_a_repeated_delivery_is_refused_rather_than_consuming_twice():
    # Courier callbacks are retried. The second one must not take another
    # three units out of the warehouse.
    assert not plan_transition("deliver", S.DELIVERED).ok
