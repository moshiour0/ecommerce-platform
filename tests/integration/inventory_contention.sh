#!/bin/sh
# Flash-sale contention check for inventory reservation.
#
# This is the scenario the original audit asked about: many buyers, few units.
# Unit tests pin the arithmetic; only real contention shows whether the
# pessimistic row lock actually serialises concurrent reservations, or whether
# two transactions can each read the same quantity_available and both proceed.
#
# STOCK units exist and BUYERS requests are fired in parallel, one unit each.
# Exactly STOCK must succeed. More means the platform oversold, which in
# production means unfulfillable orders and refunds.
#
# Requires the product row to exist already -- there is deliberately no
# auto-seed, so the caller passes PRODUCT_ID for a row it created.
#
#   kubectl -n ecommerce create configmap invcm \
#     --from-file=inventory_contention.sh=tests/integration/inventory_contention.sh
#   kubectl -n ecommerce run invcheck --image=curlimages/curl:8.5.0 \
#     --restart=Never --rm -i --quiet \
#     --env PRODUCT_ID=<uuid> --env STOCK=5 --env BUYERS=20 --overrides='...'
#
# Exit 0 only when exactly STOCK reservations succeed.

INV=${INVENTORY_URL:-http://inventory-service:8013/inventory}
STOCK=${STOCK:-5}
BUYERS=${BUYERS:-20}
CODES=/tmp/inv_codes
rm -f "$CODES"

if [ -z "$PRODUCT_ID" ]; then
  echo "RESULT: FAIL (PRODUCT_ID not set)"
  exit 1
fi

echo "product=$PRODUCT_ID  stock=$STOCK  buyers=$BUYERS"

i=0
while [ "$i" -lt "$BUYERS" ]; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$INV/reserve" -H "Content-Type: application/json" -H "Idempotency-Key: $(cat /proc/sys/kernel/random/uuid)" -d "{\"product_id\":\"$PRODUCT_ID\",\"quantity\":1}" >> "$CODES" &
  i=$((i + 1))
done
wait

GRANTED=$(grep '^2..$' "$CODES" | wc -l | tr -d ' ')
REFUSED=$(grep '^409$' "$CODES" | wc -l | tr -d ' ')
OTHER=$(grep -vE '^(2..|409)$' "$CODES" | wc -l | tr -d ' ')

echo "granted (2xx):  $GRANTED"
echo "refused (409):  $REFUSED"
echo "other:          $OTHER"

if [ "$OTHER" -gt 0 ]; then
  echo "unexpected statuses:"
  grep -vE '^(2..|409)$' "$CODES" | sort | uniq -c
fi

# Granting more than STOCK is an oversell: two transactions read the same
# quantity_available and both proceeded. Granting fewer means the lock, or a
# lock_timeout, refused a buyer who should have been served.
if [ "$GRANTED" -eq "$STOCK" ] && [ "$OTHER" -eq 0 ]; then
  echo "RESULT: PASS - exactly $STOCK of $BUYERS buyers served, no oversell"
  exit 0
fi

if [ "$GRANTED" -gt "$STOCK" ]; then
  echo "RESULT: FAIL - OVERSOLD by $((GRANTED - STOCK)) units"
  exit 1
fi

echo "RESULT: FAIL - expected exactly $STOCK granted and 0 unexpected statuses"
exit 1
