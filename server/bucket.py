"""Attachment files in an S3-compatible bucket (Cloudflare R2 by default), reached
through URLs presigned here with AWS Signature Version 4, so no SDK is needed.
Callers deal in keys, bytes and URLs; every failure is a BucketError."""

import hmac
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

import httpx2

ENDPOINT = os.environ.get("S3_ENDPOINT", "").rstrip("/")
BUCKET = os.environ.get("S3_BUCKET")
ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
REGION = os.environ.get("S3_REGION", "auto")
ENABLED = all([ENDPOINT, BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY])
URL_SECONDS = 3600  # how long a presigned URL works


class BucketError(Exception):
    """The bucket refused a request or could not be reached."""


def upload_url(key: str) -> tuple[str, datetime]:
    """A URL that stores a file under key with one PUT, and when it stops working."""
    return _presign("PUT", key)


def download_url(key: str) -> tuple[str, datetime]:
    """A URL that fetches the file under key, and when it stops working."""
    return _presign("GET", key)


async def put(key: str, data: bytes) -> None:
    await _send("PUT", key, data)


async def get(key: str) -> bytes:
    return (await _send("GET", key)).content


async def size(key: str) -> int | None:
    """The size in bytes of the file under key, or None if there is none yet."""
    response = await _send("HEAD", key, missing_ok=True)
    if response.status_code == 404:
        return None
    return int(response.headers["content-length"])


async def delete(key: str) -> None:
    await _send("DELETE", key)


async def _send(
    method: str, key: str, content: bytes | None = None, missing_ok: bool = False
) -> httpx2.Response:
    try:
        async with httpx2.AsyncClient(timeout=30) as client:
            url, _ = _presign(method, key)
            response = await client.request(method, url, content=content)
    except httpx2.HTTPError as error:
        raise BucketError(f"{method} {key} failed: {error}") from error
    if not response.is_success and not (missing_ok and response.status_code == 404):
        raise BucketError(f"{method} {key} failed: HTTP {response.status_code}")
    return response


def _presign(method: str, key: str) -> tuple[str, datetime]:
    """A URL that lets its holder make this one request for URL_SECONDS, and when it
    stops working."""
    now = datetime.now(UTC)
    day = now.strftime("%Y%m%d")
    scope = f"{day}/{REGION}/s3/aws4_request"
    path = f"/{BUCKET}/{key}"  # keys are hex, so they need no escaping
    query = urlencode(
        {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{ACCESS_KEY_ID}/{scope}",
            "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
            "X-Amz-Expires": URL_SECONDS,
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
    url = f"{ENDPOINT}{path}?{query}&X-Amz-Signature={signature}"
    return url, now + timedelta(seconds=URL_SECONDS)
