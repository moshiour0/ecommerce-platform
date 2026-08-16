'use strict';

// Pure decisions behind the tiered rate limiter.
//
// Deliberately free of express, redis and process.env: ratelimit.js throws at
// import time when REDIS_URL is missing and opens a Redis connection eagerly,
// so nothing defined there can be unit tested without infrastructure. Same
// split as reservation_rules.py / charge_rules.py on the Python side -- the
// decision is pinned by unit tests (test/ratelimit_rules.test.js), the wiring
// is proven under real parallel load by tests/integration/rate_limit_burst.sh.

// One window for every tier. Budgets are per window, per key.
//
// Read is generous because browsing is the dominant traffic. Write is tight
// because those paths start sagas, take pessimistic row locks and move money.
// Admin sits between: privileged, but a human clicking, not a crawler.
const WINDOW_MS = 60 * 1000;
const BUDGETS = { read: 200, write: 20, admin: 50 };

// Paths that must never be throttled, at any tier.
//
// /health is what BOTH the Kubernetes readiness and liveness probes call
// (infrastructure/k8s/services/api-gateway.yaml). While it sat behind the
// limiter, a flood from one IP exhausted the shared read budget, /health began
// answering 429, and kubelet read that as a failed liveness probe and
// restarted the gateway. The limiter converted a flood into an outage instead
// of absorbing one. Exempting the probe path is what makes it survivable.
const EXEMPT_PATHS = ['/health'];

/**
 * True when a path must bypass rate limiting entirely.
 */
function isExempt(path) {
  const p = normalize(path);
  return EXEMPT_PATHS.some((exempt) => p === exempt || p.startsWith(exempt + '/'));
}

/**
 * Which tier a request belongs to: 'read' | 'write' | 'admin'.
 *
 * Admin is tested FIRST. The previous ordering tested write first, so a
 * hypothetical /admin/checkout would have been billed to the write tier and
 * never keyed per-user. No such route exists today -- the gateway proxies only
 * /api/shop and /api/checkout, and neither BFF serves /admin -- so this fixes
 * the precedence while it is still free to fix.
 */
function classify(path, method) {
  const p = normalize(path);
  const verb = String(method || '').toUpperCase();

  if (p.includes('/admin')) return 'admin';

  // Write-heavy: starts a saga, mutates a cart, or touches payments. Cart and
  // order reads stay on the read tier, which is why the method is part of the
  // test rather than the path alone.
  if (
    p.includes('/checkout') ||
    (p.includes('/cart') && verb !== 'GET') ||
    (p.includes('/orders') && verb === 'POST') ||
    p.includes('/payments')
  ) {
    return 'write';
  }

  return 'read';
}

/**
 * Path a limiter should key on, independent of where the middleware is mounted.
 *
 * req.path is relative to the mount point, so the same request reads as
 * '/api/shop/admin/x' at the root and '/shop/admin/x' under app.use('/api').
 * Classifying the second would still work by luck here, but only because every
 * marker is a substring; keying on originalUrl removes the luck. The query
 * string is dropped so ?q= cannot change a request's tier.
 */
function requestPath(req) {
  const url = req.originalUrl || req.url || req.path || '/';
  return url.split('?')[0];
}

/**
 * Parse the TRUST_PROXY environment variable into an express `trust proxy`
 * value.
 *
 * express derives req.ip -- the limiter's key -- from this setting, so a wrong
 * value silently breaks limiting in one of two directions:
 *
 *   too low   every client behind a proxy presents the proxy's IP, so the
 *             entire platform shares a single 200/min bucket
 *   too high  X-Forwarded-For is attacker-controlled, so a client spoofs a
 *             fresh IP per request and the limiter never fires at all
 *
 * Default false is correct for the deployment as it stands: all 20 services are
 * ClusterIP, there is no Ingress manifest, and nothing sits between a client
 * and the gateway. Set TRUST_PROXY to the NUMBER OF PROXY HOPS the day that
 * changes -- a hop count is the only form that is not spoofable.
 */
function parseTrustProxy(raw) {
  if (raw === undefined || raw === null || String(raw).trim() === '') return false;
  const value = String(raw).trim().toLowerCase();

  if (value === 'false' || value === 'off' || value === '0') return false;
  if (value === 'true' || value === 'on') return true;

  if (/^\d+$/.test(value)) return Number(value);

  // Anything else is handed to express verbatim: named presets such as
  // 'loopback' or 'uniquelocal', or an explicit subnet list.
  return String(raw).trim();
}

/**
 * True for a trust-proxy setting that lets a client forge its own limiter key.
 *
 * `true` trusts the leftmost X-Forwarded-For entry no matter who wrote it, so
 * a client sends a different value each request and gets an unlimited budget.
 * Callers log this loudly rather than refusing, because it is legitimate in a
 * mesh where the gateway is unreachable except through a trusted proxy.
 */
function isSpoofableTrustProxy(value) {
  return value === true;
}

function normalize(path) {
  return String(path == null ? '' : path).toLowerCase();
}

module.exports = {
  WINDOW_MS,
  BUDGETS,
  EXEMPT_PATHS,
  isExempt,
  classify,
  requestPath,
  parseTrustProxy,
  isSpoofableTrustProxy,
};
