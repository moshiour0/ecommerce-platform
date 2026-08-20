'use strict';

// Builds the ioredis client the Node services share.
//
// ioredis rather than node-redis, for both of them. node-redis v4 -- what
// api-gateway was on -- has no Sentinel support at all, and websocket-gateway
// was already on ioredis, so standardising here removes a Redis client rather
// than adding one. rate-limit-redis does not care which: it takes a
// sendCommand function.

const Redis = require('ioredis');
const { clientOptions } = require('./redis_rules');

/**
 * A client that follows the primary, wherever the sentinels say it is.
 *
 * @param {object} env    usually process.env
 * @param {object} extra  merged over the derived options
 */
function createRedisClient(env, extra = {}) {
  const options = clientOptions(env);

  if (options.mode === 'direct') {
    return new Redis(options.url, extra);
  }

  const { mode, ...sentinelOptions } = options;
  return new Redis({ ...sentinelOptions, ...extra });
}

/**
 * How to describe this connection in a log line, without leaking a password
 * that may be embedded in REDIS_URL.
 */
function describeConnection(env) {
  const options = clientOptions(env);
  if (options.mode === 'direct') {
    // Strip credentials: redis://user:pass@host:6379 must not reach a log.
    return `direct ${String(options.url).replace(/\/\/[^@]*@/, '//')}`;
  }
  const hosts = options.sentinels.map((s) => `${s.host}:${s.port}`).join(',');
  return `sentinel name=${options.name} via ${hosts}`;
}

module.exports = { createRedisClient, describeConnection };
