#!/usr/bin/env python3
"""
Report whether Redis replication and the sentinel quorum are actually healthy.

Both degrade quietly. A replica pointed at the wrong host reports `role:slave`
forever, because that field comes from its own config rather than from a
successful handshake. Losing one sentinel out of three changes nothing anyone
can observe, and losing the second changes everything -- from that point no
failover can be agreed and the replica is decoration.

    python scripts/check_redis.py

Exits non-zero if the pair is not replicating or the quorum cannot be met.

The decisions live in shared/libs/python-common/redis_topology.py and are unit
tested; this file only talks to containers.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "redis_topology_rules",
    ROOT / "shared" / "libs" / "python-common" / "redis_topology.py")
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)

# Both Redis nodes, in no particular order. Which one is currently the primary
# is asked, never assumed: sentinel does not fail back, so after a promotion
# the container named `redis` is the replica and stays that way. Hardcoding the
# roles here would report a healthy topology as broken every time a failover
# had actually worked.
NODES = ["ecommerce-platform-redis-1", "ecommerce-platform-redis-replica-1"]
SENTINELS = [f"ecommerce-platform-redis-sentinel-{n}-1" for n in (1, 2, 3)]
MASTER_NAME = "mymaster"


def redis_cli(container: str, *args: str, port: int = 6379) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "redis-cli", "-p", str(port), *args],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return ""
    return result.stdout


def container_ip(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
        capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def primary_address() -> str:
    """The address a quorum of sentinels currently calls the primary."""
    answers = []
    for name in SENTINELS:
        output = redis_cli(name, "sentinel", "get-master-addr-by-name",
                           MASTER_NAME, port=26379)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if lines:
            answers.append(lines[0])
    if not answers:
        return ""
    # The majority answer. Sentinels can disagree briefly mid-failover, and
    # taking whichever was asked first would make this script's output depend
    # on container ordering.
    return max(set(answers), key=answers.count)


def identify_nodes():
    """Map (primary, replica, address) from what the sentinels report."""
    address = primary_address()
    for name in NODES:
        if container_ip(name) and container_ip(name) == address:
            other = [n for n in NODES if n != name]
            return name, (other[0] if other else ""), address
    # No match: either the sentinels are unreachable or the primary is a node
    # this script does not know about. Fall back to the declared order and let
    # the replication check report whatever it actually finds.
    return NODES[0], NODES[1], address


def main() -> int:
    failures = []

    print("=" * 62)
    print(" REDIS TOPOLOGY")
    print("=" * 62)

    primary, replica, address = identify_nodes()
    print(f"\nsentinels name {address or '(no answer)'} as the primary")
    if primary != NODES[0]:
        print("  note: that is the promoted node, not the one named `redis`. "
              "Sentinel does not fail back, so this is a valid steady state.")

    primary_info = rules.parse_info_replication(redis_cli(primary, "info", "replication"))
    replica_info = rules.parse_info_replication(redis_cli(replica, "info", "replication"))

    print(f"\nprimary  {primary}")
    print(f"         role={primary_info.get('role')} "
          f"connected_replicas={rules.replica_count(primary_info)}")
    print(f"replica  {replica}")
    print(f"         role={replica_info.get('role')} "
          f"link={replica_info.get('master_link_status')}")

    decision = rules.check_replication(primary_info, replica_info)
    print(f"\n  {'[OK]  ' if decision.ok else '[FAIL]'} {decision.detail}")
    if not decision.ok:
        failures.append(decision.detail)

    # How far behind the replica is. Not a pass/fail -- replication is
    # asynchronous by design and a non-zero lag is normal -- but a lag that
    # keeps growing is the shape of a replica that cannot keep up.
    #
    # The two INFO calls are separate round trips, so the replica's offset can
    # read slightly *ahead* of the primary snapshot taken a moment earlier.
    # That is a sampling artefact rather than a replica running ahead of its
    # primary, which cannot happen; it is reported as a magnitude to avoid a
    # negative number that looks like a fault.
    primary_offset = primary_info.get("master_repl_offset", 0)
    replica_offset = replica_info.get("slave_repl_offset", 0)
    if isinstance(primary_offset, int) and isinstance(replica_offset, int):
        print(f"         offsets: primary={primary_offset} replica={replica_offset} "
              f"(|delta| {abs(primary_offset - replica_offset)} bytes, "
              f"sampled a moment apart)")

    print(f"\nsentinels (quorum {rules.DEFAULT_QUORUM})")
    reachable = 0
    for name in SENTINELS:
        output = redis_cli(name, "sentinel", "master", MASTER_NAME, port=26379)
        if output.strip():
            reachable += 1
            # The address every client will be handed. Worth printing: after a
            # failover this is the *other* container, and seeing that is the
            # clearest proof the failover really happened.
            lines = output.splitlines()
            addr = ""
            for i, line in enumerate(lines):
                if line.strip() == "ip" and i + 1 < len(lines):
                    addr = lines[i + 1].strip()
                    break
            print(f"  [OK]   {name} -> primary is {addr or '(unknown)'}")
        else:
            print(f"  [FAIL] {name} did not answer")

    decision = rules.check_sentinel_quorum(len(SENTINELS), rules.DEFAULT_QUORUM,
                                           reachable)
    print(f"\n  {'[OK]  ' if decision.ok else '[FAIL]'} {decision.detail}")
    if not decision.ok:
        failures.append(decision.detail)

    print("\n--- RESULT ---")
    if failures:
        print(f"[FAIL] {len(failures)} problem(s):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] primary has a linked replica and the sentinels can agree to "
          "fail over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
