import io
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.feedback import Feedback
from app.models.user import User
from tests.utils import get_jwt_header


def _make_test_png(width: int = 90, height: int = 160, *, with_exif: bool = False):
    """Deliberately 9:16 - neither of the two fixed post ratios.

    That shape is the point of most of this file: a screenshot has whatever shape
    the reporter's screen has, so anything the post pipeline would reject for its
    aspect ratio must be accepted here.
    """
    img = Image.new("RGB", (width, height), color=(30, 90, 200))
    buf = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x0112] = 1  # Orientation, standing in for the GPS tags that matter.
        img.save(buf, format="PNG", exif=exif)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def _make_test_video(duration: float = 1.0, size: str = "160x90") -> bytes:
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "clip.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"testsrc=duration={duration}:size={size}:rate=5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_bytes()


def _form(**overrides) -> dict:
    """A minimal valid submission; override one field per test."""
    data = {
        "kind": "feedback",
        "message": "The feed is great, the composer less so.",
        "consent": "true",
    }
    data.update(overrides)
    return data


class TestCreateFeedback:
    async def test_signed_out_submission_is_stored_anonymously(
        self, client: AsyncClient, db: AsyncSession
    ):
        """The whole reason this route allows no auth: "I can't sign in" has to be
        reportable from the login screen."""
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        response = await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug", message="Login says invalid credentials."),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "bug"

        feedback = await db.get(Feedback, body["id"])
        await db.refresh(feedback)
        assert feedback.user_id is None
        assert feedback.is_anonymous is True
        assert feedback.allow_contact is False
        assert feedback.contact_email is None
        # Server-stamped, and stamped now - not taken from anything the client sent.
        assert before <= feedback.user_agreed_data_saving_at <= datetime.now(
            timezone.utc
        ) + timedelta(seconds=5)

    async def test_arrives_as_new_and_the_submitter_cannot_set_the_state(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        """`status` is ours, not the submitter's.

        Not merely filtered out - unreachable: `feedback_create_form` is an
        explicit allow-list of form fields and the route builds the row itself, so
        an extra multipart field is ignored the same way one naming
        `user_agreed_data_saving_at` or `user_id` would be. Sending one must
        neither take effect nor make the request fail.
        """
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(status="done", user_agreed_data_saving_at="1999-01-01"),
            headers=get_jwt_header(user),
        )
        assert response.status_code == 201, response.text

        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.status == "new"
        assert feedback.user_agreed_data_saving_at.year != 1999

    async def test_signed_out_cannot_claim_identity_or_contact(
        self, client: AsyncClient, db: AsyncSession
    ):
        """A crafted request must not be able to attach itself to an account it is
        not signed in as, nor ask to be contacted with no address to contact."""
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="false", allow_contact="true"),
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.is_anonymous is True
        assert feedback.allow_contact is False
        assert feedback.contact_email is None

    async def test_signed_in_non_anonymous_records_the_user(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="false", allow_contact="false"),
            headers=get_jwt_header(user),
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.user_id == user.id
        assert feedback.is_anonymous is False
        assert feedback.contact_email is None

    async def test_allow_contact_resolves_the_address_off_the_account(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        """No address is stored - `allow_contact` is consent to be reached *as an
        account*, and the address is read off it when a reply is written."""
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="false", allow_contact="true"),
            headers=get_jwt_header(user),
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.allow_contact is True
        assert feedback.contact_email == user.email
        # Nothing was copied: the address tracks the account rather than a snapshot
        # of it, so changing it changes where a reply would go.
        user.email = f"changed-{user.email}"
        db.add(user)
        await db.commit()
        await db.refresh(feedback)
        assert feedback.contact_email == user.email

    async def test_contact_address_disappears_with_the_account(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        """The reason not to snapshot: `user_id` is ON DELETE SET NULL, so erasing
        an account cuts the link - a stored copy would outlive the erasure and sit
        on in this table after the person asked to be forgotten."""
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="false", allow_contact="true"),
            headers=get_jwt_header(user),
        )
        feedback_id = response.json()["id"]

        await db.delete(user)
        await db.commit()

        feedback = await db.get(Feedback, feedback_id, populate_existing=True)
        assert feedback is not None, "the report itself must survive the erasure"
        assert feedback.user_id is None
        assert feedback.contact_email is None

    async def test_anonymous_signed_in_submission_stores_no_user_id(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        """Anonymity is the *absence of the link*, not a flag over a stored one -
        a row keeping user_id would be de-anonymized by anyone with DB access."""
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="true"),
            headers=get_jwt_header(user),
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.user_id is None
        assert feedback.is_anonymous is True

    async def test_anonymous_plus_contact_is_refused(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        response = await client.post(
            "/api/v1/feedback",
            data=_form(is_anonymous="true", allow_contact="true"),
            headers=get_jwt_header(user),
        )
        assert response.status_code == 400
        error = response.json()["detail"]["error"]
        assert error == "feedback_contact_requires_identity"

    async def test_without_consent_nothing_is_stored(
        self, client: AsyncClient, db: AsyncSession
    ):
        before = await db.scalar(select(Feedback.id).order_by(Feedback.id.desc()))
        response = await client.post("/api/v1/feedback", data=_form(consent="false"))
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_consent_required"
        after = await db.scalar(select(Feedback.id).order_by(Feedback.id.desc()))
        assert after == before

    @pytest.mark.parametrize(
        "kind", ["feedback", "bug", "feature_request", "other"]
    )
    async def test_every_kind_is_accepted(self, client: AsyncClient, kind: str):
        response = await client.post("/api/v1/feedback", data=_form(kind=kind))
        assert response.status_code == 201, response.text

    async def test_unknown_kind_is_refused(self, client: AsyncClient):
        response = await client.post("/api/v1/feedback", data=_form(kind="complaint"))
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_invalid_kind"

    async def test_rating_is_stored_for_feedback(
        self, client: AsyncClient, db: AsyncSession
    ):
        response = await client.post(
            "/api/v1/feedback", data=_form(kind="feedback", rating="4")
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.rating == 4

    async def test_rating_on_another_kind_is_refused(self, client: AsyncClient):
        """Rejected rather than dropped: a rating on a bug report means the client
        and the route disagree about the form, and that should be visible."""
        response = await client.post(
            "/api/v1/feedback", data=_form(kind="bug", rating="4")
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_rating_not_allowed"

    @pytest.mark.parametrize("rating", ["0", "6"])
    async def test_rating_out_of_range_is_refused(
        self, client: AsyncClient, rating: str
    ):
        response = await client.post("/api/v1/feedback", data=_form(rating=rating))
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_invalid_rating"

    async def test_blank_message_is_refused(self, client: AsyncClient):
        response = await client.post("/api/v1/feedback", data=_form(message="   "))
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_message_empty"

    async def test_overlong_message_is_refused(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/feedback",
            data=_form(message="x" * (settings.FEEDBACK_MESSAGE_MAX_LENGTH + 1)),
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_message_too_long"

    async def test_locale_is_recorded_from_accept_language(
        self, client: AsyncClient, db: AsyncSession
    ):
        """Whoever answers a report needs to know which language to answer in."""
        response = await client.post(
            "/api/v1/feedback", data=_form(), headers={"Accept-Language": "de"}
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        assert feedback.locale == "de"

    async def test_too_many_files_is_refused(self, client: AsyncClient):
        files = [
            ("files", (f"shot{i}.png", _make_test_png(), "image/png"))
            for i in range(settings.FEEDBACK_MEDIA_MAX_FILES + 1)
        ]
        response = await client.post("/api/v1/feedback", data=_form(), files=files)
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_media_too_many_files"

    async def test_unsupported_file_type_is_refused(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/feedback",
            data=_form(),
            files=[("files", ("log.txt", b"not an image", "text/plain"))],
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "feedback_media_invalid_type"


class TestFeedbackMedia:
    async def test_screenshot_of_any_shape_is_accepted_and_fetchable(
        self, client: AsyncClient, db: AsyncSession, media_client: AsyncClient
    ):
        """The single most important difference from post media: 9:16 is neither
        POST_MEDIA_LANDSCAPE_RATIO nor POST_MEDIA_PORTRAIT_RATIO, and a phone
        screenshot is exactly that shape. Rejecting it would make the bug-report
        form useless for reporting anything visible.
        """
        response = await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug"),
            files=[("files", ("shot.png", _make_test_png(90, 160), "image/png"))],
        )
        assert response.status_code == 201, response.text

        feedback = await db.get(Feedback, response.json()["id"])
        await db.refresh(feedback, attribute_names=["media"])
        assert len(feedback.media) == 1
        item = feedback.media[0]
        assert item.media_type == "image"
        # Shape preserved, not cropped to a feed ratio.
        assert (item.width, item.height) == (90, 160)

        fetched = await media_client.get(item.url)
        assert fetched.status_code == 200
        assert Image.open(io.BytesIO(fetched.content)).size == (90, 160)

    async def test_png_screenshot_stays_png(
        self, client: AsyncClient, db: AsyncSession
    ):
        """A PNG screenshot re-encoded as JPEG picks up ringing around exactly the
        text and UI edges the reporter is pointing at."""
        response = await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug"),
            files=[("files", ("shot.png", _make_test_png(), "image/png"))],
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        await db.refresh(feedback, attribute_names=["media"])
        assert feedback.media[0].content_type == "image/png"

    async def test_exif_is_stripped(
        self, client: AsyncClient, db: AsyncSession, media_client: AsyncClient
    ):
        """A submission can be anonymous, so GPS in a photo of a broken screen
        would undo exactly the anonymity that was chosen."""
        response = await client.post(
            "/api/v1/feedback",
            data=_form(),
            files=[
                ("files", ("photo.png", _make_test_png(with_exif=True), "image/png"))
            ],
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        await db.refresh(feedback, attribute_names=["media"])

        fetched = await media_client.get(feedback.media[0].url)
        stored = Image.open(io.BytesIO(fetched.content))
        assert not dict(stored.getexif())

    async def test_screen_recording_keeps_its_shape_and_gets_a_poster(
        self, client: AsyncClient, db: AsyncSession, media_client: AsyncClient
    ):
        """16:9 survives: cropping a screen recording to 4:5 would cut off the
        sides, which is where the bug usually is."""
        response = await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug"),
            files=[
                ("files", ("clip.mp4", _make_test_video(1.0, "160x90"), "video/mp4"))
            ],
        )
        assert response.status_code == 201, response.text
        feedback = await db.get(Feedback, response.json()["id"])
        await db.refresh(feedback, attribute_names=["media"])
        item = feedback.media[0]
        assert item.media_type == "video"
        assert item.content_type == "video/mp4"
        # 16:9 in, 16:9 out - no center crop to a feed ratio.
        assert item.width / item.height == pytest.approx(160 / 90, rel=0.05)
        assert item.poster_url is not None
        assert (await media_client.get(item.poster_url)).status_code == 200

    async def test_several_attachments_are_all_stored(
        self, client: AsyncClient, db: AsyncSession
    ):
        response = await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug"),
            files=[
                ("files", ("a.png", _make_test_png(90, 160), "image/png")),
                ("files", ("b.png", _make_test_png(160, 90), "image/png")),
            ],
        )
        assert response.status_code == 201
        feedback = await db.get(Feedback, response.json()["id"])
        await db.refresh(feedback, attribute_names=["media"])
        assert [(m.width, m.height) for m in feedback.media] == [(90, 160), (160, 90)]


class TestListFeedback:
    async def test_requires_superuser(self, client: AsyncClient, create_user):
        user: User = await create_user()
        assert (await client.get("/api/v1/feedback")).status_code == 401
        response = await client.get("/api/v1/feedback", headers=get_jwt_header(user))
        assert response.status_code == 403

    async def test_superuser_sees_submissions_newest_first(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        admin: User = await create_user()
        admin.is_superuser = True
        db.add(admin)
        await db.commit()

        await client.post("/api/v1/feedback", data=_form(message="older one"))
        await client.post(
            "/api/v1/feedback",
            data=_form(kind="bug", message="newer one"),
            files=[("files", ("shot.png", _make_test_png(), "image/png"))],
        )

        response = await client.get(
            "/api/v1/feedback", headers=get_jwt_header(admin)
        )
        assert response.status_code == 200
        rows = response.json()
        assert rows[0]["message"] == "newer one"
        assert rows[0]["media"][0]["url"].startswith("http")
        assert rows[0]["user_agreed_data_saving_at"] is not None
        assert rows[0]["status"] == "new"

    async def test_filtering_by_kind(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        admin: User = await create_user()
        admin.is_superuser = True
        db.add(admin)
        await db.commit()

        await client.post("/api/v1/feedback", data=_form(kind="feature_request"))
        response = await client.get(
            "/api/v1/feedback?kind=feature_request", headers=get_jwt_header(admin)
        )
        assert response.status_code == 200
        assert {row["kind"] for row in response.json()} == {"feature_request"}


class TestFeedbackRateLimit:
    async def test_signed_out_submissions_are_limited_by_ip(
        self, client: AsyncClient, monkeypatch
    ):
        """Signed out there is no user id to key on, so the budget falls back to
        the client IP - `request.client.host`, never a spoofable X-Forwarded-For.
        """
        monkeypatch.setattr(settings, "FEEDBACK_RATE_LIMIT", 2)
        for _ in range(2):
            assert (
                await client.post("/api/v1/feedback", data=_form())
            ).status_code == 201

        response = await client.post("/api/v1/feedback", data=_form())
        assert response.status_code == 429
        assert response.json()["detail"]["error"] == "rate_limited"
        assert "Retry-After" in response.headers

    async def test_forwarded_for_header_does_not_reset_the_budget(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "FEEDBACK_RATE_LIMIT", 1)
        assert (await client.post("/api/v1/feedback", data=_form())).status_code == 201
        response = await client.post(
            "/api/v1/feedback",
            data=_form(),
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert response.status_code == 429

    async def test_superusers_are_exempt(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        admin: User = await create_user()
        admin.is_superuser = True
        db.add(admin)
        await db.commit()

        monkeypatch.setattr(settings, "FEEDBACK_RATE_LIMIT", 1)
        for _ in range(3):
            response = await client.post(
                "/api/v1/feedback",
                data=_form(),
                headers=get_jwt_header(admin),
            )
            assert response.status_code == 201
