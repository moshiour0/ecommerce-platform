#!/bin/sh
# Concurrency check for the cart checkout mutex.
#
# Unit tests pin the lock's ownership semantics against a fake clock; they
# cannot prove mutual exclusion under real contention. This fires N genuinely
# parallel checkouts at one cart and asserts exactly one is accepted.
#
# Each request carries a distinct Idempotency-Key, so the idempotency guard
# cannot be what serialises them. Only the mutex can.
#
# Runs inside the cluster so it exercises real service-to-service networking
# rather than a port-forward:
#
#   kubectl -n ecommerce create configmap mutexcm \
#     --from-file=checkout_mutex.sh=tests/integration/checkout_mutex.sh
#   kubectl -n ecommerce run mutexcheck --image=curlimages/curl:8.5.0 \
#     --restart=Never --rm -i --quiet --overrides='{"spec":{
#       "enableServiceLinks":false,
#       "containers":[{"name":"mutexcheck","image":"curlimages/curl:8.5.0",
#         "command":["sh","/s/checkout_mutex.sh"],
#         "volumeMounts":[{"name":"s","mountPath":"/s"}]}],
#       "volumes":[{"name":"s","configMap":{"name":"mutexcm"}}]}}'
#
# Exit 0 only when exactly one checkout wins.

CART=${CART_URL:-http://cart-service:8007/cart}
N=${CONCURRENCY:-10}
CODES=/tmp/codes
rm -f "$CODES"

USER_ID=$(cat /proc/sys/kernel/random/uuid)
PRODUCT=$(cat /proc/sys/kernel/random/uuid)
echo "user=$USER_ID  concurrency=$N"

SEED=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$CART/$USER_ID/items" -H "Content-Type: application/json" -H "Idempotency-Key: $(cat /proc/sys/kernel/random/uuid)" -d "{\"item\":{\"product_id\":\"$PRODUCT\",\"quantity\":1}}")
echo "seed cart: HTTP $SEED"
if [ "$SEED" != "200" ] && [ "$SEED" != "201" ]; then
  echo "RESULT: FAIL (could not seed cart)"
  exit 1
fi

i=0
while [ "$i" -lt "$N" ]; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$CART/$USER_ID/checkout" -H "Content-Type: application/json" -H "Idempotency-Key: $(cat /proc/sys/kernel/random/uuid)" >> "$CODES" &
  i=$((i + 1))
done
wait

# Accepted: the endpoint answers 202, not 200 -- checkout is initiated
# asynchronously and the saga finishes later. Match any 2xx so the assertion
# stays about mutual exclusion rather than a specific success code.
#
# Rejected: BOTH 409 and 404 are correct rejections, and which one a loser
# gets is pure timing.
#   409 -- the mutex was still held when this request arrived
#   404 -- the winner had already committed and deleted the cart from Redis,
#          so this request took the lock and found nothing to check out
# Treating 404 as a failure made this test flake on roughly half its runs
# while mutual exclusion was in fact never violated.
#
# grep -c exits non-zero on zero matches, so piping through wc keeps each
# count a plain number regardless of whether anything matched.
OK=$(grep '^2..$' "$CODES" | wc -l | tr -d ' ')
REJECTED=$(grep -E '^(409|404)$' "$CODES" | wc -l | tr -d ' ')
OTHER=$(grep -vE '^(2..|409|404)$' "$CODES" | wc -l | tr -d ' ')

echo "accepted (2xx):      $OK"
echo "rejected (409/404):  $REJECTED"
echo "other:               $OTHER"

if [ "$OTHER" -gt 0 ]; then
  echo "unexpected statuses:"
  grep -vE '^(2..|409|404)$' "$CODES" | sort | uniq -c
fi

# Exactly one winner is the whole point. Two means the mutex failed and one
# cart was checked out twice; zero means it locked itself out. A 5xx anywhere
# means something broke rather than being correctly refused.
if [ "$OK" -eq 1 ] && [ "$OTHER" -eq 0 ]; then
  echo "RESULT: PASS - exactly one checkout accepted out of $N"
  exit 0
fi

echo "RESULT: FAIL - expected exactly 1 accepted and 0 unexpected statuses"
exit 1
