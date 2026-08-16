"""
Unit tests for CQRS event identification.

The bug these exist for: every failed inventory reservation was indexed as a
product, because the type inference ended in a fallback that assumed anything
unrecognised was a ProductCreated. The payloads below are copied from real
outbox rows.
"""

from conftest import event_rules

infer_event_type = event_rules.infer_event_type
is_product_payload = event_rules.is_product_payload
INDEXED_EVENTS = event_rules.INDEXED_EVENTS

# Verbatim from inventory_db.outbox_messages.
FAILED_RESERVATION = {
    "id": "833f7e22-a8de-4caa-b9bd-a0e54071f039",
    "reason": "InsufficientStock",
    "_span_id": "82b40d88dcbd91d0",
    "_trace_id": "67b849ad276363d42da9a85eb5182078",
    "product_id": "73347139-58c7-4f26-a200-084a222141af",
    "occurred_at": "2026-08-16T11:43:35.047838+00:00",
    "quantity_available": 0,
    "quantity_requested": 1,
}


# ---------------------------------------------------------------------------
# the bug
# ---------------------------------------------------------------------------

def test_a_failed_reservation_is_not_a_product():
    # It was. quantity_available and quantity_requested match neither the
    # inventory signature (total_quantity_available, quantity_reserved) nor the
    # product one, so it fell through to the fallback and was indexed as a
    # product whose every catalog field was null.
    assert infer_event_type({}, FAILED_RESERVATION) == "InventoryReservationFailed"


def test_a_failed_reservation_is_not_indexed_at_all():
    assert infer_event_type({}, FAILED_RESERVATION) not in INDEXED_EVENTS


def test_an_unrecognised_payload_returns_none_rather_than_guessing():
    # None means unknown. It must never mean "probably a product": indexing on
    # a guess writes real data into the read model on an assumption.
    assert infer_event_type({}, {"something": "unfamiliar", "id": "x"}) is None


def test_an_empty_payload_is_unknown():
    assert infer_event_type({}, {}) is None


def test_a_non_dict_payload_is_unknown():
    for payload in ("a string", None, 42, []):
        assert infer_event_type({}, payload) is None


# ---------------------------------------------------------------------------
# the shapes that are recognised
# ---------------------------------------------------------------------------

def test_a_price_event_is_recognised():
    assert infer_event_type({}, {"product_id": "p", "base_price_cents": 1500}) \
        == "PriceUpdated"


def test_a_successful_reservation_is_recognised():
    assert infer_event_type({}, {"product_id": "p", "total_quantity_available": 5}) \
        == "InventoryReserved"
    assert infer_event_type({}, {"product_id": "p", "quantity_reserved": 2}) \
        == "InventoryReserved"


def test_a_product_event_is_recognised():
    assert infer_event_type({}, {"id": "p", "name": "Keyboard",
                                 "description": "clicky"}) == "ProductCreated"


def test_failure_is_checked_before_the_reserved_shape():
    # A payload carrying both a failure reason and a quantity must not be read
    # as a successful reservation.
    payload = dict(FAILED_RESERVATION, quantity_reserved=0)
    assert infer_event_type({}, payload) == "InventoryReservationFailed"


def test_an_explicit_type_always_wins():
    # Shape sniffing is a fallback for payloads with no type, not a second
    # opinion about ones that have it.
    assert infer_event_type({"type": "SomethingElse"}, FAILED_RESERVATION) \
        == "SomethingElse"
    assert infer_event_type({"event_type": "AlsoFine"}, {}) == "AlsoFine"


def test_only_three_event_types_are_indexed():
    assert INDEXED_EVENTS == {"ProductCreated", "PriceUpdated", "InventoryReserved"}


# ---------------------------------------------------------------------------
# defence in depth
# ---------------------------------------------------------------------------

def test_a_payload_with_no_name_or_sku_is_not_a_product():
    # Whatever it was labelled. This is what stops a null-filled document being
    # written even if the type inference is fooled again.
    assert is_product_payload(FAILED_RESERVATION) is False
    assert is_product_payload({"id": "p"}) is False
    assert is_product_payload({"name": None, "sku": None}) is False
    assert is_product_payload({"name": "", "sku": ""}) is False


def test_either_a_name_or_a_sku_is_enough():
    assert is_product_payload({"name": "Keyboard"}) is True
    assert is_product_payload({"sku": "SKU-1"}) is True


def test_a_non_dict_is_not_a_product():
    for payload in (None, "x", 3, []):
        assert is_product_payload(payload) is False


# ---------------------------------------------------------------------------
# the type header, which is why the shape checks are now a fallback
# ---------------------------------------------------------------------------

HEADER = event_rules.TYPE_HEADER


def test_the_header_names_the_event():
    # Debezium carries the outbox `type` column here. It cannot go in the
    # message envelope: that is part of the Avro value schema and the registry
    # runs FULL_TRANSITIVE, which rejected the change and failed the connector.
    labelled = infer_event_type({}, FAILED_RESERVATION,
                                {HEADER: "InventoryReservationFailed"})
    assert labelled == "InventoryReservationFailed"


def test_the_header_beats_the_shape():
    # A payload that looks like a product but is labelled otherwise is what it
    # says it is. Shape sniffing was only ever a workaround for the missing
    # label.
    product_shaped = {"id": "p", "name": "Keyboard", "description": "clicky"}
    assert infer_event_type(
        {}, product_shaped, {HEADER: "SomethingElse"}) == "SomethingElse"


def test_the_header_beats_an_explicit_field():
    assert infer_event_type(
        {"type": "FromBody"}, {}, {HEADER: "FromHeader"}) == "FromHeader"


def test_an_absent_header_falls_back_to_the_shape():
    # Events published before the connectors carried the header still have to
    # be identified.
    assert infer_event_type(
        {}, FAILED_RESERVATION, {}) == "InventoryReservationFailed"
    assert infer_event_type(
        {}, FAILED_RESERVATION, None) == "InventoryReservationFailed"


def test_an_empty_header_value_falls_back():
    assert infer_event_type(
        {}, {"base_price_cents": 1}, {HEADER: ""}) == "PriceUpdated"


def test_unrelated_headers_are_ignored():
    assert infer_event_type({}, {}, {"id": "x", "timestamp": "y"}) is None
