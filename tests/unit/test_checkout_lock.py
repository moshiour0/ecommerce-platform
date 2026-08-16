"""
Unit tests for the checkout mutual-exclusion protocol.

Concurrency correctness cannot be proved by unit tests, so these do the part
that can be: they pin the *ownership* semantics of the lock against a Redis
model with controllable time. The bug this replaces -- an unconditional DELETE
on release -- required a TTL to expire mid-checkout while a second request held
the lock. That interleave never happens in a happy-path test and is awkward to
force against a real Redis; against a fake clock it is three lines.

A real concurrency check lives in tests/integration/test_checkout_mutex.py and
runs against the deployed service.

Run:  python -m pytest tests/unit -q
"""

import pytest

from conftest import checkout_lock

RELEASE_SCRIPT = checkout_lock.RELEASE_SCRIPT
acquire = checkout_lock.acquire
release = checkout_lock.release
current_owner = checkout_lock.current_owner
lock_key_for = checkout_lock.lock_key_for
new_token = checkout_lock.new_token


class FakeRedis:
    """Enough Redis to model the lock, with time under test control.

    Implements SET NX EX, GET, and EVAL of a compare-and-delete. Expiry is
    driven by an explicit clock rather than sleeping, so the race the bug
    needed is reproducible instead of occasional.
    """

    def __init__(self):
        self.store: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key):
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self.now >= expires_at:
            del self.store[key]          # lazy expiry, as Redis does
            return None
        return value

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._live(key) is not None:
            return None                  # redis-py returns None when NX fails
        self.store[key] = (value, self.now + ex if ex is not None else None)
        return True

    async def get(self, key):
        return self._live(key)

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    async def eval(self, script, numkeys, *args):
        assert script == RELEASE_SCRIPT, "unexpected script; test models only release"
        key, token = args[0], args[1]
        if self._live(key) == token:
            del self.store[key]
            return 1
        return 0


@pytest.fixture
def redis():
    return FakeRedis()


KEY = "lock:checkout:user-1"


# ---------------------------------------------------------------------------
# basic mutual exclusion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_caller_acquires(redis):
    assert await acquire(redis, KEY, "tok-a") is True
    assert await current_owner(redis, KEY) == "tok-a"


@pytest.mark.asyncio
async def test_second_caller_is_refused_while_held(redis):
    await acquire(redis, KEY, "tok-a")
    assert await acquire(redis, KEY, "tok-b") is False, "two holders at once"


@pytest.mark.asyncio
async def test_release_frees_the_lock_for_the_next_caller(redis):
    await acquire(redis, KEY, "tok-a")
    assert await release(redis, KEY, "tok-a") == 1
    assert await acquire(redis, KEY, "tok-b") is True


@pytest.mark.asyncio
async def test_locks_are_per_user_not_global(redis):
    assert await acquire(redis, lock_key_for("alice"), "tok-a") is True
    assert await acquire(redis, lock_key_for("bob"), "tok-b") is True, \
        "one user's checkout must not block another's"


# ---------------------------------------------------------------------------
# ownership -- the actual bug
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_owner_cannot_release(redis):
    await acquire(redis, KEY, "tok-a")
    assert await release(redis, KEY, "tok-b") == 0, "released a lock it never held"
    assert await current_owner(redis, KEY) == "tok-a"


@pytest.mark.asyncio
async def test_expired_holder_cannot_release_the_new_holders_lock(redis):
    """The original defect, reproduced exactly.

    A slow checkout outlives the TTL, a second request acquires the lock, and
    the first request's finally-block runs. With an unconditional DELETE it
    would free a lock someone else holds, leaving two checkouts running for
    one cart. Compare-and-delete makes it a no-op.
    """
    await acquire(redis, KEY, "slow-request", ttl_seconds=10)
    redis.advance(11)                                  # TTL lapses mid-checkout
    assert await acquire(redis, KEY, "second-request") is True

    freed = await release(redis, KEY, "slow-request")  # the late finally-block

    assert freed == 0, "the expired holder deleted someone else's lock"
    assert await current_owner(redis, KEY) == "second-request", \
        "mutual exclusion was dropped while a checkout was in flight"


@pytest.mark.asyncio
async def test_release_is_idempotent(redis):
    await acquire(redis, KEY, "tok-a")
    assert await release(redis, KEY, "tok-a") == 1
    assert await release(redis, KEY, "tok-a") == 0, "second release must be a no-op"


@pytest.mark.asyncio
async def test_releasing_an_absent_lock_is_harmless(redis):
    assert await release(redis, KEY, "tok-a") == 0


@pytest.mark.asyncio
async def test_tokens_are_unique_per_attempt():
    """A constant token cannot distinguish holders, which is what made the
    unconditional release look correct."""
    assert len({new_token() for _ in range(1000)}) == 1000


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_expires_so_a_crashed_holder_cannot_wedge_checkout(redis):
    await acquire(redis, KEY, "crashed", ttl_seconds=10)
    redis.advance(11)
    assert await current_owner(redis, KEY) is None
    assert await acquire(redis, KEY, "next") is True


@pytest.mark.asyncio
async def test_lock_survives_right_up_to_its_ttl(redis):
    await acquire(redis, KEY, "tok-a", ttl_seconds=10)
    redis.advance(9.9)
    assert await acquire(redis, KEY, "tok-b") is False, "expired early"


@pytest.mark.asyncio
async def test_acquire_always_sets_an_expiry(redis):
    """A lock with no TTL wedges a user's checkout permanently if the holder
    dies before releasing."""
    await acquire(redis, KEY, "tok-a")
    _value, expires_at = redis.store[KEY]
    assert expires_at is not None, "lock acquired without a TTL"


# ---------------------------------------------------------------------------
# the script itself
# ---------------------------------------------------------------------------

def test_release_script_compares_before_deleting():
    """Guards against someone 'simplifying' this back to an unconditional DEL,
    which is the bug it replaced."""
    assert "get" in RELEASE_SCRIPT and "del" in RELEASE_SCRIPT
    assert "ARGV[1]" in RELEASE_SCRIPT, "release must compare against the token"
    assert RELEASE_SCRIPT.index("get") < RELEASE_SCRIPT.index("del"), \
        "the comparison must precede the delete"


def test_release_is_a_single_script_not_two_calls():
    """GET then DEL is not atomic: the lock can expire and be re-acquired
    between them, which is the same defect wearing a different hat."""
    assert RELEASE_SCRIPT.count("redis.call") == 2, \
        "check and delete must happen inside one script"
