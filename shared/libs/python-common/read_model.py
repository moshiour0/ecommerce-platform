"""
Field ownership for the shared product read model, and the only supported way
to write it.

Why this exists
---------------
The `products` document in Elasticsearch is assembled from three services, and
this repository has now written the same bug three separate times:

  * tests/e2e/es_healer.py PUT whole documents, stripping sku, updated_at and
    every deactivation on every e2e run;
  * reindex-worker was one line away from writing whole documents built from
    catalog rows, which would have deleted every price and stock level;
  * stream-processor's ProductCreated handler actually did it -- es.index()
    replaces, so a redelivered creation event erased price and stock, and Kafka
    is at-least-once so redelivery is routine.

Each was written by someone who knew the document had several owners. Knowing
was not enough, because the dangerous call (`index`, `PUT`) is shorter to type
than the safe one and looks like what you want. So this module removes the
choice: it exposes one write function, it is always a partial update, and it
refuses fields the calling service does not own.

Ownership
---------
Exactly one service writes each field. The one exception is `updated_at`, which
every writer touches and which therefore means "when anything last changed this
document" -- not "when this owner last changed it". That ambiguity is precisely
why `catalog_updated_at` exists as a separate, single-owner field: reindex-worker
needs to know when *it* last wrote, and comparing against a mark left by pricing
made products with any price history permanently unrepairable.
"""

from typing import Any, Dict, FrozenSet, Iterable, Mapping

PRODUCTS_INDEX = "products"

CATALOG = "catalog-service"
PRICING = "pricing-service"
INVENTORY = "inventory-service"

# The seller's own signals, denormalised onto every product they sell.
#
# A fourth writer rather than folding these into CATALOG, because they do not
# come from catalog-service and they do not change when a product changes. They
# are a *seller* fact copied onto product documents so Elasticsearch can score
# on them in one query -- which is the whole reason this exists (§3g: the
# ranking was fetching them per seller, per results page, over HTTP).
#
# Giving them their own owner is what stops a catalog update from silently
# blanking a rating: a ProductCreated redelivery writes CATALOG's fields, and
# anything it does not name is left alone.
SELLER_SIGNALS = "seller-projection"

# Written by every owner, and so owned by none. Kept deliberately small: each
# addition here is a field nobody can reason about.
ANY_OWNER = "*"

# Every writer allowed to touch a product document. Derived nowhere else, so
# adding an owner without adding it here fails loudly instead of writing
# fields nobody owns.
KNOWN_WRITERS = frozenset({CATALOG, PRICING, INVENTORY, SELLER_SIGNALS})

PRODUCT_FIELD_OWNERS: Mapping[str, str] = {
    "product_id": CATALOG,
    # Whose product this is. Catalog-owned: a seller cannot be reassigned
    # by a price change or a stock movement.
    "seller_id": CATALOG,
    "sku": CATALOG,
    # What kind of thing this is. Catalog-owned, and indexed so personalisation
    # can ask whether a buyer leans towards this category without a lookup per
    # result (ARCHITECTURE 3j).
    "category_id": CATALOG,
    "name": CATALOG,
    "description": CATALOG,
    "is_active": CATALOG,
    # catalog's list price. Distinct from price_cents on purpose -- one field
    # with two claimants is what made the price ambiguous for the life of the
    # project.
    "base_price_cents": CATALOG,
    # Single-owner timestamp, so a backfill can tell its own writes apart from
    # everyone else's.
    "catalog_updated_at": CATALOG,

    # The effective price a customer pays (§3: pricing owns pricing).
    "price_cents": PRICING,

    "quantity_available": INVENTORY,

    # --- seller signals, denormalised (§3g) --------------------------------
    # Every one of these is a fact about the seller, copied here so a search
    # scores in one query instead of N HTTP lookups. They are refreshed as a
    # set: a partial write would leave a rating from today beside a return rate
    # from last week, and nothing would look wrong.
    # Whether the seller may still be sold from. Unlike the rest of this
    # block it does not tune a score -- it decides whether the product is
    # findable, which is why search filters on it rather than weighting it.
    "seller_may_sell": SELLER_SIGNALS,
    "seller_rating": SELLER_SIGNALS,
    "seller_review_count": SELLER_SIGNALS,
    "seller_on_time_dispatch_rate": SELLER_SIGNALS,
    "seller_cancellation_rate": SELLER_SIGNALS,
    "seller_return_rate": SELLER_SIGNALS,
    # How much fulfilment history backs the rates above. Carried because
    # ranking needs it to decide how far to move a score, and a rate without
    # its confidence is a number that looks more certain than it is.
    "seller_confidence": SELLER_SIGNALS,
    # geo_point. Nullable, and absent means unlocated rather than far away --
    # distance_decay returns exactly 1.0 for None, so a seller who never set
    # coordinates is not penalised for it.
    "seller_location": SELLER_SIGNALS,
    # This writer's own mark, for the same reason catalog has one: a refresh
    # must be able to tell its own writes from everyone else's.
    "seller_signals_updated_at": SELLER_SIGNALS,

    "updated_at": ANY_OWNER,
}


class OwnershipError(ValueError):
    """A service tried to write a field it does not own, or one nobody owns."""


def fields_owned_by(owner: str) -> FrozenSet[str]:
    """Every field `owner` may write, including the shared ones."""
    return frozenset(
        field for field, holder in PRODUCT_FIELD_OWNERS.items()
        if holder == owner or holder == ANY_OWNER
    )


def foreign_fields(owner: str) -> FrozenSet[str]:
    """Fields that belong to somebody else. The ones a bug would erase."""
    return frozenset(
        field for field, holder in PRODUCT_FIELD_OWNERS.items()
        if holder != owner and holder != ANY_OWNER
    )


def validate_product_write(owner: str, fields: Iterable[str]) -> None:
    """Raise unless `owner` may write every one of `fields`.

    Unknown fields are refused as well as foreign ones. Elasticsearch maps new
    fields dynamically, so a typo does not fail -- it silently creates
    `quantity_avaliable` alongside the real column and the document looks fine
    until someone searches on it.
    """
    if owner not in KNOWN_WRITERS:
        raise OwnershipError(
            f"unknown writer {owner!r}; expected one of "
            f"{sorted(KNOWN_WRITERS)}")

    allowed = fields_owned_by(owner)
    offending = [f for f in fields if f not in allowed]
    if not offending:
        return

    unknown = [f for f in offending if f not in PRODUCT_FIELD_OWNERS]
    stolen = [f for f in offending if f in PRODUCT_FIELD_OWNERS]

    parts = []
    if stolen:
        parts.append(
            "fields owned by another service: "
            + ", ".join(f"{f} ({PRODUCT_FIELD_OWNERS[f]})" for f in sorted(stolen)))
    if unknown:
        parts.append("fields nobody owns: " + ", ".join(sorted(unknown)))

    raise OwnershipError(
        f"{owner} may not write " + "; ".join(parts)
        + ". Writing another service's field into this document overwrites "
          "live data that this service cannot reconstruct.")


def product_update_body(owner: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """The Elasticsearch body for a partial product write.

    Always `doc_as_upsert`, never a whole document. The upsert half matters for
    ordering: a price arriving before its ProductCreated should keep the price
    rather than be dropped, and it becomes visible once catalog catches up --
    search requires the catalog marker, so a partial document is never served
    as a product.
    """
    validate_product_write(owner, fields.keys())
    return {"doc": dict(fields), "doc_as_upsert": True}


def write_product(es, product_id: str, owner: str, fields: Dict[str, Any],
                  index: str = PRODUCTS_INDEX):
    """The only supported way to write a product document.

    `es` is an Elasticsearch client. Deliberately no counterpart that replaces a
    document: if one existed, it would eventually be used, which is the entire
    history of this file.
    """
    body = product_update_body(owner, fields)
    # product_id is the document id, so it costs nothing to keep it in the
    # source too. Upserts from pricing and inventory used to omit it, leaving
    # documents whose whole content was a stock level and a timestamp.
    body["doc"].setdefault("product_id", product_id)
    return es.update(index=index, id=product_id, body=body)


def quality_from_document(source: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn an indexed product document into ranking's quality inputs.

    Lives here, beside the field names it reads, because it is the other end of
    the same contract: the projection writes `seller_rating`, ranking wants
    `rating`, and a rename on one side has to be a rename on the other. Two
    copies of this translation in two services is how a field quietly stops
    being read.

    A document with no seller signals yields all-None, which `quality_boost`
    scores as exactly 1.0 -- the same as the HTTP lookup this replaced did on
    failure. A product indexed before the projection existed is therefore
    neutral rather than broken, and needs no backfill to stay searchable.
    """
    return {
        "rating": source.get("seller_rating"),
        "review_count": source.get("seller_review_count"),
        "on_time_dispatch_rate": source.get("seller_on_time_dispatch_rate"),
        "cancellation_rate": source.get("seller_cancellation_rate"),
        "return_rate": source.get("seller_return_rate"),
        "confidence": source.get("seller_confidence"),
    }


def has_seller_signals(source: Mapping[str, Any]) -> bool:
    """Whether the projection has written to this document yet."""
    return source.get("seller_signals_updated_at") is not None
