#!/bin/sh
# Burst check for the api-gateway tiered rate limiter.
#
# Unit tests pin which tier a path lands in and which paths are exempt; they
# cannot prove that a real Redis-backed counter refuses the 201st request, nor
# that the counter stays correct when the requests arrive genuinely in
# parallel. rate-limit-redis increments with INCR, which is atomic -- but that
# is exactly the kind of claim the inventory contention run disproved for a
# different mechanism, so it gets asserted rather than assumed.
#
# Two properties, in order:
#
#   1. The limiter engages and never over-serves. More than BUDGET requests
#      admitted in one window means concurrent requests read the same counter
#      value and both proceeded -- the rate-limiting equivalent of an oversell.
#
#   2. /health survives an exhausted budget. This is a regression test for a
#      real defect: the limiter was mounted ahead of the health route, so a
#      flood from one IP drove /health to 429. In Kubernetes both the readiness
#      and liveness probes call that path, and kubelet answers a 429 liveness
#      probe by restarting the pod -- the limiter turning a flood into an
#      outage. Health is checked AFTER the burst on purpose; checking it first
#      would pass even with the defect present.
#
# The probe path is deliberately one the gateway does not proxy, so it stops at
# the gateway's own 404 handler and the burst puts no load on the BFFs or any
# downstream service. 404 here means "the limiter let it through", which is the
# only thing this test is asking about.
#
# Runs inside the mesh, like the other integration checks:
#
#   docker run --rm --network ecommerce-platform_mesh \
#     -v "$PWD/tests/integration:/s:ro" curlimages/curl:8.5.0 \
#     sh /s/rate_limit_burst.sh
#
#   kubectl -n ecommerce create configmap rlcm \
#     --from-file=rate_limit_burst.sh=tests/integration/rate_limit_burst.sh
#   kubectl -n ecommerce run rlcheck --image=curlimages/curl:8.5.0 \
#     --restart=Never --rm -i --quiet --overrides='{"spec":{
#       "enableServiceLinks":false,
#       "containers":[{"name":"rlcheck","image":"curlimages/curl:8.5.0",
#         "command":["sh","/s/rate_limit_burst.sh"],
#         "volumeMounts":[{"name":"s","mountPath":"/s"}]}],
#       "volumes":[{"name":"s","configMap":{"name":"rlcm"}}]}}'
#
# Exit 0 only when the limiter capped the burst and /health stayed up.

GATEWAY=${GATEWAY_URL:-http://api-gateway:8000}
BUDGET=${BUDGET:-200}          # read tier, must match BUDGETS.read in ratelimit_rules.js
REQUESTS=${REQUESTS:-250}
PROBE=${PROBE_PATH:-/rl-probe}
CODES=/tmp/rl_codes
rm -f "$CODES"

echo "gateway=$GATEWAY  budget=$BUDGET  requests=$REQUESTS  probe=$PROBE"

# The limiter keys on the caller's IP, and this container has its own address
# on the mesh network, so each run starts from a fresh counter. If an address
# is recycled inside the 60s window the leftover count only makes FEWER
# requests pass, never more -- which is why the assertion below is "at most
# BUDGET" rather than "exactly BUDGET". Over-serving is the failure; being
# stricter than advertised is not.
i=0
while [ "$i" -lt "$REQUESTS" ]; do
  curl -s -o /dev/null -w "%{http_code}\n" "$GATEWAY$PROBE" >> "$CODES" &
  i=$((i + 1))
done
wait

# grep -c exits non-zero on zero matches, so each count goes through wc to stay
# a plain number whether or not anything matched.
LIMITED=$(grep '^429$' "$CODES" | wc -l | tr -d ' ')
ADMITTED=$(grep '^404$' "$CODES" | wc -l | tr -d ' ')
OTHER=$(grep -vE '^(404|429)$' "$CODES" | wc -l | tr -d ' ')

echo "admitted (404 from gateway): $ADMITTED"
echo "limited  (429):              $LIMITED"
echo "other:                       $OTHER"

if [ "$OTHER" -gt 0 ]; then
  echo "unexpected statuses:"
  grep -vE '^(404|429)$' "$CODES" | sort | uniq -c
  # 000 is curl's code for a connection that never completed, which under load
  # usually means the gateway fell over rather than shedding traffic. A limiter
  # that crashes instead of refusing is worse than no limiter.
  echo "RESULT: FAIL - the gateway returned something other than 404/429 under load"
  exit 1
fi

if [ "$ADMITTED" -gt "$BUDGET" ]; then
  echo "RESULT: FAIL - OVER-SERVED by $((ADMITTED - BUDGET)) requests (budget $BUDGET)"
  exit 1
fi

if [ "$LIMITED" -eq 0 ]; then
  echo "RESULT: FAIL - limiter never engaged; $REQUESTS requests all admitted"
  exit 1
fi

echo "  burst OK: capped at $ADMITTED admitted, $LIMITED refused"

# ---------------------------------------------------------------------------
# The budget for this IP is now exhausted. Probes must still be answered.
# ---------------------------------------------------------------------------
HEALTH_FAIL=0
j=0
while [ "$j" -lt 5 ]; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$GATEWAY/health")
  if [ "$CODE" != "200" ]; then
    echo "  /health returned $CODE with the budget exhausted"
    HEALTH_FAIL=$((HEALTH_FAIL + 1))
  fi
  j=$((j + 1))
done

if [ "$HEALTH_FAIL" -gt 0 ]; then
  echo "RESULT: FAIL - /health is behind the rate limiter."
  echo "        In Kubernetes this is a liveness probe failure during a flood,"
  echo "        so the gateway restarts exactly when it is under attack."
  exit 1
fi

echo "  health OK: /health answered 200 five times with the budget exhausted"
echo "RESULT: PASS - burst capped at $BUDGET and /health stayed up"
exit 0
