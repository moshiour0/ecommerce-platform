'use strict';

// Unit tests for the rate limiter's decisions.
//
// Run with:  npm test          (from services/api-gateway)
//
// That is a bare `node --test`, with no path argument, because the ways of
// naming test files are not portable across the versions this repo runs on:
// `node --test "test/**/*.test.js"` fails on Node 18 ("Could not find"), which
// is what the service image and CI use, while `node --test test/` fails on
// Node 24 on Windows, which is where it gets run by hand. Bare --test discovers
// them on both, and skips node_modules by default.
//
// Uses node:test, which is built into Node 18+, so this adds no dependency to a
// service image built with `npm install --production`. These tests pin the
// decision only; that a burst actually receives 429 from a real Redis-backed
// store is proven by tests/integration/rate_limit_burst.sh, the same way the
// checkout mutex is pinned by unit tests and proven under real contention.

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  BUDGETS,
  isExempt,
  classify,
  requestPath,
  parseTrustProxy,
  isSpoofableTrustProxy,
} = require('../src/middleware/ratelimit_rules');

// ---------------------------------------------------------------------------
// exemption — the defect that turned a flood into a restart loop
// ---------------------------------------------------------------------------

test('/health is exempt from rate limiting', () => {
  assert.equal(isExempt('/health'), true);
});

test('/health is exempt even though it classifies as a read path', () => {
  // The regression in miniature: classify() has no idea /health is special, so
  // without the exemption the probe path draws from the same 200/min budget as
  // browsing. Exhaust that and kubelet restarts the gateway mid-flood.
  assert.equal(classify('/health', 'GET'), 'read');
  assert.equal(isExempt('/health'), true);
});

test('exemption is case insensitive', () => {
  assert.equal(isExempt('/HEALTH'), true);
});

test('exemption matches path segments, not prefixes', () => {
  // /healthz belongs to nobody here; matching it would hand an attacker a free
  // uncounted endpoint just by appending a character.
  assert.equal(isExempt('/healthz'), false);
  assert.equal(isExempt('/health-check-admin'), false);
  assert.equal(isExempt('/api/shop/healthy'), false);
});

test('paths under /health stay exempt', () => {
  assert.equal(isExempt('/health/ready'), true);
});

test('ordinary traffic is not exempt', () => {
  assert.equal(isExempt('/api/shop/products'), false);
  assert.equal(isExempt('/'), false);
});

// ---------------------------------------------------------------------------
// tier classification
// ---------------------------------------------------------------------------

test('browsing is read tier', () => {
  assert.equal(classify('/api/shop/products', 'GET'), 'read');
  assert.equal(classify('/api/shop/search?q=shoes', 'GET'), 'read');
  assert.equal(classify('/unmatched', 'GET'), 'read');
});

test('checkout is write tier regardless of method', () => {
  assert.equal(classify('/api/checkout', 'POST'), 'write');
  assert.equal(classify('/api/checkout/confirm', 'GET'), 'write');
});

test('cart mutations are write tier but cart reads are not', () => {
  // The method is part of the test on purpose: browsing a cart is cheap,
  // mutating one takes the Redis mutex the checkout path depends on.
  assert.equal(classify('/api/shop/cart', 'POST'), 'write');
  assert.equal(classify('/api/shop/cart', 'DELETE'), 'write');
  assert.equal(classify('/api/shop/cart', 'GET'), 'read');
});

test('order creation is write tier but order history is not', () => {
  assert.equal(classify('/api/shop/orders', 'POST'), 'write');
  assert.equal(classify('/api/shop/orders', 'GET'), 'read');
});

test('payments are always write tier', () => {
  assert.equal(classify('/api/checkout/payments', 'GET'), 'write');
});

test('method is matched case insensitively', () => {
  assert.equal(classify('/api/shop/cart', 'post'), 'write');
});

test('a missing method degrades to the non-GET reading', () => {
  // Defensive: an absent method must not silently downgrade a cart mutation to
  // the generous read budget.
  assert.equal(classify('/api/shop/cart', undefined), 'write');
});

test('admin wins over write', () => {
  // Precedence deliberately changed: the previous ordering billed an
  // /admin/checkout to the write tier, so it would never have been keyed
  // per-user. No such route exists yet, which is what makes it safe to fix.
  assert.equal(classify('/api/admin/checkout', 'POST'), 'admin');
  assert.equal(classify('/api/admin/users', 'GET'), 'admin');
});

// ---------------------------------------------------------------------------
// budgets
// ---------------------------------------------------------------------------

test('write budget is tighter than read', () => {
  assert.ok(BUDGETS.write < BUDGETS.read);
});

test('every tier has a positive budget', () => {
  // A tier at 0 would reject every request; a missing tier would make
  // rateLimit() fall back to its own default and silently ignore the
  // architecture's numbers.
  for (const tier of ['read', 'write', 'admin']) {
    assert.equal(typeof BUDGETS[tier], 'number', `${tier} budget must be a number`);
    assert.ok(BUDGETS[tier] > 0, `${tier} budget must be positive`);
  }
});

// ---------------------------------------------------------------------------
// keying path — mount-point independence
// ---------------------------------------------------------------------------

test('requestPath uses originalUrl so the mount point cannot change the tier', () => {
  // Mounted at app.use('/api', ...), req.path is stripped to '/admin/users'
  // while originalUrl keeps the whole thing. Keying on originalUrl means the
  // perimeter limiter and the post-auth admin limiter classify identically.
  const req = { originalUrl: '/api/admin/users', url: '/admin/users', path: '/admin/users' };
  assert.equal(requestPath(req), '/api/admin/users');
  assert.equal(classify(requestPath(req), 'GET'), 'admin');
});

test('requestPath drops the query string', () => {
  // Otherwise ?next=/admin would let a caller talk their way into another tier.
  const req = { originalUrl: '/api/shop/products?q=/checkout' };
  assert.equal(requestPath(req), '/api/shop/products');
  assert.equal(classify(requestPath(req), 'GET'), 'read');
});

test('requestPath falls back when originalUrl is absent', () => {
  assert.equal(requestPath({ url: '/api/shop' }), '/api/shop');
  assert.equal(requestPath({}), '/');
});

// ---------------------------------------------------------------------------
// trust proxy — what req.ip, and therefore the limiter key, is derived from
// ---------------------------------------------------------------------------

test('trust proxy defaults to false when unset', () => {
  // Correct for the current topology: ClusterIP services, no Ingress, nothing
  // between client and gateway. Trusting a forwarded header here would let any
  // caller forge its own key.
  assert.equal(parseTrustProxy(undefined), false);
  assert.equal(parseTrustProxy(''), false);
  assert.equal(parseTrustProxy('   '), false);
});

test('trust proxy accepts explicit falsey spellings', () => {
  assert.equal(parseTrustProxy('false'), false);
  assert.equal(parseTrustProxy('off'), false);
  assert.equal(parseTrustProxy('0'), false);
  assert.equal(parseTrustProxy('FALSE'), false);
});

test('a hop count parses as a number, not a string', () => {
  // express treats the string '1' and the number 1 differently: the number
  // means "one proxy hop", which is the only non-spoofable form.
  assert.equal(parseTrustProxy('1'), 1);
  assert.equal(parseTrustProxy('2'), 2);
});

test('named presets pass through to express untouched', () => {
  assert.equal(parseTrustProxy('loopback'), 'loopback');
  assert.equal(parseTrustProxy('uniquelocal'), 'uniquelocal');
});

test('only literal true is flagged as spoofable', () => {
  // Trusting every X-Forwarded-For means a client sends a fresh IP per request
  // and never exhausts a budget. A hop count does not have that property.
  assert.equal(isSpoofableTrustProxy(parseTrustProxy('true')), true);
  assert.equal(isSpoofableTrustProxy(parseTrustProxy('on')), true);
  assert.equal(isSpoofableTrustProxy(parseTrustProxy('1')), false);
  assert.equal(isSpoofableTrustProxy(parseTrustProxy('false')), false);
  assert.equal(isSpoofableTrustProxy(parseTrustProxy('loopback')), false);
});
