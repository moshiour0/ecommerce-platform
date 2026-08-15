from pydantic import BaseModel, Field
from uuid import UUID
from typing import List

class CartItem(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)

class CartAddRequest(BaseModel):
    item: CartItem

class CartResponse(BaseModel):
    user_id: UUID
    items: List[CartItem]
