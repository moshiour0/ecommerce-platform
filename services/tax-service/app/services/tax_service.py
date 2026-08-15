import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import TaxRule, OutboxMessage, IdempotencyKey
from ..schemas import TaxRuleCreate

async def create_tax_rule(db: AsyncSession, rule_in: TaxRuleCreate, idempotency_key: str) -> TaxRule:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    rule_id = uuid.uuid4()
    tax_rule = TaxRule(
        id=rule_id,
        country_code=rule_in.country_code,
        region_code=rule_in.region_code,
        tax_rate_basis_points=rule_in.tax_rate_basis_points,
        is_active=rule_in.is_active
    )
    db.add(tax_rule)

    # Create OutboxMessage within the same atomic transaction
    payload = {
        "id": str(tax_rule.id),
        "country_code": tax_rule.country_code,
        "region_code": tax_rule.region_code,
        "tax_rate_basis_points": tax_rule.tax_rate_basis_points,
        "is_active": tax_rule.is_active,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="TaxRule",
        aggregate_id=str(tax_rule.id),
        type="TaxRuleCreated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(tax_rule)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return tax_rule
