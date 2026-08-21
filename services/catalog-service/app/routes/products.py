from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import uuid
from typing import List

from ..database import get_db
from ..schemas import ProductCreate, ProductResponse
from ..services.catalog_service import create_product
from ..models import Product  # We need the model to query the database

router = APIRouter(prefix="/products", tags=["products"])

@router.post("/", response_model=ProductResponse, status_code=201)
async def create_product_endpoint(
    product_in: ProductCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    return await create_product(db, product_in, idempotency_key)

@router.get("/{product_id}", response_model=ProductResponse, status_code=200)
async def get_product_endpoint(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
        
    return product


# A seller's own catalogue. Filtered in the query rather than by the caller:
# a listing endpoint that returns everything and expects the caller to filter
# is one refactor away from leaking every seller's products to each of them.
@router.get("/", response_model=List[ProductResponse], status_code=200)
async def list_products_endpoint(
    seller_id: uuid.UUID,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Product)
        .where(Product.seller_id == seller_id)
        .order_by(Product.created_at.desc())
        .limit(min(limit, 200)))
    return result.scalars().all()
