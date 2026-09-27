"""Tests for app/core/storage.py and app/core/sigv4.py.

The signing tests run against the real MinIO container, deliberately. Signature
code that is only checked against itself proves nothing - the whole question is
whether an S3 implementation accepts what we produce, and MinIO is the same
protocol Railway's bucket speaks.
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core import sigv4
from app.core.config import settings
from app.core.storage import StorageError, post_media_key, profile_picture_key, storage


@pytest.fixture
def key() -> str:
    return f"tests/{uuid.uuid4()}.bin"


class TestRoundTrip:
    async def test_put_get_delete(self, key: str):
        await storage.put_object(key, b"hello", content_type="text/plain")
        assert await storage.get_object(key) == b"hello"
        await storage.delete_object(key)
        with pytest.raises(StorageError):
            await storage.get_object(key)

    async def test_presigned_url_is_accepted_by_the_bucket(self, key: str):
        """The end-to-end proof that the hand-rolled SigV4 is correct: an S3 server
        recomputes the signature from what it received and only serves the object
        if the two agree."""
        await storage.put_object(key, b"presigned", content_type="text/plain")
        async with httpx.AsyncClient() as client:
            resp = await client.get(storage.presigned_url(key))
        assert resp.status_code == 200, resp.text
        assert resp.content == b"presigned"
        assert resp.headers["content-type"] == "text/plain"

    async def test_unsigned_request_is_refused(self, key: str):
        """Objects are private. Knowing the key is not enough - which is what makes
        it safe to hand the URL to a client at all."""
        await storage.put_object(key, b"secret", content_type="text/plain")
        url = storage.presigned_url(key).split("?")[0]
        async with httpx.AsyncClient() as client:
            assert (await client.get(url)).status_code == 403

    async def test_content_type_and_cache_control_survive(self, key: str):
        await storage.put_object(
            key,
            b"jpeg-ish-bytes",
            content_type="image/jpeg",
            cache_control="private, max-age=86400, immutable",
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(storage.presigned_url(key))
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == "private, max-age=86400, immutable"

    async def test_delete_of_a_missing_object_is_not_an_error(self):
        """Best-effort by design: the callers that delete (replacing a profile
        picture, resetting content) must not fail a user-visible operation because
        the bucket had already forgotten the object."""
        await storage.delete_object(f"tests/{uuid.uuid4()}-never-written")


class TestUrlStability:
    """The client caches media keyed by URL string, so a signature that rotated per
    request would turn every feed refresh into a full re-download."""

    def test_same_object_yields_the_same_url_within_the_window(self):
        assert storage.presigned_url("post-media/x.jpg") == storage.presigned_url(
            "post-media/x.jpg"
        )

    def test_different_objects_yield_different_urls(self):
        assert storage.presigned_url("post-media/a.jpg") != storage.presigned_url(
            "post-media/b.jpg"
        )

    def test_a_later_signing_instant_produces_a_different_url(self):
        """The flip side: quantization is the only reason the URL holds still. Sign
        from two different instants and the signature genuinely changes, so a
        rolled-over window does hand the client a fresh URL rather than an expired
        one."""
        common = {
            "method": "GET",
            "host": "example.test",
            "canonical_uri": "/bucket/key",
            "access_key_id": "AKID",
            "secret_access_key": "secret",
            "region": "us-east-1",
            "expires_in": 3600,
        }
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert sigv4.presign(signed_at=at, **common) == sigv4.presign(
            signed_at=at, **common
        )
        assert sigv4.presign(signed_at=at, **common) != sigv4.presign(
            signed_at=at + timedelta(minutes=15), **common
        )

    def test_url_carries_the_configured_lifetime(self):
        url = storage.presigned_url("post-media/x.jpg")
        assert f"X-Amz-Expires={settings.MEDIA_URL_TTL_SECONDS}" in url


class TestKeys:
    def test_post_media_key_is_random_and_prefixed(self):
        a = post_media_key("image/jpeg")
        b = post_media_key("image/jpeg")
        assert a != b
        assert a.startswith("post-media/") and a.endswith(".jpg")

    def test_post_media_key_carries_no_identity(self):
        """Anonymity depends on this: the URL of an anonymous post's photo is shown
        to every recipient."""
        user_id = uuid.uuid4()
        assert str(user_id) not in post_media_key("image/jpeg")

    def test_profile_picture_key_is_fresh_each_time(self):
        user_id = uuid.uuid4()
        assert profile_picture_key(user_id, "image/png") != profile_picture_key(
            user_id, "image/png"
        )


class TestSigV4:
    def test_canonical_query_is_sorted_and_encoded(self):
        query = sigv4.canonical_query(
            {"X-Amz-Credential": "AKID/20260101/us-east-1/s3/aws4_request", "b": "1"}
        )
        assert query == (
            "X-Amz-Credential=AKID%2F20260101%2Fus-east-1%2Fs3%2Faws4_request&b=1"
        )

    def test_encode_path_keeps_separators_and_encodes_spaces(self):
        assert sigv4.encode_path("/bucket/a b/c~d") == "/bucket/a%20b/c~d"

    def test_signing_key_is_deterministic(self):
        args = ("secret", "20260101", "us-east-1")
        assert sigv4.signing_key(*args) == sigv4.signing_key(*args)


class TestTransientRetry:
    """`SlowDown` is how S3 asks for a pause, and it arrives on a request that is
    otherwise perfectly good: correctly addressed, correctly signed, accepted by the
    bucket. Nothing underneath this module backs off - SigV4 is hand-rolled here, so
    there is no SDK doing it for us - and `_store_media` (app/api/posts.py) is
    all-or-nothing, so before this retry existed one throttled PUT aborted a whole
    post. That is not hypothetical; it happened in production.

    Scripted transport rather than MinIO: what matters is which answers are replayed
    and which are not, and a real bucket will not throttle on demand.
    """

    @pytest.fixture
    async def scripted(self, monkeypatch):
        """Swap the shared client for one answering from a script, collecting every
        attempt. An `Exception` in the script is raised rather than returned, which
        is how a transport failure is expressed.
        """
        clients: list[httpx.AsyncClient] = []

        def install(*script) -> list[httpx.Request]:
            seen: list[httpx.Request] = []
            queue = list(script)

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                answer = queue.pop(0) if queue else httpx.Response(200)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            clients.append(client)
            monkeypatch.setattr(storage, "_client", client)
            # Real waiting would put the backoff schedule into the suite's runtime
            # for no added confidence; that a wait happens at all is the behaviour.
            monkeypatch.setattr(settings, "STORAGE_RETRY_BASE_MS", 1)
            return seen

        yield install
        for client in clients:
            await client.aclose()

    @staticmethod
    def _slow_down() -> httpx.Response:
        """What Tigris actually answered, trimmed to the part that matters."""
        return httpx.Response(
            503,
            text=(
                '<?xml version="1.0" encoding="UTF-8"?><Error><Code>SlowDown</Code>'
                "<Message>Please reduce your request rate.</Message></Error>"
            ),
        )

    async def test_a_throttled_put_is_retried_and_then_succeeds(self, scripted):
        attempts = scripted(self._slow_down(), httpx.Response(200))
        await storage.put_object("tests/retry.bin", b"x", content_type="text/plain")
        assert len(attempts) == 2

    async def test_a_transport_error_is_retried(self, scripted):
        attempts = scripted(httpx.ConnectError("connection reset"), httpx.Response(200))
        await storage.put_object("tests/retry.bin", b"x", content_type="text/plain")
        assert len(attempts) == 2

    async def test_the_attempt_budget_is_honoured(self, scripted, monkeypatch):
        attempts = scripted(*[self._slow_down()] * 5)
        monkeypatch.setattr(settings, "STORAGE_MAX_ATTEMPTS", 2)
        with pytest.raises(StorageError, match="503"):
            await storage.put_object("tests/retry.bin", b"x", content_type="text/plain")
        assert len(attempts) == 2

    async def test_a_forbidden_request_is_not_retried(self, scripted):
        """A 403 is deterministic - a revoked key or a bad signature answers the same
        however often it is asked - so retrying only delays the error while holding
        the author's request open.
        """
        attempts = scripted(httpx.Response(403, text="<Code>AccessDenied</Code>"))
        with pytest.raises(StorageError, match="403"):
            await storage.put_object("tests/retry.bin", b"x", content_type="text/plain")
        assert len(attempts) == 1

    async def test_a_missing_object_is_not_retried(self, scripted):
        attempts = scripted(httpx.Response(404))
        with pytest.raises(StorageError, match="404"):
            await storage.get_object("tests/absent.bin")
        assert len(attempts) == 1

    async def test_every_attempt_is_signed_afresh(self, scripted, monkeypatch):
        """SigV4 covers `x-amz-date`, so reusing the first attempt's headers would
        turn a retryable 503 into a permanent SignatureDoesNotMatch - the error would
        not go away, only change its face. What is pinned is the re-signing call, not
        the bytes: `x-amz-date` is second-granular, so two attempts milliseconds
        apart legitimately produce an identical signature.
        """
        signings: list[datetime] = []
        real = sigv4.signed_headers

        def counting(**kwargs):
            signings.append(kwargs["signed_at"])
            return real(**kwargs)

        monkeypatch.setattr(sigv4, "signed_headers", counting)
        attempts = scripted(self._slow_down(), httpx.Response(200))
        await storage.put_object("tests/retry.bin", b"x", content_type="text/plain")
        assert len(attempts) == 2
        assert len(signings) == 2

    async def test_a_delete_still_swallows_a_persistent_failure(self, scripted):
        """`delete_object` is best-effort by contract (a missing object is already the
        desired state). Retrying must not turn a give-up into something callers have
        to handle.
        """
        attempts = scripted(*[self._slow_down()] * 5)
        await storage.delete_object("tests/retry.bin")
        assert len(attempts) == settings.STORAGE_MAX_ATTEMPTS
