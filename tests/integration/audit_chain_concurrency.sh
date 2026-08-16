#!/bin/sh
# Concurrency check for the audit hash chain.
#
# Unit tests pin what a valid chain looks like and prove that tampering is
# detectable. They cannot prove that the service builds a valid chain when
# several appends arrive at once, and that is the case where it is easy to get
# wrong: two requests read the same tail record, both compute the same
# sequence, and both link to the same prev_hash. The chain forks, or one write
# is lost. Either way the log stops being evidence.
#
# Appends are serialized by a transaction-scoped advisory lock, with UNIQUE on
# sequence as an independent backstop. This fires N appends in parallel and then
# asks the service to re-hash its entire log.
#
# The assertion is not "every request succeeded" -- a 409 from the UNIQUE
# constraint is a correct outcome, because refusing the second writer is what
# keeps the chain sound. The assertion is that the chain verifies afterwards and
# that nothing returned a 5xx.
#
#   docker run --rm -i --network ecommerce-platform_mesh curlimages/curl:8.5.0 \
#     sh -s < tests/integration/audit_chain_concurrency.sh
#
# Exit 0 only when every append was accepted or cleanly refused, and the whole
# chain still verifies.

AUDIT=${AUDIT_URL:-http://audit-service:8019/audit}
N=${CONCURRENCY:-15}
CODES=/tmp/audit_codes
rm -f "$CODES"

echo "audit=$AUDIT  concurrency=$N"

BEFORE=$(curl -s "$AUDIT/verify")
BEFORE_COUNT=$(echo "$BEFORE" | sed 's/.*"records_checked":\([0-9]*\).*/\1/')
BEFORE_INTACT=$(echo "$BEFORE" | sed 's/.*"intact":\([a-z]*\).*/\1/')
echo "before: $BEFORE_COUNT records, intact=$BEFORE_INTACT"

if [ "$BEFORE_INTACT" != "true" ]; then
  echo "RESULT: FAIL - the chain was already broken before this test ran"
  exit 1
fi

i=0
while [ "$i" -lt "$N" ]; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$AUDIT/records" \
    -H "Content-Type: application/json" \
    -d "{\"actor\":\"contention-check\",\"action\":\"burst.append\",\"resource_type\":\"Test\",\"resource_id\":\"burst-$i\",\"payload\":{\"n\":$i}}" \
    >> "$CODES" &
  i=$((i + 1))
done
wait

ACCEPTED=$(grep '^201$' "$CODES" | wc -l | tr -d ' ')
CONFLICTED=$(grep '^409$' "$CODES" | wc -l | tr -d ' ')
OTHER=$(grep -vE '^(201|409)$' "$CODES" | wc -l | tr -d ' ')

echo "accepted (201):   $ACCEPTED"
echo "conflicted (409): $CONFLICTED"
echo "other:            $OTHER"

if [ "$OTHER" -gt 0 ]; then
  echo "unexpected statuses:"
  grep -vE '^(201|409)$' "$CODES" | sort | uniq -c
  echo "RESULT: FAIL - an append neither succeeded nor was cleanly refused"
  exit 1
fi

AFTER=$(curl -s "$AUDIT/verify")
AFTER_COUNT=$(echo "$AFTER" | sed 's/.*"records_checked":\([0-9]*\).*/\1/')
AFTER_INTACT=$(echo "$AFTER" | sed 's/.*"intact":\([a-z]*\).*/\1/')
echo "after:  $AFTER_COUNT records, intact=$AFTER_INTACT"

if [ "$AFTER_INTACT" != "true" ]; then
  echo "RESULT: FAIL - concurrent appends produced a chain that does not verify"
  echo "$AFTER"
  exit 1
fi

EXPECTED=$((BEFORE_COUNT + ACCEPTED))
if [ "$AFTER_COUNT" -ne "$EXPECTED" ]; then
  echo "RESULT: FAIL - expected $EXPECTED records, found $AFTER_COUNT"
  echo "        an accepted append is missing, or one landed without a 201"
  exit 1
fi

echo "RESULT: PASS - $ACCEPTED concurrent appends, chain verifies over $AFTER_COUNT records"
exit 0
