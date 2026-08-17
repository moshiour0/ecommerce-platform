#!/bin/sh
# Object storage access check: the bucket policy, asserted against the bucket.
#
# media_rules decides which purposes are confidential, and bucket_policy()
# generates the bucket's access policy from that same taxonomy so the two
# cannot disagree. That generation is unit tested. What is not provable in a
# unit test is whether the object store enforces what was generated -- and a
# policy that the store silently accepted but does not apply looks identical
# from the outside until someone reads a KYC document.
#
# So this asks the store, anonymously, for one object under each kind of
# prefix:
#
#   1. products/  must be readable without credentials. Listing photos are
#      served straight from the store to browsers; if this fails, every product
#      image on the site is broken.
#
#   2. documents/ must not be. A seller's trade licence and a customer's
#      try-on photo passing a virus scan does not make either of them public.
#      This is the assertion that matters: a bucket created without a policy,
#      or created with `mc anonymous set public`, passes check 1 and fails
#      here.
#
# Both objects are written with credentials first, because "403 on a key that
# holds nothing" would pass check 2 for the wrong reason -- MinIO answers 403
# rather than 404 to an anonymous caller precisely so that missing and
# forbidden are indistinguishable. Writing the object first removes that
# ambiguity: the object is definitely there, and the anonymous caller is
# definitely being refused.
#
# Runs inside the mesh, like the other integration checks:
#
#   docker run --rm -i --network ecommerce-platform_mesh \
#     -e S3_ACCESS_KEY -e S3_SECRET_KEY \
#     curlimages/curl:8.5.0 sh -s < tests/integration/storage_policy.sh

set -eu

ENDPOINT="${S3_ENDPOINT_URL:-http://minio:9000}"
BUCKET="${S3_BUCKET:-media}"
ACCESS_KEY="${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
SECRET_KEY="${S3_SECRET_KEY:?S3_SECRET_KEY is required}"

STAMP=$(date +%s)
PUBLIC_KEY="products/policy-check/${STAMP}.jpg"
PRIVATE_KEY="documents/policy-check/${STAMP}.pdf"

FAILURES=0

# curl --user with --aws-sigv4 signs the request the way the SDK does; this is
# the only place in the script that has credentials at all.
#
# The body goes to a file rather than up the pipe: uploading from stdin makes
# curl use chunked transfer encoding, and S3 signature v4 requires a
# Content-Length, so a piped body comes back 411 before the policy is ever
# consulted.
put_object() {
  key=$1
  body=$2
  printf '%s' "$body" > /tmp/probe-body
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X PUT --upload-file /tmp/probe-body \
    --user "${ACCESS_KEY}:${SECRET_KEY}" \
    --aws-sigv4 "aws:amz:us-east-1:s3" \
    -H "Content-Type: application/octet-stream" \
    "${ENDPOINT}/${BUCKET}/${key}")
  if [ "$code" != "200" ]; then
    echo "  FAIL  could not write ${key} (HTTP ${code}) -- cannot test the policy"
    exit 1
  fi
  echo "  wrote ${key}"
}

anonymous_get() {
  curl -s -o /dev/null -w '%{http_code}' "${ENDPOINT}/${BUCKET}/$1"
}

delete_object() {
  curl -s -o /dev/null -X DELETE \
    --user "${ACCESS_KEY}:${SECRET_KEY}" \
    --aws-sigv4 "aws:amz:us-east-1:s3" \
    "${ENDPOINT}/${BUCKET}/$1"
}

echo "Object storage policy check against ${ENDPOINT}/${BUCKET}"
echo

echo "Seeding one object under each kind of prefix:"
put_object "$PUBLIC_KEY" "public-listing-photo-bytes"
put_object "$PRIVATE_KEY" "confidential-trade-licence-bytes"
echo

echo "1. A public prefix must be readable with no credentials"
code=$(anonymous_get "$PUBLIC_KEY")
if [ "$code" = "200" ]; then
  echo "  PASS  anonymous GET ${PUBLIC_KEY} -> 200"
else
  echo "  FAIL  anonymous GET ${PUBLIC_KEY} -> ${code} (expected 200)"
  echo "        listing photos are served directly from the store; this breaks them all"
  FAILURES=$((FAILURES + 1))
fi
echo

echo "2. A confidential prefix must not be"
code=$(anonymous_get "$PRIVATE_KEY")
if [ "$code" = "403" ] || [ "$code" = "404" ]; then
  echo "  PASS  anonymous GET ${PRIVATE_KEY} -> ${code}"
else
  echo "  FAIL  anonymous GET ${PRIVATE_KEY} -> ${code} (expected 403)"
  echo "        seller documents and try-on photos are world readable"
  FAILURES=$((FAILURES + 1))
fi
echo

delete_object "$PUBLIC_KEY"
delete_object "$PRIVATE_KEY"
echo "cleaned up both probe objects"
echo

if [ "$FAILURES" -eq 0 ]; then
  echo "PASS: the bucket enforces the purpose taxonomy"
  exit 0
fi
echo "FAIL: ${FAILURES} check(s) failed"
exit 1
