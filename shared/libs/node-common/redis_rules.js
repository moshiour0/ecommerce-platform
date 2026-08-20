'use strict';

// Where Redis is, decided from the environment. Pure functions only.
//
// The Python side of this lives in
// shared/libs/python-common/redis_topology.py and must agree with it. The
// agreement that matters is not the happy path -- it is that a malformed
// sentinel entry *raises* on both sides rather than being skipped. A list that
// silently drops one of three sentinels leaves a quorum of two out of two,
// which cannot survive losing either, and nothing anywhere reports the change.

const DEFAULT_MASTER_NAME = 'mymaster';

/**
 * Parse "host:port,host:port" into [{ host, port }].
 * Throws on a malformed entry rather than skipping it.
 */
function parseSentinelHosts(value) {
  if (!value || !value.trim()) return [];

  const hosts = [];
  for (const raw of value.split(',')) {
    const chunk = raw.trim();
    if (!chunk) continue;

    const idx = chunk.lastIndexOf(':');
    if (idx <= 0) {
      throw new Error(
        `sentinel entry "${chunk}" is not host:port; a sentinel list with a ` +
        'typo silently shrinks the quorum'
      );
    }
    const host = chunk.slice(0, idx);
    const port = Number(chunk.slice(idx + 1));
    if (!Number.isInteger(port)) {
      throw new Error(`sentinel entry "${chunk}" has a non-numeric port`);
    }
    hosts.push({ host, port });
  }
  return hosts;
}

/**
 * 'sentinel' or 'direct'.
 *
 * Sentinel wins when both are set. REDIS_URL is already in every service's
 * environment, so letting it win would mean a service quietly ignoring its
 * sentinel list and connecting straight to a host about to be demoted.
 */
function connectionMode(redisUrl, sentinelHosts) {
  if (parseSentinelHosts(sentinelHosts).length > 0) return 'sentinel';
  if (redisUrl && redisUrl.trim()) return 'direct';
  throw new Error(
    'neither REDIS_SENTINELS nor REDIS_URL is set. Rule 8: no default ' +
    'endpoint for shared infrastructure.'
  );
}

/**
 * The options object for ioredis, from the environment.
 *
 * `role` is deliberately absent, which means ioredis connects to the master.
 * Replication is asynchronous, so a replica read can serve a cart that was
 * just emptied or a rate-limit counter one increment behind -- the second is a
 * caller exceeding their budget, which is a bypass rather than a nuisance.
 * The replica exists to be promoted, not to be read.
 */
function clientOptions(env) {
  const mode = connectionMode(env.REDIS_URL, env.REDIS_SENTINELS);

  if (mode === 'direct') {
    return { mode, url: env.REDIS_URL };
  }

  return {
    mode,
    sentinels: parseSentinelHosts(env.REDIS_SENTINELS),
    name: env.REDIS_MASTER_NAME || DEFAULT_MASTER_NAME,
    // A failover takes a few seconds: the sentinels agree the primary is
    // down, elect a leader among themselves, promote the replica, then tell
    // clients. Commands issued in that window must wait rather than fail.
    enableOfflineQueue: true,
    maxRetriesPerRequest: 5
  };
}

module.exports = {
  DEFAULT_MASTER_NAME,
  parseSentinelHosts,
  connectionMode,
  clientOptions
};
