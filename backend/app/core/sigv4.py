"""AWS Signature Version 4 - the only part of the S3 protocol this backend
implements itself.

Written here rather than pulled in with boto3 for one reason boto3 cannot give
us: **a presigned URL has to come out byte-identical for every request inside a
time window.** botocore stamps `X-Amz-Date` from the wall clock at the moment
`generate_presigned_url` is called, so two feed responses a second apart hand the
client two different URL strings for the same object. The mobile client caches
media keyed by that string (see `AuthenticatedByteCache` in the app repo), so a
rotating signature means every scroll-to-refresh re-downloads every image and
every video poster. Pinning the signing timestamp fixes that, and pinning it is
not something botocore exposes.

That leaves us signing our own requests, which is less alarming than it sounds:
SigV4 is a fixed, fully specified recipe over HMAC-SHA256 with no network
involvement, this backend needs exactly four operations against it (PUT/GET/
DELETE an object, PUT a bucket), and app/core/storage.py exercises all of them
against a real S3 implementation in the test suite. Doing it here also means no
second HTTP stack: app/core/storage.py drives it with the `httpx` client the
project already depends on.

Both flavours are here because both are needed, and they differ only in where the
signature is carried:
- `presign` puts the credential, timestamp and signature in the **query string**,
  producing a URL anyone can GET with no headers at all. This is what the client
  is handed.
- `signed_headers` puts them in an **Authorization header**. This is what the
  backend itself uses to upload and delete.
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
_SERVICE = "s3"

# Tells S3 not to fold a body hash into the signature. Required for query-string
# signing (the signer never sees the body) and harmless for header signing of an
# empty body.
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def encode_path(path: str) -> str:
    """Percent-encode an object path for the canonical request.

    `/` stays literal (it separates key segments); Python's `quote` already leaves
    the RFC 3986 unreserved set alone, which is exactly what SigV4 asks for.
    Everything else - spaces, `+`, non-ASCII - is encoded, and encoded identically
    here and in the URL we hand out, which is the only thing that actually matters.
    """
    return quote(path, safe="/")


def _encode_param(value: str) -> str:
    # Nothing is safe in a query value - not even `/`, which appears inside
    # X-Amz-Credential and must reach S3 as %2F.
    return quote(value, safe="")


def canonical_query(params: dict[str, str]) -> str:
    """Query parameters in the byte order SigV4 requires: sorted by encoded name,
    each name and value percent-encoded."""
    encoded = sorted(
        (_encode_param(name), _encode_param(value)) for name, value in params.items()
    )
    return "&".join(f"{name}={value}" for name, value in encoded)


def credential_scope(datestamp: str, region: str) -> str:
    return f"{datestamp}/{region}/{_SERVICE}/aws4_request"


def signing_key(secret_access_key: str, datestamp: str, region: str) -> bytes:
    key = _hmac(f"AWS4{secret_access_key}".encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, _SERVICE)
    return _hmac(key, "aws4_request")


def _sign(
    *,
    secret_access_key: str,
    region: str,
    amz_date: str,
    canonical_request: str,
) -> str:
    datestamp = amz_date[:8]
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            credential_scope(datestamp, region),
            sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    return hmac.new(
        signing_key(secret_access_key, datestamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_request(
    *,
    method: str,
    canonical_uri: str,
    query: str,
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """Returns `(canonical_request, signed_header_names)`.

    Header names are lowercased and sorted, values stripped - S3 recomputes this
    from what it actually received, so any disagreement here comes back as a 403
    with no further explanation.
    """
    normalized = sorted(
        (name.lower(), value.strip()) for name, value in headers.items()
    )
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in normalized)
    signed = ";".join(name for name, _ in normalized)
    canonical_request = "\n".join(
        [method, canonical_uri, query, canonical_headers, signed, payload_hash]
    )
    return canonical_request, signed


def presign(
    *,
    method: str,
    host: str,
    canonical_uri: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    signed_at: datetime,
    expires_in: int,
    extra_query: dict[str, str] | None = None,
) -> str:
    """The query string (no leading `?`) authorizing `method` on `canonical_uri`
    until `expires_in` seconds after `signed_at`.

    `signed_at` is a parameter rather than read from the clock precisely so the
    caller can quantize it - see the module docstring, and
    `storage.ObjectStorage.presigned_url` for the quantization actually used. The
    result is a pure function of the arguments: same arguments, same string.

    `host` must be the host the client will really connect to. It is a signed
    header, so a URL signed for `minio:9000` and then rewritten to `localhost:9000`
    fails with SignatureDoesNotMatch - which is why the storage settings carry a
    separate public endpoint instead of patching the string afterwards.
    """
    amz_date = signed_at.strftime(AMZ_DATE_FORMAT)
    params = dict(extra_query or {})
    params.update(
        {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": (
                f"{access_key_id}/{credential_scope(amz_date[:8], region)}"
            ),
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires_in),
            "X-Amz-SignedHeaders": "host",
        }
    )
    query = canonical_query(params)
    canonical_request, _ = _canonical_request(
        method=method,
        canonical_uri=canonical_uri,
        query=query,
        headers={"host": host},
        payload_hash=UNSIGNED_PAYLOAD,
    )
    signature = _sign(
        secret_access_key=secret_access_key,
        region=region,
        amz_date=amz_date,
        canonical_request=canonical_request,
    )
    return f"{query}&X-Amz-Signature={signature}"


def signed_headers(
    *,
    method: str,
    host: str,
    canonical_uri: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    signed_at: datetime,
    payload: bytes | None,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, str]:
    """Headers (including `Authorization`) authorizing one request the backend
    makes itself. Used for uploads, deletes and bucket creation; never handed out.

    Unlike `presign` this hashes the real body: S3 verifies `x-amz-content-sha256`
    against what it received, so an upload is authenticated end to end rather than
    only in its headers.
    """
    amz_date = signed_at.strftime(AMZ_DATE_FORMAT)
    payload_hash = EMPTY_PAYLOAD_SHA256 if not payload else sha256_hex(payload)

    to_sign = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    to_sign.update({name.lower(): value for name, value in (headers or {}).items()})

    canonical_request, signed = _canonical_request(
        method=method,
        canonical_uri=canonical_uri,
        query=canonical_query(query or {}),
        headers=to_sign,
        payload_hash=payload_hash,
    )
    signature = _sign(
        secret_access_key=secret_access_key,
        region=region,
        amz_date=amz_date,
        canonical_request=canonical_request,
    )
    authorization = (
        f"{ALGORITHM} "
        f"Credential={access_key_id}/{credential_scope(amz_date[:8], region)}, "
        f"SignedHeaders={signed}, "
        f"Signature={signature}"
    )
    # `host` is dropped from the returned headers: httpx sets it from the URL, and
    # repeating it here risks sending a value that differs from what goes on the
    # wire (which is precisely what the signature covers).
    out = {name: value for name, value in to_sign.items() if name != "host"}
    out["Authorization"] = authorization
    return out
