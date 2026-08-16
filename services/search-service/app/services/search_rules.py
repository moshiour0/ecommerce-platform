"""
Search result decisions, as pure functions.

The one that matters is price, because two services have a claim on it and the
read model has to resolve them at read time rather than by letting them
overwrite each other in the index.

  catalog-service   base_price_cents -- the list price, set when the product
                    was created, and the only price that exists until pricing
                    has published anything
  pricing-service   price_cents -- the effective price, which is what pricing
                    owns per §3 and what a customer should be charged

Each field has exactly one writer. Before this split they were the same field
with two claimants: catalog stored a price_cents that reached nothing, and the
document's price_cents was written only by pricing. A product with no pricing
row therefore had no price in the index at all, and search substituted 0 --
displaying it as free rather than at the price catalog was told.

Precedence is pricing first, catalog second, and never a silent zero. A missing
price is reported as missing so a caller can decide what to do, rather than
being flattened into a number that happens to be a legal price.
"""

from typing import Any, Dict, Optional, Tuple

PRICING_FIELD = "price_cents"
CATALOG_FIELD = "base_price_cents"


def effective_price(source: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """The price to show, and where it came from.

    Returns (None, "unpriced") when neither service has supplied one. That is a
    real state -- a product created seconds ago whose PriceUpdated has not
    arrived yet -- and it is deliberately not 0. Zero is a price a customer
    could be charged, and the difference between "free" and "we do not know
    yet" is the difference between a bug report and an order.
    """
    priced = source.get(PRICING_FIELD)
    if isinstance(priced, bool):
        # A JSON true would otherwise pass the int check below and become 1.
        priced = None
    if isinstance(priced, int) and priced >= 0:
        return priced, "pricing"

    base = source.get(CATALOG_FIELD)
    if isinstance(base, bool):
        base = None
    if isinstance(base, int) and base >= 0:
        return base, "catalog-base"

    return None, "unpriced"
