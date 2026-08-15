'use strict';

/**
 * OpenTelemetry bootstrap for the Node edge (Architecture Rule 6).
 *
 * Rule 6.3 defines the trace path as
 *   Client -> WAF -> Gateway -> BFF -> Service -> Outbox -> Kafka -> Consumer
 * The Python half of that chain is traced. Without this file the Node half is
 * not, so a checkout begins its trace at order-saga and the edge — where
 * latency and 4xx/5xx actually surface to users — is invisible.
 *
 * LOAD ORDER MATTERS. OpenTelemetry patches `http` and `express` by
 * intercepting `require`, so it must run before either is loaded. Importing it
 * from inside index.js is fragile: any require that resolves earlier escapes
 * instrumentation silently, and the failure looks like "some spans are
 * missing" rather than an error. It is therefore loaded via
 * `NODE_OPTIONS=--require node-common/tracing`, which runs before the entry
 * module exists.
 *
 * Failure here must never stop a service booting. Observability going dark is
 * an incident; a storefront that will not start because a collector is
 * unreachable is a worse one.
 */

const SERVICE_NAME = process.env.OTEL_SERVICE_NAME || process.env.HOSTNAME || 'node-service';
const ENDPOINT = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;

if (!ENDPOINT) {
  console.log(`[tracing] OTEL_EXPORTER_OTLP_ENDPOINT unset — ${SERVICE_NAME} runs untraced`);
} else {
  try {
    const { NodeSDK } = require('@opentelemetry/sdk-node');
    const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
    const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
    const { Resource } = require('@opentelemetry/resources');
    const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');

    const sdk = new NodeSDK({
      resource: new Resource({
        [SemanticResourceAttributes.SERVICE_NAME]: SERVICE_NAME,
      }),
      traceExporter: new OTLPTraceExporter({
        url: `${ENDPOINT.replace(/\/$/, '')}/v1/traces`,
      }),
      instrumentations: [
        getNodeAutoInstrumentations({
          // Filesystem spans drown out request spans and carry no signal here.
          '@opentelemetry/instrumentation-fs': { enabled: false },
          '@opentelemetry/instrumentation-express': {
            // Express emits a span per middleware, so a single checkout
            // produced spans for query parsing, expressInit, cors and the JSON
            // body parser before any real work appeared. That is noise in
            // every trace and it buries the downstream calls that matter.
            // Route handlers and routers are kept; generic middleware is not.
            ignoreLayersType: ['middleware'],
          },
          '@opentelemetry/instrumentation-http': {
            // Health probes run every few seconds per container and would
            // otherwise dominate the trace store.
            ignoreIncomingRequestHook: (req) =>
              req.url === '/health' || req.url === '/metrics',
          },
          // tcp.connect spans add a layer per outbound call without saying
          // anything the HTTP span does not already carry.
          '@opentelemetry/instrumentation-net': { enabled: false },
          '@opentelemetry/instrumentation-dns': { enabled: false },
        }),
      ],
    });

    sdk.start();
    console.log(`[tracing] configured for ${SERVICE_NAME} -> ${ENDPOINT}`);

    const shutdown = () => {
      sdk.shutdown()
        .catch((err) => console.error('[tracing] shutdown error', err))
        .finally(() => process.exit(0));
    };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
  } catch (err) {
    console.error(`[tracing] setup failed (${err.message}) — ${SERVICE_NAME} runs untraced`);
  }
}
