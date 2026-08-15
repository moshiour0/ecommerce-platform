'use strict';

/**
 * Circuit breaker + bulkhead for synchronous inter-service calls.
 *
 * Architecture Rule 11: every synchronous inter-service call must be wrapped
 * in a circuit breaker that opens after 5 consecutive failures, stays open for
 * 30 seconds, then allows a single half-open probe. Bulkhead isolation must
 * stop a failing downstream from starving the caller's pool for other routes.
 *
 * Wrapping happens at the client factory, not the call site, so routes keep
 * calling `catalogClient.get(...)` unchanged and no future call can quietly
 * skip the breaker.
 */

const axios = require('axios');
const CircuitBreaker = require('opossum');

const DEFAULT_TIMEOUT_MS = 3000;

// Rule 11 specifies *consecutive* failures. opossum is a percentage-over-a-
// window breaker and has no consecutive mode, so the count is tracked here
// and the circuit is opened explicitly; opossum still owns the state machine
// (open -> half-open single probe -> closed) and the reset timer.
//
// An earlier attempt mapped this to volumeThreshold 5 + errorThresholdPercentage
// 100. That never trips: opossum opens when the error rate *exceeds* the
// threshold, and nothing exceeds 100. Verified against a stopped downstream —
// eight consecutive failures left the circuit closed and every request paid
// the full 3s timeout.
const FAILURE_THRESHOLD = 5;

const BREAKER_DEFAULTS = {
  timeout: DEFAULT_TIMEOUT_MS,
  resetTimeout: 30000,        // stay open 30s, then a single half-open probe
  // Disable opossum's own percentage trigger so the two cannot disagree.
  errorThresholdPercentage: 100,
  volumeThreshold: Number.MAX_SAFE_INTEGER,
  rollingCountTimeout: 30000,
  rollingCountBuckets: 6
};

// Concurrent in-flight requests allowed per downstream. A slow dependency can
// then consume at most this many sockets, leaving the rest of the pool free
// for routes that do not touch it.
const DEFAULT_BULKHEAD = 20;

class BulkheadFullError extends Error {
  constructor(name, limit) {
    super(`Bulkhead full for ${name} (limit ${limit})`);
    this.name = 'BulkheadFullError';
    this.bulkhead = name;
    this.statusCode = 503;
  }
}

/**
 * Counting semaphore. Rejects immediately when saturated rather than queueing
 * — an unbounded queue just relocates the starvation it was meant to prevent.
 */
function createBulkhead(name, limit) {
  let inFlight = 0;
  return async function run(fn) {
    if (inFlight >= limit) throw new BulkheadFullError(name, limit);
    inFlight += 1;
    try {
      return await fn();
    } finally {
      inFlight -= 1;
    }
  };
}

/**
 * A 4xx is the downstream working correctly and rejecting our request. Only
 * transport failures, timeouts and 5xx indicate the dependency is unhealthy,
 * so only those may open the circuit. Without this filter a burst of 404s
 * would trip the breaker and take out a healthy service.
 */
function isDownstreamFailure(err) {
  const status = err && err.response && err.response.status;
  if (typeof status === 'number') return status >= 500;
  return true; // no response: timeout, DNS, connection refused
}

/**
 * Build an axios instance whose verb methods run inside a bulkhead and a
 * circuit breaker. Returns an object exposing the same call surface routes
 * already use, so existing code needs no changes.
 */
function createResilientClient({ name, baseURL, timeout = DEFAULT_TIMEOUT_MS, bulkhead = DEFAULT_BULKHEAD, logger = console, breakerOptions = {} }) {
  if (!name) throw new Error('createResilientClient requires a name');
  if (!baseURL) throw new Error(`createResilientClient(${name}) requires a baseURL`);

  const instance = axios.create({ baseURL, timeout });
  const limiter = createBulkhead(name, bulkhead);

  const breaker = new CircuitBreaker(
    (config) => instance.request(config),
    {
      ...BREAKER_DEFAULTS,
      timeout,
      name,
      // errorFilter is a constructor option, not a method. Returning true
      // means "do not count this as a circuit failure".
      errorFilter: (err) => !isDownstreamFailure(err),
      ...breakerOptions
    }
  );

  const resetMs = breakerOptions.resetTimeout || BREAKER_DEFAULTS.resetTimeout;
  let consecutiveFailures = 0;
  let probing = false;   // true between a half-open event and its outcome

  breaker.on('open', () => {
    consecutiveFailures = 0;
    logger.error(`Circuit OPEN for ${name} — failing fast for ${resetMs / 1000}s`);
  });
  breaker.on('halfOpen', () => {
    probing = true;
    logger.warn(`Circuit HALF-OPEN for ${name} — probing with a single request`);
  });
  breaker.on('close', () => {
    consecutiveFailures = 0;
    probing = false;
    logger.info(`Circuit CLOSED for ${name} — downstream healthy again`);
  });

  breaker.on('success', () => {
    consecutiveFailures = 0;
    probing = false;
  });

  breaker.on('failure', (err) => {
    // A 4xx means the downstream answered correctly; it must not open anything.
    if (!isDownstreamFailure(err)) return;
    if (probing) {
      // The half-open probe failed: straight back to open, no need to
      // re-accumulate five failures while the dependency is still down.
      probing = false;
      if (!breaker.opened) breaker.open();
      return;
    }
    consecutiveFailures += 1;
    if (consecutiveFailures >= FAILURE_THRESHOLD && !breaker.opened) {
      logger.error(`${name}: ${consecutiveFailures} consecutive failures — opening circuit`);
      breaker.open();
    }
  });

  const call = (config) => limiter(() => breaker.fire(config));

  return {
    name,
    breaker,
    request: (config) => call(config),
    get: (url, config = {}) => call({ ...config, method: 'get', url }),
    delete: (url, config = {}) => call({ ...config, method: 'delete', url }),
    post: (url, data, config = {}) => call({ ...config, method: 'post', url, data }),
    put: (url, data, config = {}) => call({ ...config, method: 'put', url, data }),
    patch: (url, data, config = {}) => call({ ...config, method: 'patch', url, data }),
    // Exposed for /health so operators can see which dependencies are tripped.
    stats: () => ({
      name,
      open: breaker.opened,
      halfOpen: breaker.halfOpen,
      stats: breaker.stats
    })
  };
}

module.exports = { createResilientClient, BulkheadFullError, isDownstreamFailure };
