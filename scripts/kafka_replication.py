#!/usr/bin/env python3
"""
Report and repair the replication factor of every Kafka topic.

Raising the cluster from one broker to three replicates nothing that already
exists. Topics carry the replica assignment they were created with, so every
topic from the single-broker era still has exactly one copy of every partition
and will keep it until somebody reassigns it. Nothing complains: the cluster is
green, every produce succeeds, and stopping one container loses the data.

    python scripts/kafka_replication.py            # report
    python scripts/kafka_replication.py --fix      # reassign, then re-report

The decisions -- parsing, what counts as under-replicated, which brokers hold
which partition -- live in shared/libs/python-common/kafka_replication.py and
are unit tested. This file is the part that talks to a broker.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "kafka_replication_rules",
    ROOT / "shared" / "libs" / "python-common" / "kafka_replication.py")
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)

# Commands run through a broker container rather than a Kafka client, so this
# needs no Python dependencies at all -- the same reason bootstrap_schema.py
# shells into postgres.
BROKER_CONTAINER = "ecommerce-platform-kafka-1"
BOOTSTRAP = "localhost:29092"
BROKERS = [1, 2, 3]


def kafka(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", BROKER_CONTAINER, *args],
        capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"{' '.join(args[:1])} failed:\n{result.stderr.strip()}")
    return result.stdout


def describe_all() -> list:
    return rules.parse_describe(
        kafka("kafka-topics", "--bootstrap-server", BOOTSTRAP, "--describe"))


def live_broker_ids() -> list:
    """Which brokers ZooKeeper currently has registered.

    Asked rather than assumed: generating a three-replica plan while only two
    brokers are up produces a plan Kafka rejects halfway through, leaving some
    topics reassigned and some not.
    """
    output = kafka("zookeeper-shell", "zookeeper:2181", "ls", "/brokers/ids",
                   check=False)
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            return sorted(int(v) for v in line[1:-1].split(",") if v.strip())
    return []


def reassign(topics) -> None:
    plan = rules.plan_reassignment(topics, BROKERS)
    payload = json.dumps(plan)

    print(f"\nreassigning {len(plan['partitions'])} partition(s) across brokers {BROKERS}")

    # The plan goes in through stdin: writing it to a file would mean writing
    # it inside the container, and the container has no editor.
    write = subprocess.run(
        ["docker", "exec", "-i", BROKER_CONTAINER,
         "sh", "-c", "cat > /tmp/reassign.json"],
        input=payload, capture_output=True, text=True)
    if write.returncode != 0:
        sys.exit(f"could not stage the plan: {write.stderr.strip()}")

    print(kafka("kafka-reassign-partitions", "--bootstrap-server", BOOTSTRAP,
                "--reassignment-json-file", "/tmp/reassign.json", "--execute"))

    # Reassignment is asynchronous: the command returns as soon as the
    # controller accepts the plan, long before the replicas have copied
    # anything. Reporting success here would be reporting that a plan was
    # submitted, not that the data is replicated.
    for attempt in range(60):
        verify = kafka("kafka-reassign-partitions", "--bootstrap-server", BOOTSTRAP,
                       "--reassignment-json-file", "/tmp/reassign.json", "--verify",
                       check=False)
        if "is still in progress" not in verify:
            print(verify.strip()[-2000:])
            return
        if attempt % 5 == 0:
            print(f"  still copying... ({attempt * 2}s)")
        time.sleep(2)
    sys.exit("reassignment did not finish within 120s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true",
                        help="reassign under-replicated topics to all brokers")
    args = parser.parse_args()

    live = live_broker_ids()
    print(f"brokers registered: {live or 'unknown'}")
    if args.fix and len(live) < rules.TARGET_REPLICATION_FACTOR:
        sys.exit(f"refusing to plan {rules.TARGET_REPLICATION_FACTOR} replicas "
                 f"with {len(live)} broker(s) up -- start them all first")

    topics = describe_all()
    print(rules.summarise(topics))

    under = rules.find_under_replicated(topics)
    if not under:
        return 0

    if not args.fix:
        print(f"\n{len(under)} topic(s) under-replicated. Re-run with --fix to reassign.")
        return 1

    reassign(under)

    # Re-describe rather than trusting the verify output: this is the only
    # statement that comes from the cluster's current state.
    remaining = rules.find_under_replicated(describe_all())
    print()
    print(rules.summarise(describe_all()))
    return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
