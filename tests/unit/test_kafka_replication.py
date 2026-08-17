"""
Unit tests for the topic replication rules.

The property under test is that raising the broker count does not, by itself,
replicate anything. A topic created against a single broker keeps one replica
until something reassigns it, and the failure is invisible: the cluster is
green, every producer succeeds, and one container stop loses the data.

The parser gets the most attention because it is the part that can be
confidently wrong. `kafka-topics --describe` starts both its header line and
its partition lines with "Topic:", so a parser that keys on that prefix reads
every header as a partition and reports a replication factor of zero -- or
worse, reads partition lines as topics and reports everything as fine.
"""

import pytest

from conftest import kafka_replication

Topic = kafka_replication.Topic
parse_describe = kafka_replication.parse_describe
find_under_replicated = kafka_replication.find_under_replicated
plan_replicas = kafka_replication.plan_replicas
plan_reassignment = kafka_replication.plan_reassignment
summarise = kafka_replication.summarise
is_internal = kafka_replication.is_internal
TARGET = kafka_replication.TARGET_REPLICATION_FACTOR


# Real output, copied from the single-broker cluster this change replaces.
SINGLE_BROKER = (
    "Topic: Cart.events\tTopicId: hHs0c1VGT-eskwZxyZB3OQ\tPartitionCount: 1\t"
    "ReplicationFactor: 1\tConfigs: \n"
    "\tTopic: Cart.events\tPartition: 0\tLeader: 1\tReplicas: 1\tIsr: 1\n"
)

REPLICATED = (
    "Topic: Order.events\tTopicId: abc\tPartitionCount: 2\t"
    "ReplicationFactor: 3\tConfigs: min.insync.replicas=2\n"
    "\tTopic: Order.events\tPartition: 0\tLeader: 1\tReplicas: 1,2,3\tIsr: 1,2,3\n"
    "\tTopic: Order.events\tPartition: 1\tLeader: 2\tReplicas: 2,3,1\tIsr: 2,3,1\n"
)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_a_single_broker_topic_is_read_as_one_replica():
    topics = parse_describe(SINGLE_BROKER)
    assert len(topics) == 1
    assert topics[0].name == "Cart.events"
    assert topics[0].replication_factor == 1


def test_the_header_line_does_not_become_a_partition():
    # The bug this guards: both lines start with "Topic:", so keying on that
    # prefix invents a partition from the header.
    topics = parse_describe(SINGLE_BROKER)
    assert list(topics[0].partitions) == [0]


def test_replicas_are_read_in_order():
    topics = parse_describe(REPLICATED)
    assert topics[0].partitions == {0: [1, 2, 3], 1: [2, 3, 1]}
    assert topics[0].replication_factor == 3


def test_several_topics_keep_their_order_and_stay_separate():
    topics = parse_describe(SINGLE_BROKER + REPLICATED)
    assert [t.name for t in topics] == ["Cart.events", "Order.events"]
    assert topics[0].replication_factor == 1
    assert topics[1].replication_factor == 3


def test_noise_between_topics_is_ignored():
    topics = parse_describe("\n" + SINGLE_BROKER + "\nsome warning line\n" + REPLICATED)
    assert [t.name for t in topics] == ["Cart.events", "Order.events"]


def test_empty_output_is_no_topics_rather_than_an_error():
    assert parse_describe("") == []


# ---------------------------------------------------------------------------
# what counts as under-replicated
# ---------------------------------------------------------------------------

def test_the_replication_factor_is_the_worst_partition_not_the_best():
    # Six partitions where one has a single replica is exactly as exposed as
    # six that all do. Reporting the maximum would call this healthy.
    topic = Topic("Mixed.events", {0: [1, 2, 3], 1: [1, 2, 3], 2: [1]})
    assert topic.replication_factor == 1
    assert topic.under_replicated_partitions(TARGET) == [2]


def test_a_fully_replicated_topic_is_not_reported():
    topics = parse_describe(REPLICATED)
    assert find_under_replicated(topics) == []


def test_internal_topics_are_named_but_never_exempt():
    # __consumer_offsets at RF=1 rewinds every consumer group on one broker
    # loss. It is labelled differently in reports; it is not skipped.
    offsets = Topic("__consumer_offsets", {0: [1]})
    assert is_internal(offsets.name)
    assert find_under_replicated([offsets]) == [offsets]


def test_a_full_replica_set_that_is_not_in_sync_is_a_separate_finding():
    # Assigned three, only two caught up. Not under-replicated -- the
    # reassignment already happened -- but not healthy either.
    topic = Topic("Lagging.events", {0: [1, 2, 3]}, isr={0: [1, 2]})
    assert topic.replication_factor == 3
    assert find_under_replicated([topic]) == []
    assert topic.out_of_sync_partitions() == [0]


def test_isr_defaults_to_the_replica_set_when_it_was_never_reported():
    # A Topic built by hand, without ISR data, must not read as out of sync.
    assert Topic("Hand.made", {0: [1, 2, 3]}).out_of_sync_partitions() == []


# ---------------------------------------------------------------------------
# reassignment plans
# ---------------------------------------------------------------------------

def test_a_partition_never_gets_two_replicas_on_one_broker():
    replicas = plan_replicas(0, [1, 2, 3], 3)
    assert sorted(replicas) == [1, 2, 3]
    assert len(set(replicas)) == 3


def test_more_replicas_than_brokers_is_refused_rather_than_truncated():
    # Kafka rejects this too, but far later -- after the plan has been written
    # and half-executed. Silently returning two replicas for a target of three
    # would be worse than either.
    with pytest.raises(ValueError, match="two replicas on one broker"):
        plan_replicas(0, [1, 2], 3)


def test_leadership_rotates_across_partitions():
    # Same three brokers every time, but the first entry is the preferred
    # leader, so a plan that starts every partition at broker 1 elects broker 1
    # for all of them.
    leaders = [plan_replicas(p, [1, 2, 3], 3)[0] for p in range(6)]
    assert leaders == [1, 2, 3, 1, 2, 3]


def test_the_plan_covers_every_partition_of_every_topic():
    topics = parse_describe(SINGLE_BROKER + REPLICATED)
    plan = plan_reassignment(topics, [1, 2, 3])
    assert plan["version"] == 1
    covered = {(p["topic"], p["partition"]) for p in plan["partitions"]}
    assert covered == {("Cart.events", 0), ("Order.events", 0), ("Order.events", 1)}


def test_every_planned_partition_reaches_the_target():
    topics = parse_describe(SINGLE_BROKER)
    plan = plan_reassignment(topics, [1, 2, 3])
    assert all(len(p["replicas"]) == TARGET for p in plan["partitions"])


def test_the_plan_is_stable_across_runs():
    # Two identical runs must produce byte-identical JSON, or a --verify that
    # diffs the plan against the cluster reports drift that is not there.
    topics = parse_describe(SINGLE_BROKER + REPLICATED)
    assert plan_reassignment(topics, [1, 2, 3]) == plan_reassignment(topics, [3, 1, 2])


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_a_healthy_cluster_says_so_without_listing_topics():
    assert "all topics have 3 replicas" in summarise(parse_describe(REPLICATED))


def test_an_under_replicated_topic_is_named_in_the_report():
    text = summarise(parse_describe(SINGLE_BROKER))
    assert "UNDER-REPLICATED" in text
    assert "Cart.events" in text
