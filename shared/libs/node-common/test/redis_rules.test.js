'use strict';

// The Node half of the Redis topology rules. The Python half is
// tests/unit/test_redis_topology.py, and the two must agree -- particularly
// that a malformed sentinel entry raises rather than being skipped. If one
// side skipped, a typo would shrink the quorum on that side only, and no
// error would appear anywhere until a failover was needed and could not be
// agreed.

const test = require('node:test');
const assert = require('node:assert');

const {
  DEFAULT_MASTER_NAME,
  parseSentinelHosts,
  connectionMode,
  clientOptions
} = require('../redis_rules');

test('a sentinel list parses with whitespace and a trailing comma', () => {
  assert.deepStrictEqual(
    parseSentinelHosts(' a:26379, b:26379 , c:26379, '),
    [{ host: 'a', port: 26379 },
     { host: 'b', port: 26379 },
     { host: 'c', port: 26379 }]
  );
});

test('an empty list is no hosts rather than an error', () => {
  assert.deepStrictEqual(parseSentinelHosts(''), []);
  assert.deepStrictEqual(parseSentinelHosts(undefined), []);
});

test('a malformed entry raises instead of being skipped', () => {
  // Matches the Python side. Skipping would leave two sentinels with a quorum
  // of two, unable to survive losing either.
  assert.throws(
    () => parseSentinelHosts('redis-sentinel-1:26379,redis-sentinel-2'),
    /shrinks the quorum/
  );
});

test('a non-numeric port raises', () => {
  assert.throws(() => parseSentinelHosts('a:twentysix'), /non-numeric port/);
});

test('a sentinel list wins over a direct url', () => {
  // REDIS_URL is in every service environment already. If it won, adding
  // sentinels would change nothing until a failover left the service talking
  // to a demoted host.
  assert.strictEqual(
    connectionMode('redis://redis:6379', 'redis-sentinel-1:26379'),
    'sentinel'
  );
});

test('a url alone is direct mode', () => {
  assert.strictEqual(connectionMode('redis://redis:6379', ''), 'direct');
  assert.strictEqual(connectionMode('redis://redis:6379', '   '), 'direct');
});

test('neither configured is refused rather than defaulted', () => {
  // Rule 8: no default endpoint for shared infrastructure.
  assert.throws(() => connectionMode('', ''), /Rule 8/);
});

test('sentinel options carry the master name and never a role', () => {
  const options = clientOptions({
    REDIS_URL: 'redis://redis:6379',
    REDIS_SENTINELS: 'redis-sentinel-1:26379,redis-sentinel-2:26379'
  });
  assert.strictEqual(options.mode, 'sentinel');
  assert.strictEqual(options.name, DEFAULT_MASTER_NAME);
  assert.strictEqual(options.sentinels.length, 2);
  // No `role: 'slave'`: reads must go to the primary, or a caller can be
  // served a rate-limit counter one increment behind their real usage.
  assert.strictEqual(options.role, undefined);
});

test('an explicit master name overrides the default', () => {
  const options = clientOptions({
    REDIS_SENTINELS: 'a:26379',
    REDIS_MASTER_NAME: 'carts'
  });
  assert.strictEqual(options.name, 'carts');
});

test('commands issued during a failover queue rather than fail', () => {
  const options = clientOptions({ REDIS_SENTINELS: 'a:26379' });
  assert.strictEqual(options.enableOfflineQueue, true);
  assert.ok(options.maxRetriesPerRequest >= 1);
});
