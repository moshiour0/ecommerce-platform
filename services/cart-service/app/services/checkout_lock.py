"""
Checkout mutual-exclusion primitive (Architecture: cart-service boundary).

A distributed lock is only as good as its release. This module exists because
the release was wrong: the original `finally` block issued an unconditional
DELETE, so a request whose Postgres commit outlived the 10-second TTL would
delete a lock a *different* request had since acquired -- dropping mutual
exclusion at precisely the moment two checkouts were in flight for one cart.

The fix is a per-request token plus a compare-and-delete, and the check and
the delete must be atomic, hence Lua rather than GET-then-DEL.

Nothing here talks to Postgres, so the protocol can be tested against a Redis
model that simulates TTL expiry and a competing holder -- the exact interleave
that the bug required and that no happy-path test reaches.
"""

import uuid
from typing import Optional

# Default lifetime. Must exceed the longest plausible checkout, because expiry
# mid-checkout is what lets a second request in. It is a safety net for a
# crashed holder, not a scheduling parameter.
DEFAULT_TTL_SECONDS = 10

# Release only if we still hold the lock. Returns 1 when deleted, 0 when the
# lock is absent or owned by someone else. An unconditional DEL here is the
# original bug; keep the comparison.
RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


def lock_key_for(user_id: str) -> str:
    """One lock per cart owner. Checkout is serialized per user, not globally."""
    return f"lock:checkout:{user_id}"


def new_token() -> str:
    """A fresh owner token per attempt.

    A constant value cannot distinguish holders, which is what made the
    unconditional release look correct.
    """
    return str(uuid.uuid4())


async def acquire(redis, key: str, token: str,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """SET NX EX. True when this caller now owns the lock."""
    return bool(await redis.set(key, token, nx=True, ex=ttl_seconds))


async def release(redis, key: str, token: str) -> int:
    """Compare-and-delete. 1 when released, 0 when not the owner."""
    return int(await redis.eval(RELEASE_SCRIPT, 1, key, token) or 0)


async def current_owner(redis, key: str) -> Optional[str]:
    """Who holds the lock, if anyone. Diagnostics only."""
    value = await redis.get(key)
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)
