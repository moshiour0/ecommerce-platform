from pydantic import BaseModel, ConfigDict
from typing import List, Optional

class SearchResultItem(BaseModel):
    product_id: str
    name: str
    description: Optional[str] = None
    # Optional because a product can genuinely have no price yet -- created
    # a moment ago, PriceUpdated not delivered, and no catalog base price.
    # This was an int defaulting to 0, which rendered such a product as
    # free rather than as unpriced.
    price_cents: Optional[int] = None
    # Which service the price came from: pricing, catalog-base, or
    # unpriced. Exposed so a caller does not have to guess whether a low
    # number is a discount or a fallback.
    price_source: str = "unpriced"
    quantity_available: int = 0

    model_config = ConfigDict(from_attributes=True)

class SearchResponse(BaseModel):
    total_hits: int
    items: List[SearchResultItem]
