"""
Unit tests for the Redis topology rules.

The failures worth catching here are the quiet ones. A replica that never
connected still reports `role:slave`, because that field comes from its own
config rather than from a successful handshake -- so a replica pointed at the
wrong host looks correct forever, right up until the primary dies and there is
nothing to promote.

The same shape repeats at every level: a sentinel list with a typo silently
shrinks the quorum, a quorum of two out of two cannot survive losing either
sentinel, and a client that prefers REDIS_URL over its sentinel list connects
straight to a host that is about to be demoted. None of these produce an error
until the moment they matter.
"""

import pytest

from conftest import redis_topology

parse_sentinel_hosts = redis_topology.parse_sentinel_hosts
connection_mode = redis_topology.connection_mode
parse_info_replication = redis_topology.parse_info_replication
check_replication = redis_topology.check_replication
check_sentinel_quorum = redis_topology.check_sentinel_quorum
is_primary = redis_topology.is_primary
replica_count = redis_topology.replica_count


# Real `INFO replication` output, trimmed to the fields that carry meaning.
PRIMARY_WITH_REPLICA = """# Replication
role:master
connected_slaves:1
slave0:ip=172.19.0.5,port=6379,state=online,offset=1274,lag=0
master_failover_state:no-failover
master_replid:8e1f3c2a9b4d
master_repl_offset:1274
"""

PRIMARY_ALONE = """# Replication
role:master
connected_slaves:0
master_failover_state:no-failover
master_repl_offset:0
"""

LINKED_REPLICA = """# Replication
role:slave
master_host:redis
master_port:6379
master_link_status:up
slave_repl_offset:1274
slave_read_only:1
"""

# A replica pointed at a host that does not answer. Note role:slave is still
# set -- this is the case the master_link_status check exists for.
ORPHAN_REPLICA = """# Replication
role:slave
master_host:redsi
master_port:6379
master_link_status:down
master_link_down_since_seconds:412
slave_repl_offset:0
"""


# ---------------------------------------------------------------------------
# parsing INFO
# ---------------------------------------------------------------------------

def test_section_headers_and_blank_lines_are_skipped():
    info = parse_info_replication(PRIMARY_WITH_REPLICA)
    assert "# Replication" not in info
    assert info["role"] == "master"


def test_counters_come_back_as_integers():
    # "connected_slaves:0" and "connected_slaves:1" differ by one character;
    # comparing them as strings is how an off-by-one survives review.
    info = parse_info_replication(PRIMARY_WITH_REPLICA)
    assert info["connected_slaves"] == 1
    assert isinstance(info["connected_slaves"], int)


def test_a_value_containing_colons_is_kept_whole():
    # slave0 is "ip=...,port=...,state=online,..." and contains no colon, but
    # master_host on an IPv6 deployment does. Splitting on the first colon only
    # is what keeps that intact.
    info = parse_info_replication("role:slave\nmaster_host:fe80::1\n")
    assert info["master_host"] == "fe80::1"


def test_empty_info_is_an_empty_dict_rather_than_an_error():
    assert parse_info_replication("") == {}


def test_role_and_replica_count_read_from_a_parsed_info():
    assert is_primary(parse_info_replication(PRIMARY_WITH_REPLICA))
    assert not is_primary(parse_info_replication(LINKED_REPLICA))
    assert replica_count(parse_info_replication(PRIMARY_WITH_REPLICA)) == 1
    assert replica_count(parse_info_replication(PRIMARY_ALONE)) == 0


# ---------------------------------------------------------------------------
# is this pair actually replicating
# ---------------------------------------------------------------------------

def test_a_healthy_pair_passes():
    decision = check_replication(parse_info_replication(PRIMARY_WITH_REPLICA),
                                parse_info_replication(LINKED_REPLICA))
    assert decision.ok, decision.detail


def test_a_replica_that_never_connected_is_caught():
    # THE test in this file. role:slave is set from config, so this replica
    # looks correct in every way except the one that matters.
    decision = check_replication(parse_info_replication(PRIMARY_ALONE),
                                 parse_info_replication(ORPHAN_REPLICA))
    assert not decision.ok
    assert "master_link_status" in decision.detail


def test_a_primary_with_no_connected_replicas_is_caught():
    # The replica believes it is linked, the primary has never seen it. Can
    # happen mid-handshake, and is a standing fault otherwise.
    decision = check_replication(parse_info_replication(PRIMARY_ALONE),
                                 parse_info_replication(LINKED_REPLICA))
    assert not decision.ok
    assert "0 connected replicas" in decision.detail


def test_two_primaries_are_reported_as_split_brain():
    decision = check_replication(parse_info_replication(PRIMARY_WITH_REPLICA),
                                 parse_info_replication(PRIMARY_WITH_REPLICA))
    assert not decision.ok
    assert "split brain" in decision.detail


def test_a_demoted_primary_is_reported_rather_than_assumed():
    # After a failover the old primary comes back as a replica. Pointing this
    # check at it must say so instead of reading the pair as healthy.
    decision = check_replication(parse_info_replication(LINKED_REPLICA),
                                 parse_info_replication(LINKED_REPLICA))
    assert not decision.ok
    assert "expected to be primary" in decision.detail


# ---------------------------------------------------------------------------
# can the sentinels still agree
# ---------------------------------------------------------------------------

def test_three_sentinels_all_reachable_is_healthy():
    assert check_sentinel_quorum(3, 2, 3).ok


def test_losing_one_of_three_is_still_healthy():
    # The point of three: one can die and a majority still exists.
    assert check_sentinel_quorum(3, 2, 2).ok


def test_losing_two_of_three_means_no_failover_is_possible():
    decision = check_sentinel_quorum(3, 2, 1)
    assert not decision.ok
    assert "outage, not a failover" in decision.detail


def test_two_sentinels_are_refused_even_when_both_are_up():
    # Two sentinels with quorum 2 look fine until either one restarts, and
    # then no failover can ever be agreed. Reported now rather than then.
    decision = check_sentinel_quorum(2, 2, 2)
    assert not decision.ok
    assert "three is the minimum" in decision.detail


def test_a_quorum_larger_than_the_sentinel_count_can_never_be_met():
    decision = check_sentinel_quorum(3, 4, 3)
    assert not decision.ok
    assert "no failover can ever be agreed" in decision.detail


# ---------------------------------------------------------------------------
# where the client is told to connect
# ---------------------------------------------------------------------------

def test_a_sentinel_list_wins_over_a_direct_url():
    # REDIS_URL is in every service's environment already. If it won, adding
    # sentinels would change nothing and nobody would notice until a failover
    # left the service talking to a demoted host.
    assert connection_mode("redis://redis:6379",
                           "redis-sentinel-1:26379,redis-sentinel-2:26379") == "sentinel"


def test_a_url_alone_is_direct_mode():
    assert connection_mode("redis://redis:6379", None) == "direct"
    assert connection_mode("redis://redis:6379", "") == "direct"
    assert connection_mode("redis://redis:6379", "   ") == "direct"


def test_neither_configured_is_refused_rather_than_defaulted():
    # Rule 8: no default endpoint for shared infrastructure.
    with pytest.raises(ValueError, match="Rule 8"):
        connection_mode(None, None)


def test_a_sentinel_list_parses_with_whitespace_and_trailing_commas():
    hosts = parse_sentinel_hosts(" a:26379, b:26379 , c:26379, ")
    assert hosts == [("a", 26379), ("b", 26379), ("c", 26379)]


def test_a_malformed_sentinel_entry_raises_instead_of_being_skipped():
    # Skipping it would leave two sentinels with a quorum of two -- unable to
    # survive losing either, with nothing reporting the change.
    with pytest.raises(ValueError, match="shrinks the quorum"):
        parse_sentinel_hosts("redis-sentinel-1:26379,redis-sentinel-2")


def test_a_non_numeric_port_raises():
    with pytest.raises(ValueError, match="non-numeric port"):
        parse_sentinel_hosts("redis-sentinel-1:twentysix")


def test_an_empty_sentinel_list_is_no_hosts_not_an_error():
    assert parse_sentinel_hosts(None) == []
    assert parse_sentinel_hosts("") == []
