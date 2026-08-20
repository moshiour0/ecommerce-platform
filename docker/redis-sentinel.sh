#!/bin/sh
# Entrypoint for a Redis Sentinel container.
#
# Sentinel rewrites its own configuration file at runtime -- it records the
# replicas it discovers, the other sentinels it meets, and the current primary
# after a failover. A read-only bind mount therefore does not work: sentinel
# exits with "Sentinel config file ... is not writable". So the config is
# generated here, into a writable path, from environment variables.
#
# The primary is monitored by ADDRESS, resolved once here at startup, not by
# hostname. That is not a stylistic choice -- it is the difference between a
# failover and an outage, and it cost a full test run to find:
#
#   sentinel-1 | # Failed to resolve hostname 'redis'
#   sentinel-1 | # +tilt #tilt mode entered
#
# With `resolve-hostnames yes`, sentinel re-resolves the monitored name while
# checking on it. Docker's embedded DNS deletes a container's record the
# instant the container dies, so the very event sentinel exists to react to is
# the event that makes the name unresolvable. The failing lookup blocks
# sentinel's event loop long enough to trip its TILT watchdog, and a sentinel
# in TILT mode does not perform failovers. The result looks exactly like a
# working setup right up until it is needed: three healthy sentinels, a linked
# replica, and no promotion.
#
# Resolving here means sentinel holds an address that does not stop existing
# when the container does. Sentinel then tracks the topology by address from
# then on -- it learns the replica from the primary's INFO output, and after a
# promotion it follows the new primary's address on its own.
set -eu

MASTER_NAME="${REDIS_MASTER_NAME:-mymaster}"
MASTER_HOST="${REDIS_MASTER_HOST:-redis}"
MASTER_PORT="${REDIS_MASTER_PORT:-6379}"

# Two of three. See shared/libs/python-common/redis_topology.py for why a
# quorum of one is worse than no failover at all.
QUORUM="${REDIS_SENTINEL_QUORUM:-2}"

# How long the primary must be unreachable before this sentinel calls it down.
# Short enough that a real failure is not a long outage, long enough that a GC
# pause or a slow disk flush is not mistaken for one.
DOWN_AFTER_MS="${REDIS_DOWN_AFTER_MS:-5000}"

# The ceiling on one failover attempt before another sentinel may try.
FAILOVER_TIMEOUT_MS="${REDIS_FAILOVER_TIMEOUT_MS:-10000}"

CONF=/tmp/sentinel.conf

# Resolve once, now, while the primary is definitely up. Waiting rather than
# failing: on a cold start compose may have this container running before the
# primary has an address at all.
MASTER_ADDR=""
attempt=0
while [ "$attempt" -lt 60 ]; do
  MASTER_ADDR=$(getent hosts "$MASTER_HOST" | awk '{ print $1 }' | head -n 1)
  if [ -n "$MASTER_ADDR" ]; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ -z "$MASTER_ADDR" ]; then
  echo "sentinel: could not resolve ${MASTER_HOST} after 60s; refusing to" >&2
  echo "sentinel: start, because monitoring an unresolvable name is the same" >&2
  echo "sentinel: as not monitoring anything." >&2
  exit 1
fi

cat > "$CONF" <<EOF
port 26379
dir /tmp

sentinel monitor ${MASTER_NAME} ${MASTER_ADDR} ${MASTER_PORT} ${QUORUM}
sentinel down-after-milliseconds ${MASTER_NAME} ${DOWN_AFTER_MS}
sentinel failover-timeout ${MASTER_NAME} ${FAILOVER_TIMEOUT_MS}

# One replica resyncs at a time. With a single replica this changes nothing;
# with several it stops a failover from taking every replica offline at once
# to resync, which would leave the new primary alone and unreplicated.
sentinel parallel-syncs ${MASTER_NAME} 1
EOF

echo "sentinel: monitoring ${MASTER_NAME} at ${MASTER_ADDR}:${MASTER_PORT} (${MASTER_HOST}), quorum ${QUORUM}"
exec redis-sentinel "$CONF"
