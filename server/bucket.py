"""Attachment files in an S3-compatible bucket (Cloudflare R2 by default), reached
through URLs presigned here with AWS Signature Version 4, so no SDK is needed."""

import hmac
import os
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

import httpx2

ENDPOINT = os.environ.get("S3_ENDPOINT", "").rstrip("/")
BUCKET = os.environ.get("S3_BUCKET")
ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
REGION = os.environ.get("S3_REGION", "auto")
ENABLED = all([ENDPOINT, BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY])


def presign(method: str, key: str, expires: int = 3600) -> str:
    """A URL that lets its holder make this one request for expires seconds."""
    now = datetime.now(UTC)
    day = now.strftime("%Y%m%d")
    scope = f"{day}/{REGION}/s3/aws4_request"
    path = f"/{BUCKET}/{key}"  # keys are hex, so they need no escaping
    query = urlencode(
        {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{ACCESS_KEY_ID}/{scope}",
            "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
            "X-Amz-Expires": expires,
            "X-Amz-SignedHeaders": "host",
        },
        safe="-_.~",
    )
    request = "\n".join(
        [
            method,
            path,
            query,
            f"host:{urlsplit(ENDPOINT).netloc}\n",
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    signed = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            now.strftime("%Y%m%dT%H%M%SZ"),
            scope,
            sha256(request.encode()).hexdigest(),
        ]
    )
    signing_key = f"AWS4{SECRET_ACCESS_KEY}".encode()
    for part in (day, REGION, "s3", "aws4_request"):
        signing_key = hmac.digest(signing_key, part.encode(), "sha256")
    signature = hmac.new(signing_key, signed.encode(), sha256).hexdigest()
    return f"{ENDPOINT}{path}?{query}&X-Amz-Signature={signature}"


async def request(method: str, key: str, content: bytes | None = None):
    async with httpx2.AsyncClient(timeout=30) as client:
        return await client.request(method, presign(method, key), content=content)
