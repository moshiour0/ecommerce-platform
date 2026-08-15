from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Optional

class TaxRuleBase(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    region_code: Optional[str] = None
    tax_rate_basis_points: int = Field(..., ge=0)
    is_active: bool = True

class TaxRuleCreate(TaxRuleBase):
    pass

class TaxRuleResponse(TaxRuleBase):
    id: UUID
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
