"""
What the event mesh does when a broker dies.

Three brokers and `replication.factor=3` are a configuration, not a property.
The configuration is satisfied by a cluster that loses every acknowledged write
the moment a broker stops -- all it takes is a producer using acks=1, or an
existing topic that was created when there was one broker and never reassigned,
or unclean leader election quietly promoting a replica that never received the
data. Each of those looks completely healthy until the broker actually goes.

So this stops one, for real, and asks the cluster three questions:

  1. Does an acknowledged write survive? Messages produced with acks=all
     before the outage must all be readable after it. This is the assertion
     that fails if a topic is still RF=1: the data was only ever on the broker
     that just died.

  2. Do writes continue? With RF=3 and min.insync.replicas=2, two surviving
     brokers are still enough to acknowledge. If produces start failing with
     NOT_ENOUGH_REPLICAS, the cluster is configured to stop rather than
     degrade -- which is what min.insync.replicas=3, or RF=2, would give.

  3. Does it heal? When the broker returns, every partition must get back to
     three in-sync replicas on its own. A cluster that stays under-replicated
     after the broker is back is one more failure from data loss and nobody is
     being told.

It also checks the setting that makes the difference between "unavailable" and
"silently truncated": unclean.leader.election.enable must be false. With it on,
a broker that was not in the ISR can be elected leader, and every message it
never received is dropped with no error anywhere in the system.

Not in CI. It needs the Kafka layer -- three brokers plus ZooKeeper, four JVMs
-- and a GitHub-hosted runner on a private repository is 2 cores and 8 GB. Run
it by hand after touching the event mesh.
"""

import json
import subprocess
import sys
import time
import uuid

from config import describe

# Stopped and started by name, so this test only runs against compose. Against
# the cluster the equivalent is deleting a broker pod, which a StatefulSet
# recreates on its own schedule.
VICTIM = "ecommerce-platform-kafka-3-1"
SURVIVOR = "ecommerce-platform-kafka-1"
BOOTSTRAP = "kafka:29092,kafka-2:29092,kafka-3:29092"

TOPIC = f"BrokerLoss.probe.{uuid.uuid4().hex[:8]}"
BEFORE = 20
DURING = 20

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def broker(*args, container=SURVIVOR, stdin=None, timeout=120):
    result = subprocess.run(["docker", "exec", "-i", container, *args],
                            input=stdin, capture_output=True, text=True,
                            timeout=timeout)
    return result


def produce(messages, container=SURVIVOR):
    """Produce with acks=all, and report whether the broker acknowledged.

    request-required-acks=-1 is the whole point: with acks=1 every assertion
    below passes on a cluster that loses data, because the leader acknowledges
    before anyone else has a copy.
    """
    payload = "\n".join(messages) + "\n"
    result = broker("kafka-console-producer",
                    "--bootstrap-server", BOOTSTRAP,
                    "--topic", TOPIC,
                    "--request-required-acks", "-1",
                    container=container, stdin=payload)
    return result.returncode == 0, result.stderr.strip()


def consume_all(expected, container=SURVIVOR):
    result = broker("kafka-console-consumer",
                    "--bootstrap-server", BOOTSTRAP,
                    "--topic", TOPIC,
                    "--from-beginning",
                    "--max-messages", str(expected),
                    "--timeout-ms", "30000",
                    container=container)
    return [line for line in result.stdout.splitlines() if line.strip()]


def describe_topic(container=SURVIVOR):
    result = broker("kafka-topics", "--bootstrap-server", BOOTSTRAP,
                    "--describe", "--topic", TOPIC, container=container)
    return result.stdout


def isr_sizes(output):
    sizes = []
    for line in output.splitlines():
        if "Partition:" not in line:
            continue
        for field in line.split("\t"):
            field = field.strip()
            if field.startswith("Isr:"):
                sizes.append(len([v for v in field[4:].split(",") if v.strip()]))
    return sizes


def main():
    print("=" * 62)
    print(" EVENT MESH UNDER BROKER LOSS")
    print("=" * 62)
    print(f"-> {describe()}")

    print("\n0. Checking unclean leader election is off...")
    # Asked first because if it is on, everything below can pass while the
    # cluster is still capable of dropping acknowledged writes.
    config = broker("kafka-configs", "--bootstrap-server", BOOTSTRAP,
                    "--entity-type", "brokers", "--entity-name", "1",
                    "--describe", "--all").stdout
    unclean_off = "unclean.leader.election.enable=false" in config
    check(unclean_off, "unclean leader election is disabled",
          "unclean.leader.election.enable is not false: a replica that never "
          "received the data can be elected leader, dropping acknowledged "
          "writes with no error anywhere")

    print(f"\n1. Creating {TOPIC} with three replicas...")
    created = broker("kafka-topics", "--bootstrap-server", BOOTSTRAP,
                     "--create", "--topic", TOPIC,
                     "--partitions", "3", "--replication-factor", "3",
                     "--config", "min.insync.replicas=2")
    check(created.returncode == 0, "topic created with RF=3, min.insync=2",
          f"could not create the topic: {created.stderr.strip()[:300]}")
    if created.returncode != 0:
        return finish()

    initial = isr_sizes(describe_topic())
    check(initial and all(size == 3 for size in initial),
          f"all {len(initial)} partitions start with 3 in-sync replicas",
          f"partitions started with ISR sizes {initial}, expected all 3")

    print(f"\n2. Producing {BEFORE} messages with acks=all...")
    sent_before = [f"before-{i}" for i in range(BEFORE)]
    ok, err = produce(sent_before)
    check(ok, f"{BEFORE} messages acknowledged by the cluster",
          f"produce failed while all brokers were up: {err[:300]}")

    print(f"\n3. Stopping {VICTIM}...")
    subprocess.run(["docker", "stop", VICTIM], capture_output=True, timeout=90)
    # The controller needs a moment to notice and elect new leaders for the
    # partitions the stopped broker was leading.
    time.sleep(10)
    after_loss = isr_sizes(describe_topic())
    print(f"      ISR sizes now {after_loss}")
    check(after_loss and all(size >= 2 for size in after_loss),
          "every partition still has at least 2 in-sync replicas",
          f"ISR fell to {after_loss}; with min.insync.replicas=2 those "
          f"partitions can no longer accept writes")

    try:
        print(f"\n4. Producing {DURING} more, with the broker still down...")
        ok, err = produce([f"during-{i}" for i in range(DURING)])
        check(ok, f"{DURING} more messages acknowledged with one broker down",
              f"produce failed during the outage: {err[:300]}. Two surviving "
              f"brokers satisfy min.insync.replicas=2, so this should succeed.")

        print("\n5. Reading everything back...")
        received = consume_all(BEFORE + DURING)
        missing = [m for m in sent_before if m not in received]
        check(not missing,
              f"all {BEFORE} pre-outage messages survived the broker loss",
              f"{len(missing)} acknowledged message(s) lost when the broker "
              f"stopped: {missing[:5]}. This is what a topic still at RF=1, "
              f"or a producer using acks=1, looks like.")
        check(len(received) >= BEFORE + DURING,
              f"read back {len(received)} of {BEFORE + DURING} messages",
              f"read back only {len(received)} of {BEFORE + DURING}")

    finally:
        # Always restart it, including when an assertion above blew up. Leaving
        # a broker stopped would break every subsequent run and every other
        # test that touches the mesh.
        print(f"\n6. Restarting {VICTIM}...")
        subprocess.run(["docker", "start", VICTIM], capture_output=True, timeout=120)

    print("\n7. Waiting for the cluster to heal...")
    healed = []
    for attempt in range(30):
        time.sleep(4)
        healed = isr_sizes(describe_topic())
        if healed and all(size == 3 for size in healed):
            print(f"      back to 3 in-sync replicas after ~{(attempt + 1) * 4}s")
            break
    check(healed and all(size == 3 for size in healed),
          "every partition returned to 3 in-sync replicas",
          f"partitions stayed at ISR {healed} after the broker came back. The "
          f"cluster is one failure from data loss and nothing is reporting it.")

    print("\n8. Cleaning up the probe topic...")
    broker("kafka-topics", "--bootstrap-server", BOOTSTRAP,
           "--delete", "--topic", TOPIC)

    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] Acknowledged writes survived a broker loss, writes "
          "continued during it, and the cluster healed on its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
