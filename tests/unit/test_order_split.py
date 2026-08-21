"""
Unit tests for splitting one order into one order per seller.

Two properties carry the weight.

**Conservation.** The seller subtotals must sum to the goods subtotal exactly.
Money that goes missing in a split is money a seller is not paid, and it goes
missing quietly -- every individual seller order looks plausible on its own.

**The derived parent status.** It is computed from the children and never
stored, so these tests are the specification of what a buyer sees when their
two parcels are in different places. The rule is least-advanced-wins, and the
interesting cases are all the ones where a child has ended: a cancelled seller
must not pin the order at CANCELLED while another seller is still delivering,
and a delivered child must not make the order look finished while its sibling
is coming back.
"""

import pytest

from conftest import split_rules

SellerOrderStatus = split_rules.SellerOrderStatus
split_by_seller = split_rules.split_by_seller
goods_subtotal_cents = split_rules.goods_subtotal_cents
derive_order_status = split_rules.derive_order_status
is_order_complete = split_rules.is_order_complete
build_seller_order_event = split_rules.build_seller_order_event
normalise_items = split_rules.normalise_items
SplitError = split_rules.SplitError

S = SellerOrderStatus


def line(product, seller, quantity=1, price=100):
    return {"product_id": product, "seller_id": seller, "quantity": quantity,
            "price_cents": price, "line_total_cents": price * quantity}


TWO_SELLERS = [
    line("p2", "seller-b", quantity=1, price=500),
    line("p1", "seller-a", quantity=2, price=100),
    line("p3", "seller-a", quantity=1, price=300),
]


# ---------------------------------------------------------------------------
# the split
# ---------------------------------------------------------------------------

def test_one_seller_produces_one_order():
    plans = split_by_seller([line("p1", "seller-a")])
    assert len(plans) == 1
    assert plans[0].seller_id == "seller-a"


def test_lines_are_grouped_by_seller_not_by_product():
    plans = split_by_seller(TWO_SELLERS)
    assert [p.seller_id for p in plans] == ["seller-a", "seller-b"]
    assert len(plans[0].lines) == 2
    assert len(plans[1].lines) == 1


def test_money_is_conserved_across_the_split():
    # The assertion that matters. A split that loses a line loses a seller's
    # money, and every seller order still looks correct on its own.
    plans = split_by_seller(TWO_SELLERS)
    assert goods_subtotal_cents(plans) == sum(
        l["line_total_cents"] for l in TWO_SELLERS)


def test_each_subtotal_is_the_sum_of_that_sellers_lines():
    plans = split_by_seller(TWO_SELLERS)
    by_seller = {p.seller_id: p for p in plans}
    assert by_seller["seller-a"].subtotal_cents == 200 + 300
    assert by_seller["seller-b"].subtotal_cents == 500


def test_item_count_counts_units_not_lines():
    plans = split_by_seller(TWO_SELLERS)
    by_seller = {p.seller_id: p for p in plans}
    assert by_seller["seller-a"].item_count == 3     # 2 + 1
    assert by_seller["seller-b"].item_count == 1


def test_the_split_is_deterministic():
    # A retried checkout must write the same rows in the same order, or the
    # idempotency keys derived from them stop lining up.
    first = split_by_seller(TWO_SELLERS)
    shuffled = list(reversed(TWO_SELLERS))
    second = split_by_seller(shuffled)
    assert [(p.seller_id, p.lines) for p in first] == \
           [(p.seller_id, p.lines) for p in second]


def test_a_single_seller_with_many_lines_is_still_one_order():
    plans = split_by_seller([line(f"p{i}", "seller-a") for i in range(5)])
    assert len(plans) == 1
    assert len(plans[0].lines) == 5


# ---------------------------------------------------------------------------
# refusing to split what cannot be split
# ---------------------------------------------------------------------------

def test_a_line_with_no_seller_rejects_the_whole_order():
    # Not "split the rest". A line nobody can be paid for is a line nobody can
    # be asked to ship, and leaving it in an order whose other lines reserved
    # stock is worse than refusing the checkout.
    items = [line("p1", "seller-a"), {"product_id": "p2", "quantity": 1,
                                      "line_total_cents": 100}]
    with pytest.raises(SplitError, match="no seller_id"):
        split_by_seller(items)


def test_an_empty_order_is_refused():
    with pytest.raises(SplitError, match="no items"):
        split_by_seller([])


def test_a_line_with_no_product_is_refused():
    with pytest.raises(SplitError, match="no product_id"):
        split_by_seller([{"seller_id": "s", "quantity": 1,
                          "line_total_cents": 1}])


@pytest.mark.parametrize("quantity", [0, -1, "two", None])
def test_a_line_with_an_unusable_quantity_is_refused(quantity):
    items = [{"product_id": "p", "seller_id": "s", "quantity": quantity,
              "line_total_cents": 100}]
    with pytest.raises(SplitError):
        split_by_seller(items)


def test_a_line_with_no_total_is_refused():
    with pytest.raises(SplitError, match="line_total_cents"):
        split_by_seller([{"product_id": "p", "seller_id": "s", "quantity": 1}])


def test_a_negative_line_total_is_refused():
    items = [{"product_id": "p", "seller_id": "s", "quantity": 1,
              "line_total_cents": -5}]
    with pytest.raises(SplitError, match="negative"):
        split_by_seller(items)


def test_the_oldest_payload_shape_has_no_seller_and_is_refused():
    # {product_id: quantity}, which predates prices and sellers entirely.
    # Refused rather than defaulted to the platform seller: silently
    # attributing a stranger's goods to the platform is worse than a 4xx.
    with pytest.raises(SplitError, match="no seller_id"):
        split_by_seller({"p1": 2, "p2": 1})


def test_the_wrapped_list_shape_is_accepted():
    # cart-service once wrapped the list; dispatch_rules tolerates it and so
    # must this, or an order reserves stock and never splits.
    assert normalise_items({"items": [line("p1", "s")]}) == [line("p1", "s")]


# ---------------------------------------------------------------------------
# the derived parent status
# ---------------------------------------------------------------------------

def test_a_single_seller_order_gives_the_order_its_status():
    for status in S:
        assert derive_order_status([status]) == status.value


def test_least_advanced_wins():
    assert derive_order_status([S.DELIVERED, S.DISPATCHED]) == "DISPATCHED"
    assert derive_order_status([S.SETTLED, S.PENDING]) == "PENDING"
    assert derive_order_status([S.CONFIRMED, S.INVENTORY_RESERVED]) == \
        "INVENTORY_RESERVED"


def test_an_order_is_only_settled_when_every_seller_order_is():
    assert derive_order_status([S.SETTLED, S.SETTLED]) == "SETTLED"
    assert derive_order_status([S.SETTLED, S.DELIVERED]) == "DELIVERED"


def test_one_cancelled_seller_does_not_pin_the_whole_order():
    # The buyer cancelled one shop's part. The other shop is still shipping,
    # and the order must not read as cancelled.
    assert derive_order_status([S.CANCELLED, S.DISPATCHED]) == "DISPATCHED"
    assert derive_order_status([S.CANCELLED, S.SETTLED]) == "SETTLED"


def test_an_order_is_cancelled_only_when_all_of_it_is():
    assert derive_order_status([S.CANCELLED, S.CANCELLED]) == "CANCELLED"


def test_a_mix_of_cancelled_and_returned_reads_as_returned():
    # Goods went out and came back, which is the more informative of the two.
    assert derive_order_status([S.CANCELLED, S.RETURNED]) == "RETURNED"
    assert derive_order_status([S.RETURNED, S.RETURNED]) == "RETURNED"


def test_a_parcel_coming_back_outranks_dispatch_and_not_delivery():
    # One delivered, one refused: the order is not delivered, it is partly
    # coming back.
    assert derive_order_status([S.DELIVERED, S.RTO_IN_TRANSIT]) == \
        "RTO_IN_TRANSIT"
    # But a parcel still on its way out is less advanced than one coming back.
    assert derive_order_status([S.DISPATCHED, S.RTO_IN_TRANSIT]) == "DISPATCHED"


def test_no_children_cannot_be_answered():
    # Distinct from any real status: a caller must be able to tell "cannot
    # say" from "PENDING".
    assert derive_order_status([]) is None


def test_an_unrecognised_child_status_cannot_be_answered():
    # A row written by a migration or a later version must not be silently
    # ranked as the most advanced or the least.
    assert derive_order_status(["SHIPPED"]) is None
    assert derive_order_status([S.DELIVERED, "SHIPPED"]) is None
    assert derive_order_status([None]) is None


# ---------------------------------------------------------------------------
# completion is not delivery
# ---------------------------------------------------------------------------

def test_delivered_is_not_complete():
    # §3d: the buyer is finished and the platform is not -- the cash is still
    # with a courier, and until it is remitted the seller cannot be paid.
    assert not is_order_complete([S.DELIVERED, S.DELIVERED])


def test_settled_cancelled_and_returned_are_complete():
    assert is_order_complete([S.SETTLED])
    assert is_order_complete([S.CANCELLED])
    assert is_order_complete([S.RETURNED])
    assert is_order_complete([S.SETTLED, S.CANCELLED, S.RETURNED])


def test_one_unfinished_child_keeps_the_order_open():
    assert not is_order_complete([S.SETTLED, S.DISPATCHED])


def test_no_children_is_not_complete():
    assert not is_order_complete([])


# ---------------------------------------------------------------------------
# the event
# ---------------------------------------------------------------------------

def test_the_event_carries_what_a_consumer_needs_without_a_second_call():
    plan = split_by_seller(TWO_SELLERS)[0]
    event = build_seller_order_event("o1", "so1", "u1", plan, "PENDING")
    assert event["order_id"] == "o1"
    assert event["seller_order_id"] == "so1"
    assert event["seller_id"] == "seller-a"
    assert event["subtotal_cents"] == plan.subtotal_cents
    assert event["item_count"] == plan.item_count
    # The lines, so a seller notification or a courier assignment does not have
    # to ask order-saga what it is shipping.
    assert len(event["lines"]) == len(plan.lines)
