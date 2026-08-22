import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.behaviour_service import affinity_profile, forget, record

router = APIRouter(prefix="/personalisation", tags=["personalisation"])


class BehaviourRequest(BaseModel):
    kind: str
    product_id: Optional[uuid.UUID] = None
    # Supplied by the caller because it is what was on screen at the time. A
    # product can be recategorised or change hands, and what matters is what
    # the buyer was interested in then -- not what that product became later.
    category_id: Optional[uuid.UUID] = None
    seller_id: Optional[uuid.UUID] = None


def require_buyer(x_user_id: Optional[str] = Header(None)) -> str:
    """Who this is, from the gateway's verified token and nothing else.

    Personalisation is only ever done for an identified buyer. There is no
    anonymous profile keyed on a device or a session: an anonymous search gets
    no affinity and scores exactly as it did before this service existed, which
    is the correct behaviour and also the private one.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=401,
            detail="no buyer identity on this request; the gateway supplies "
                   "x-user-id from the verified token")
    return x_user_id


@router.post("/behaviour", status_code=202)
async def record_behaviour(request: BehaviourRequest,
                           buyer_id: str = Depends(require_buyer),
                           db: AsyncSession = Depends(get_db)):
    """Record one interaction.

    202 rather than 201: the caller is telling us something happened, not
    creating a resource they will refer to again. Nothing downstream needs the
    row's id, and returning one would invite somebody to depend on it.
    """
    return await record(db, buyer_id=buyer_id, kind=request.kind,
                        product_id=request.product_id,
                        category_id=request.category_id,
                        seller_id=request.seller_id)


@router.get("/affinity", status_code=200)
async def my_affinity(buyer_id: str = Depends(require_buyer),
                      db: AsyncSession = Depends(get_db)):
    """What the platform thinks this buyer is interested in.

    Readable by the buyer themselves, because a signal that decides what
    somebody is shown and is invisible to them is a signal nobody can argue
    with.
    """
    return await affinity_profile(db, buyer_id)


# The internal read, for search-service. Takes the buyer as a parameter rather
# than a header because the caller is a service acting on a buyer's behalf,
# not the buyer.
@router.get("/affinity/{buyer_id}", status_code=200)
async def affinity_for_buyer(buyer_id: uuid.UUID,
                             db: AsyncSession = Depends(get_db)):
    return await affinity_profile(db, buyer_id)


@router.delete("/behaviour", status_code=200)
async def forget_me(buyer_id: str = Depends(require_buyer),
                    db: AsyncSession = Depends(get_db)):
    """Delete everything recorded about this buyer.

    A service that records what people look at needs a way to stop having
    recorded it. Immediate and total, not a flag.
    """
    return await forget(db, buyer_id)
