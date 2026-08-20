"""
Where Redis is, and whether its replication is healthy.

A replica by itself buys almost nothing. Everything this platform keeps in
Redis has a durable counterpart -- carts are in Postgres, webhook dedup has the
processed_webhooks table behind it, and rate-limit budgets are meant to be
short-lived. So losing the data is survivable by design, and test_07 and
test_08 prove it. What is *not* survivable is Redis being unreachable: carts
stop serving, the limiter stops answering, and the fast path of webhook dedup
falls back to Postgres for every call.

That makes the thing worth buying **failover time**, not durability. A replica
with no automatic promotion still needs a human to notice and act, which is the
same outage with extra steps. So the topology is primary + replica + a Sentinel
quorum, and clients connect through Sentinel rather than to a host.

Everything in this module is a decision about strings and dictionaries. The
one function that builds a client imports redis lazily, so the unit tier can
load this file with nothing installed.
"""

from typing import List, Optional, Tuple

# Sentinel's name for the primary. Every sentinel and every client must agree
# on it or they are monitoring different things and nobody says so.
DEFAULT_MASTER_NAME = "mymaster"

# Two of three. A quorum of one would let a single sentinel that has merely
# lost the network declare a failover and promote a replica while the real
# primary is still being written to -- two primaries, both accepting writes,
# which is worse than the outage it was trying to fix.
DEFAULT_QUORUM = 2


class Decision:
    """A yes/no with the reason attached, matching the other rules modules."""

    def __init__(self, ok: bool, detail: str = ""):
        self.ok = ok
        self.detail = detail

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"Decision(ok={self.ok}, detail={self.detail!r})"


def parse_sentinel_hosts(value: Optional[str]) -> List[Tuple[str, int]]:
    """Parse "host:port,host:port" into [(host, port)].

    Malformed entries raise instead of being skipped. A typo that silently
    drops one sentinel from a three-sentinel list leaves a quorum of two out of
    two, which cannot tolerate losing either -- and nothing would report it.
    """
    if not value or not value.strip():
        return []

    hosts = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        host, sep, port = chunk.rpartition(":")
        if not sep or not host:
            raise ValueError(
                f"sentinel entry {chunk!r} is not host:port; a sentinel list "
                f"with a typo silently shrinks the quorum")
        try:
            hosts.append((host, int(port)))
        except ValueError:
            raise ValueError(f"sentinel entry {chunk!r} has a non-numeric port")
    return hosts


def connection_mode(redis_url: Optional[str],
                    sentinel_hosts: Optional[str]) -> str:
    """Either "sentinel" or "direct".

    Sentinel wins when both are configured. REDIS_URL is already present in
    every service's environment, so treating it as the tiebreaker would mean a
    service silently ignoring its sentinel list and connecting straight to a
    host that is about to be demoted.
    """
    if parse_sentinel_hosts(sentinel_hosts):
        return "sentinel"
    if redis_url and redis_url.strip():
        return "direct"
    raise ValueError(
        "neither REDIS_SENTINELS nor REDIS_URL is set. Rule 8: no default "
        "endpoint for shared infrastructure.")


def parse_info_replication(text: str) -> dict:
    """Parse the output of `INFO replication` into a dict.

    Values stay strings except the counters, which are converted because
    "connected_slaves:0" and "connected_slaves:1" differ by one character and
    comparing those as strings is how an off-by-one goes unnoticed.
    """
    info = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        info[key.strip()] = value.strip()

    for numeric in ("connected_slaves", "master_repl_offset", "slave_repl_offset"):
        if numeric in info:
            try:
                info[numeric] = int(info[numeric])
            except ValueError:
                pass
    return info


def is_primary(info: dict) -> bool:
    return info.get("role") == "master"


def replica_count(info: dict) -> int:
    value = info.get("connected_slaves", 0)
    return value if isinstance(value, int) else 0


def check_replication(primary_info: dict, replica_info: dict) -> Decision:
    """Is this pair actually replicating?

    Three ways it can look fine and not be:

      1. The replica is running but never connected. `role:slave` is set from
         config, not from a successful handshake, so a replica pointed at the
         wrong host still reports role:slave forever. master_link_status is
         the field that knows.
      2. The primary has zero connected replicas -- the same failure from the
         other side, and the side more likely to be looked at.
      3. Both report master. That is split brain: two writable Redises taking
         different data, and whichever loses the eventual failover has its
         writes discarded.
    """
    if not is_primary(primary_info):
        return Decision(False,
                        f"the node expected to be primary reports role="
                        f"{primary_info.get('role')!r}")

    if is_primary(replica_info):
        return Decision(False,
                        "both nodes report role=master: split brain, two "
                        "writable Redises accepting different data")

    link = replica_info.get("master_link_status")
    if link != "up":
        return Decision(False,
                        f"replica is not linked to the primary "
                        f"(master_link_status={link!r}); role:slave comes from "
                        f"config, not from a successful handshake")

    if replica_count(primary_info) < 1:
        return Decision(False,
                        "the primary reports 0 connected replicas while the "
                        "replica believes it is linked")

    return Decision(True, "primary has a linked replica")


def check_sentinel_quorum(sentinel_count: int, quorum: int,
                          reachable: int) -> Decision:
    """Can this sentinel set still agree to fail over?

    Asked because the answer degrades quietly. Losing one of three sentinels
    changes nothing a client can observe; losing the second changes everything
    -- at that point no failover can ever be agreed and the replica is
    decoration.
    """
    if quorum > sentinel_count:
        return Decision(False,
                        f"quorum {quorum} exceeds the {sentinel_count} "
                        f"sentinel(s) configured: no failover can ever be "
                        f"agreed")
    if sentinel_count < 3:
        return Decision(False,
                        f"{sentinel_count} sentinel(s) cannot survive losing "
                        f"one and still form a majority; three is the minimum")
    if reachable < quorum:
        return Decision(False,
                        f"only {reachable} of {sentinel_count} sentinels "
                        f"reachable, below the quorum of {quorum}: a primary "
                        f"failure now is an outage, not a failover")
    return Decision(True, f"{reachable}/{sentinel_count} sentinels reachable, "
                          f"quorum {quorum}")


def make_client(redis_url: Optional[str] = None,
                sentinel_hosts: Optional[str] = None,
                master_name: str = DEFAULT_MASTER_NAME,
                db: int = 0,
                use_asyncio: bool = False,
                **client_kwargs):
    """A Redis client that follows the primary, or a direct one.

    Reads go to the primary too -- `master_for`, never `slave_for`.
    Replication is asynchronous, so a replica read can serve a cart that was
    emptied a moment ago, or a rate-limit counter that is one increment
    behind. The first is a customer seeing items they removed; the second is a
    caller getting more requests than their budget, which is a bypass rather
    than a nuisance. The replica exists to be promoted, not to be read.

    redis is imported inside the function rather than at module scope so the
    unit tier can load this file with nothing installed, the same way
    fix_connectors.py defers requests.
    """
    mode = connection_mode(redis_url, sentinel_hosts)

    if use_asyncio:
        from redis.asyncio import Redis, Sentinel
        from redis.asyncio.retry import Retry
    else:
        from redis import Redis, Sentinel
        from redis.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError, TimeoutError

    # A failover takes a few seconds: the sentinels have to agree the primary
    # is down, elect a leader among themselves, promote a replica, and then
    # tell clients. Without a retry, every command in that window is an error
    # the caller sees.
    defaults = {
        "retry": Retry(ExponentialBackoff(cap=1.0, base=0.05), retries=3),
        "retry_on_error": [ConnectionError, TimeoutError],
        "health_check_interval": 30,
    }
    defaults.update(client_kwargs)

    if mode == "direct":
        return Redis.from_url(redis_url, db=db, **defaults)

    sentinel = Sentinel(
        parse_sentinel_hosts(sentinel_hosts),
        socket_timeout=1.0,
        # Only the encoding option is meaningful on the sentinel connections
        # themselves; the retry policy belongs to the primary connection the
        # pool hands out, not to the discovery hop.
        decode_responses=defaults.get("decode_responses", False),
    )
    return sentinel.master_for(master_name, db=db, **defaults)
