"""
Replication decisions for Kafka topics, as pure functions.

Raising the brokers from one to three does nothing to topics that already
exist: a topic created when there was one broker keeps one replica forever, and
`kafka-topics --describe` is the only place that shows it. So "we run RF=3" is
a claim about every topic individually, and the only honest way to hold it is
to enumerate them.

Everything here is a decision about text and numbers -- no client, no broker,
no network. scripts/kafka_replication.py supplies the I/O. That split is what
lets the plan generator be tested against a topic layout nobody has to boot.
"""

from typing import Dict, Iterable, List, Sequence

# Three copies, two of which must acknowledge a write. See docker-compose.yml
# for why two brokers cannot express this.
TARGET_REPLICATION_FACTOR = 3
TARGET_MIN_INSYNC_REPLICAS = 2


class Topic:
    """One topic's replication state, as reported by kafka-topics --describe."""

    def __init__(self, name: str, partitions: Dict[int, List[int]],
                 isr: Dict[int, List[int]] = None):
        self.name = name
        # partition -> the brokers holding a replica, leader first
        self.partitions = partitions
        self.isr = isr or {}

    @property
    def replication_factor(self) -> int:
        """The smallest replica count across partitions.

        The minimum rather than the maximum, on purpose: a topic where one
        partition out of six has a single replica is exactly as exposed as a
        topic where all six do, and reporting the maximum would hide it.
        """
        if not self.partitions:
            return 0
        return min(len(replicas) for replicas in self.partitions.values())

    def under_replicated_partitions(self, target: int) -> List[int]:
        return sorted(p for p, r in self.partitions.items() if len(r) < target)

    def out_of_sync_partitions(self) -> List[int]:
        """Partitions whose ISR is smaller than their replica set.

        Distinct from under-replication: the replicas are assigned, but at
        least one is not caught up. Transient during a reassignment, and a
        standing alarm otherwise.
        """
        return sorted(p for p, replicas in self.partitions.items()
                      if len(self.isr.get(p, replicas)) < len(replicas))

    def __repr__(self):
        return f"Topic({self.name!r}, rf={self.replication_factor})"


def parse_describe(output: str) -> List[Topic]:
    """Parse `kafka-topics --describe` into Topic objects.

    The format is two kinds of line, and the per-partition line starts with
    the same "Topic:" token as the header, which is the thing that makes naive
    parsing wrong. Partition lines are distinguished by carrying a
    "Partition:" field, not by leading whitespace -- the indentation is a tab
    that survives some shells and not others.
    """
    topics: Dict[str, Topic] = {}
    order: List[str] = []

    for raw in output.splitlines():
        line = raw.strip()
        if not line or not line.startswith("Topic:"):
            continue

        fields = {}
        for part in line.split("\t"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            key, _, value = part.partition(":")
            fields[key.strip()] = value.strip()

        name = fields.get("Topic")
        if name is None:
            continue

        if name not in topics:
            topics[name] = Topic(name, {}, {})
            order.append(name)

        if "Partition" not in fields:
            continue  # the header line; the partition lines carry the detail

        partition = int(fields["Partition"])
        topics[name].partitions[partition] = _int_list(fields.get("Replicas", ""))
        topics[name].isr[partition] = _int_list(fields.get("Isr", ""))

    return [topics[name] for name in order]


def _int_list(value: str) -> List[int]:
    return [int(v) for v in value.split(",") if v.strip()]


def is_internal(name: str) -> bool:
    """Kafka's own topics.

    Not skipped -- __consumer_offsets at RF=1 loses every consumer group's
    position, which for consumers set to auto.offset.reset=earliest means
    reprocessing the entire event history. They are named separately only so a
    report can say which failures are the platform's and which are the
    broker's.
    """
    return name.startswith("__") or name.startswith("_")


def find_under_replicated(topics: Iterable[Topic],
                          target: int = TARGET_REPLICATION_FACTOR) -> List[Topic]:
    return [t for t in topics if t.replication_factor < target]


def plan_replicas(partition: int, brokers: Sequence[int], target: int) -> List[int]:
    """Which brokers should hold one partition's replicas.

    Round robin with the starting broker rotated by partition, so leadership
    spreads across the cluster instead of piling every partition's leader onto
    the same broker. With 3 brokers and 3 replicas the membership is identical
    for every partition and only the order differs -- but the order is what
    picks the preferred leader, so it still matters.
    """
    if target > len(brokers):
        raise ValueError(
            f"cannot place {target} replicas on {len(brokers)} broker(s): "
            "a partition may not have two replicas on one broker")
    ordered = sorted(brokers)
    start = partition % len(ordered)
    return [ordered[(start + i) % len(ordered)] for i in range(target)]


def plan_reassignment(topics: Iterable[Topic], brokers: Sequence[int],
                      target: int = TARGET_REPLICATION_FACTOR) -> dict:
    """The JSON kafka-reassign-partitions consumes.

    Every partition of every topic passed in is listed, including ones already
    at the target: a partial plan is a plan that silently leaves some
    partitions behind, and the caller has already decided which topics to
    include.
    """
    partitions = []
    for topic in sorted(topics, key=lambda t: t.name):
        for partition in sorted(topic.partitions):
            partitions.append({
                "topic": topic.name,
                "partition": partition,
                "replicas": plan_replicas(partition, brokers, target),
            })
    return {"version": 1, "partitions": partitions}


def summarise(topics: Sequence[Topic],
              target: int = TARGET_REPLICATION_FACTOR) -> str:
    under = find_under_replicated(topics, target)
    out_of_sync = [t for t in topics if t.out_of_sync_partitions()]

    lines = [f"{len(topics)} topic(s), target replication factor {target}"]
    if not under and not out_of_sync:
        lines.append(f"  all topics have {target} replicas and a full ISR")
        return "\n".join(lines)

    for topic in under:
        lines.append(f"  UNDER-REPLICATED  {topic.name}: rf={topic.replication_factor}"
                     f" partitions={topic.under_replicated_partitions(target)}")
    for topic in out_of_sync:
        lines.append(f"  OUT-OF-SYNC       {topic.name}: partitions="
                     f"{topic.out_of_sync_partitions()}")
    return "\n".join(lines)
