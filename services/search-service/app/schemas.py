from pydantic import BaseModel, ConfigDict
from typing import List

class SearchResultItem(BaseModel):
    product_id: str
    name: str
    description: str
    price_cents: int
    quantity_available: int

    model_config = ConfigDict(from_attributes=True)

class SearchResponse(BaseModel):
    total_hits: int
    items: List[SearchResultItem]
