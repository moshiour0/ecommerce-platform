"""
Object storage: pre-signed URLs, and nothing else.

Bytes never pass through this service. A caller asks for an upload URL, PUTs
the file straight to the object store, and tells us it is there. Proxying
uploads through a Python process would put every megabyte of every product
photo and every try-on source through a request handler that has no reason to
see them, and would make the service's memory profile a function of what people
upload.

S3-compatible on purpose. MinIO runs locally and S3 or any equivalent runs in
production; the difference is three environment variables.
"""

import logging
import os

import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)

# Where this service reaches the store. Inside the mesh that is a compose
# service name; in production it is the provider's endpoint.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")

# Where a browser reaches it. A pre-signed URL is signed for a specific host,
# so a URL signed for "minio:9000" is useless to a client that cannot resolve
# that name -- which is every client outside the mesh.
S3_PUBLIC_ENDPOINT_URL = os.getenv("S3_PUBLIC_ENDPOINT_URL", "http://localhost:9000")

S3_BUCKET = os.getenv("S3_BUCKET", "media")

# Rule 8: no hardcoded fallback for a credential.
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")

# Long enough for a large file on a poor connection, short enough that a leaked
# URL is not a standing grant.
UPLOAD_URL_TTL_SECONDS = int(os.getenv("UPLOAD_URL_TTL_SECONDS", "900"))


def _client(endpoint: str):
    if not S3_ACCESS_KEY or not S3_SECRET_KEY:
        raise RuntimeError(
            "S3_ACCESS_KEY and S3_SECRET_KEY are required. Refusing to start "
            "an upload path with no credentials.")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        # Path style keeps bucket names out of the hostname, which is what MinIO
        # serves and what avoids DNS games in development.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )


def presigned_upload_url(key: str, content_type: str) -> dict:
    """A URL the client may PUT one object to.

    The content type is part of the signature, so a caller who asked to upload
    a JPEG cannot use the same URL to store an executable -- the store rejects
    a PUT whose Content-Type does not match what was signed.
    """
    url = _client(S3_PUBLIC_ENDPOINT_URL).generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        HttpMethod="PUT",
    )
    return {
        "url": url,
        "method": "PUT",
        "headers": {"Content-Type": content_type},
        "expires_in": UPLOAD_URL_TTL_SECONDS,
        "storage_key": key,
    }


def public_url(key: str) -> str:
    """The unauthenticated URL for an object under a public prefix.

    Only meaningful for prefixes the bucket policy grants anonymous read to;
    media_rules decides which those are, and this function does not check --
    the caller has already established that the asset is publicly servable.
    """
    return f"{S3_PUBLIC_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET}/{key}"
