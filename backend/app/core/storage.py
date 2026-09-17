"""S3-compatible object storage: where every uploaded image, video and poster
frame actually lives.

Replaces the in-DB byte columns (`User.profile_picture`, `PostMedia.data` /
`.poster`) that this project used as a stopgap while there was nowhere else to
put them. Postgres was never the right home for tens of megabytes per post: it
made every backup carry the media, forced deferred columns and `populate_existing`
dances to stop a feed query dragging whole videos through the ORM, and gave the
video player an in-memory byte-range slice instead of real HTTP range serving.

Two implementations, one protocol:
- **local dev / CI**: a MinIO container in docker-compose, path-style addressing
  (`http://minio:9000/<bucket>/<key>` - you cannot do bucket-as-subdomain DNS
  against `localhost`), plain HTTP.
- **Railway**: a Railway Bucket (Tigris underneath), virtual-hosted addressing
  (`https://<bucket>.storage.railway.app/<key>`), region `auto`.

Nothing in this module knows which is which; `AWS_*` / `S3_*` in app/core/config.py is
the whole difference. See RAILWAY.md for the variable mapping.

**How access control survives the move.** Objects are never public. A client
never talks to this module, and never holds a bucket credential; it is handed a
**presigned URL** - a URL carrying a short-lived SigV4 signature in its query
string - which the backend only generates after the existing authorization check
has already passed (`_can_view_post` for post media, and the same anonymity gate
in `_serialize_post` that withholds an anonymous author's name also withholds
their picture). Three consequences worth being deliberate about:

1. **The check moves from every byte fetch to once per serialization.** A URL
   handed out stays usable for up to `MEDIA_URL_TTL_SECONDS` even if the viewer's
   access is revoked in the meantime, and it is shareable by whoever holds it.
   That window is the price of not proxying bytes through the backend; keep the
   TTL modest rather than convenient.
2. **Object keys must not encode anything the post hides.** Post-media keys are
   random UUIDs under a flat prefix - deliberately *not* derived from the author -
   because a key derived from `author_id` would deanonymize an anonymous post to
   anyone who saw the URL, which is the same failure mode the EXIF strip in
   app/core/media_validation.py exists to prevent.
3. **The bucket endpoint has to be reachable by the client**, even though the
   bucket itself is private. That is why `S3_PUBLIC_ENDPOINT_URL` exists
   separately from `AWS_ENDPOINT_URL`: the host is a *signed* header, so a URL
   signed against the internal name and rewritten afterwards fails with
   SignatureDoesNotMatch. Signing happens against the public host directly.

**Why the URLs are stable.** `presigned_url` quantizes its signing timestamp to a
`MEDIA_URL_REFRESH_SECONDS` boundary, so the same object yields the *same string*
for every request in that window, and only then rolls over. The client caches
media keyed by URL (`AuthenticatedByteCache` in the app repo); a signature that
rotated per request would silently turn every feed refresh into a full
re-download of every image and poster. The cost is that a URL is only guaranteed
`TTL - REFRESH` seconds of remaining life at the moment it is handed out - which
is why the two settings are set with a wide gap between them.
"""

import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.core import sigv4
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

# Enough to keep a bucket listing human-readable; the extension is cosmetic, since
# what a client is served is decided by the object's stored Content-Type.
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}

PROFILE_PICTURE_PREFIX = "profile-pictures"
POST_MEDIA_PREFIX = "post-media"
FEEDBACK_MEDIA_PREFIX = "feedback-media"

# Stored on every object we upload and echoed back by the bucket on a presigned
# GET. Safe to make this aggressive: every key here is a fresh UUID per upload
# and nothing ever rewrites one, so a cached response can never go stale - the
# object is either the one the URL names or gone. `private` because the
# presigned URL *is* the capability, and a shared cache has no business keeping
# a copy of it.
#
# It lives here, next to the key builders, rather than as a constant in each API
# module. It was declared twice - identically, with the same paragraph of
# reasoning - in app/api/posts.py and app/api/feedback.py, and the third upload
# path (profile pictures, app/api/users.py) simply never passed one. That is the
# failure mode a duplicated constant has: not a disagreement, an omission. The
# Flutter client documents relying on this header being present on *every*
# object (see `core/media/presentation/network_media_image.dart` there), so an
# upload path that forgets it silently makes those images re-download on a
# ~15-minute cycle as the presigned URL rolls over.
MEDIA_CACHE_CONTROL = "private, max-age=86400, immutable"


class StorageError(RuntimeError):
    """The bucket refused or could not be reached. Always a 5xx as far as a client
    is concerned - never something the user did wrong."""


def _extension(content_type: str) -> str:
    return _EXTENSIONS.get(content_type, "bin")


def profile_picture_key(user_id: uuid.UUID, content_type: str) -> str:
    """A fresh key per upload, never a stable path per user.

    Replacing a picture therefore writes a *new* object and a new URL, which is
    what makes the client's cache invalidate itself. The previous object is
    deleted explicitly by the route (see app/api/users.py); overwriting one fixed
    key instead would leave every cache in the system serving the old face.
    """
    return (
        f"{PROFILE_PICTURE_PREFIX}/{user_id}/{uuid.uuid4()}.{_extension(content_type)}"
    )


def post_media_key(content_type: str) -> str:
    """A flat, random key. Carries no post id and no author id on purpose - see
    point 2 in the module docstring."""
    return f"{POST_MEDIA_PREFIX}/{uuid.uuid4()}.{_extension(content_type)}"


def feedback_media_key(content_type: str) -> str:
    """Flat and random for the same reason as `post_media_key`, and here it is not
    even a judgement call: a feedback submission can be *sent anonymously*, in which
    case the row carries no `user_id` at all (see app/models/feedback.py). A key
    derived from the submitter would put back exactly the link the anonymous option
    exists to remove.

    Its own prefix rather than sharing POST_MEDIA_PREFIX purely so a bucket listing
    stays readable and a retention rule can be scoped to feedback alone - the two
    kinds of object have quite different lifetimes.
    """
    return f"{FEEDBACK_MEDIA_PREFIX}/{uuid.uuid4()}.{_extension(content_type)}"


class ObjectStorage:
    """A minimal S3 client: exactly the four operations this backend performs.

    Deliberately not a general S3 SDK. Everything it does is one signed HTTP
    request, so the whole surface is `put_object` / `get_object` / `delete_object`
    / `ensure_bucket` plus the synchronous `presigned_url`.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # --- configuration -----------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(
            settings.AWS_ENDPOINT_URL
            and settings.S3_BUCKET_NAME
            and settings.AWS_ACCESS_KEY_ID
            and settings.AWS_SECRET_ACCESS_KEY
        )

    def _require_configured(self) -> None:
        if not self.configured:
            raise StorageError(
                "object storage is not configured - set AWS_ENDPOINT_URL, "
                "S3_BUCKET_NAME, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
            )

    def _address(self, endpoint: str, key: str | None) -> tuple[str, str, str]:
        """`(url, host, canonical_uri)` for `key` (None means the bucket itself).

        The two addressing styles differ in where the bucket name goes, and both
        are needed: MinIO on `localhost` can only do path-style (there is no
        wildcard DNS for `<bucket>.localhost`), while Railway/Tigris serves
        virtual-hosted URLs. `canonical_uri` is what gets signed, so it has to be
        derived here rather than guessed from the URL afterwards.
        """
        parts = urlsplit(endpoint)
        bucket = settings.S3_BUCKET_NAME
        if settings.S3_ADDRESSING_STYLE == "virtual":
            host = f"{bucket}.{parts.netloc}"
            path = f"/{key}" if key is not None else "/"
        else:
            host = parts.netloc
            path = f"/{bucket}/{key}" if key is not None else f"/{bucket}"
        canonical_uri = sigv4.encode_path(path)
        return f"{parts.scheme}://{host}{canonical_uri}", host, canonical_uri

    # --- URL generation (synchronous, no I/O) ------------------------------

    def presigned_url(self, key: str, *, method: str = "GET") -> str:
        """A URL that grants `method` on `key` without any credential of the
        caller's own, valid for `MEDIA_URL_TTL_SECONDS` from the quantized
        signing instant.

        Pure computation - no round trip - which is what makes it safe to call
        once per attachment while serializing a whole feed page.
        """
        self._require_configured()
        endpoint = settings.S3_PUBLIC_ENDPOINT_URL or settings.AWS_ENDPOINT_URL
        url, host, canonical_uri = self._address(str(endpoint), key)
        query = sigv4.presign(
            method=method,
            host=host,
            canonical_uri=canonical_uri,
            access_key_id=settings.AWS_ACCESS_KEY_ID,
            secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region=settings.AWS_DEFAULT_REGION,
            signed_at=_quantized_now(),
            expires_in=settings.MEDIA_URL_TTL_SECONDS,
        )
        return f"{url}?{query}"

    # --- operations --------------------------------------------------------

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
    ) -> None:
        """Upload `data` under `key`, replacing whatever was there.

        `content_type` is stored on the object, so a presigned GET serves the
        right type without the URL having to override it - which matters for
        video, where the player picks its decoder from the response header.
        """
        headers = {"content-type": content_type}
        if cache_control:
            headers["cache-control"] = cache_control
        await self._request("PUT", key, payload=data, headers=headers)

    async def get_object(self, key: str) -> bytes:
        """Read an object back. Not on any request path - the client fetches
        objects itself - but needed by the migration and by scripts that re-run
        the media pipeline over what is already stored."""
        response = await self._request("GET", key)
        return response.content

    async def delete_object(self, key: str) -> None:
        """Best-effort: a missing object is already the desired state, and a
        failure here must not fail the user-visible operation that triggered it
        (replacing a profile picture, say). Logged, not raised."""
        try:
            await self._request("DELETE", key)
        except StorageError:
            # WARNING, not ERROR: nothing the user did failed. What it leaves
            # behind is an object nobody references and a bucket that grows
            # quietly, which is exactly the kind of thing only a log notices.
            log.warning("storage.delete_failed", object_key=key, exc_info=True)

    async def ensure_bucket(self) -> None:
        """Create the bucket if it is missing, when S3_AUTO_CREATE_BUCKET is
        on. Local dev and CI only: on Railway the bucket is provisioned by the
        platform and the credentials are scoped to it, so the setting stays off
        and this is never called.
        """
        if not settings.S3_AUTO_CREATE_BUCKET:
            return
        try:
            await self._request("PUT", None, warn_on_error=False)
            log.info("storage.bucket_created", bucket=settings.S3_BUCKET_NAME)
        except StorageError as exc:
            # Already existing is the normal case on every boot after the first.
            # S3 spells it BucketAlreadyOwnedByYou (200/409 depending on region
            # rules); MinIO returns 409.
            if "409" in str(exc) or "BucketAlreadyOwnedByYou" in str(exc):
                return
            # `warn_on_error=False` above, because the 409 this swallows is the
            # normal case on every boot after the first and would otherwise put a
            # WARNING in the startup log of every single deploy. A real failure
            # still gets a line - this one - before it takes the process down.
            log.error("storage.bucket_create_failed", bucket=settings.S3_BUCKET_NAME)
            raise

    # --- plumbing ----------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        # Lazily created so importing this module never touches the event loop,
        # and shared so uploads reuse connections (a post can carry five files).
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        key: str | None,
        *,
        payload: bytes | None = None,
        headers: dict[str, str] | None = None,
        warn_on_error: bool = True,
    ) -> httpx.Response:
        self._require_configured()
        url, host, canonical_uri = self._address(str(settings.AWS_ENDPOINT_URL), key)
        request_headers = sigv4.signed_headers(
            method=method,
            host=host,
            canonical_uri=canonical_uri,
            access_key_id=settings.AWS_ACCESS_KEY_ID,
            secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region=settings.AWS_DEFAULT_REGION,
            signed_at=datetime.now(timezone.utc),
            payload=payload,
            headers=headers,
        )
        started = time.perf_counter()
        try:
            response = await self._http().request(
                method, url, content=payload, headers=request_headers
            )
        except httpx.HTTPError as exc:
            # The bucket being unreachable is invisible from the outside - the
            # route turns it into a generic 503 - so this line is the only place
            # the actual transport error is ever recorded.
            if warn_on_error:
                log.warning(
                    "storage.request_failed",
                    http_method=method,
                    object_key=key,
                    error=str(exc),
                )
            raise StorageError(f"{method} {key or '<bucket>'} failed: {exc}") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if response.status_code >= 400:
            if warn_on_error:
                log.warning(
                    "storage.request_failed",
                    http_method=method,
                    object_key=key,
                    status=response.status_code,
                    duration_ms=elapsed_ms,
                )
            raise StorageError(
                f"{method} {key or '<bucket>'} -> {response.status_code}: "
                f"{response.text[:500]}"
            )
        # DEBUG: one per attachment on every upload. Kept because upload latency
        # is the bucket's, not ours, and this is what tells the two apart.
        log.debug(
            "storage.request",
            http_method=method,
            object_key=key,
            status=response.status_code,
            duration_ms=elapsed_ms,
            bytes=len(payload) if payload else 0,
        )
        return response


_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def _quantized_now() -> datetime:
    """Now, floored to a `MEDIA_URL_REFRESH_SECONDS` boundary.

    This single line is what makes presigned URLs stable (see the module
    docstring). A refresh interval of 0 disables quantization and signs from the
    live clock, which is only useful for proving the stability behaviour in tests.
    """
    now = datetime.now(timezone.utc)
    step = settings.MEDIA_URL_REFRESH_SECONDS
    if step <= 0:
        return now
    return datetime.fromtimestamp(int(now.timestamp()) // step * step, tz=timezone.utc)


storage = ObjectStorage()
