from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import TaxRuleCreate, TaxRuleResponse
from ..services.tax_service import create_tax_rule

router = APIRouter(prefix="/taxes", tags=["taxes"])

@router.post("/", response_model=TaxRuleResponse, status_code=201)
async def create_tax_rule_endpoint(
    rule_in: TaxRuleCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await create_tax_rule(db, rule_in, idempotency_key)
