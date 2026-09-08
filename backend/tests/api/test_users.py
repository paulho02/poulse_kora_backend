import io
from collections.abc import Callable
from unittest.mock import AsyncMock

from httpx import AsyncClient
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email_verification as ev
from app.core.config import settings
from app.deps import users as users_module
from app.feed import service
from app.models.user import User
from tests.utils import generate_random_string, get_jwt_header, make_test_png


class TestRegister:
    async def test_new_account_starts_with_the_starting_token_grant(
        self, client: AsyncClient, redis: Redis
    ):
        # Registration commits straight to Postgres (fastapi-users' own session,
        # outside the `auto_rollback` fixture's transaction), so a fixed email
        # would collide with a leftover row the moment this test runs twice —
        # random, like `create_user`'s fixture, to actually get a fresh account.
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "Sup3rSecret!23",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]

        balance = await service.token_balance(redis, user_id)
        assert balance == settings.FEED_STARTING_TOKENS

    async def test_new_account_has_not_completed_onboarding(
        self, client: AsyncClient
    ):
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "Sup3rSecret!23",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["onboarding_completed"] is False

    async def test_duplicate_email_is_a_structured_error(self, client: AsyncClient):
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        payload = {
            "email": email,
            "password": "Sup3rSecret!23",
            "username": generate_random_string(15),
        }
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 201, resp.text

        payload["username"] = generate_random_string(15)
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "register_user_already_exists"

    async def test_duplicate_username_is_a_structured_error(self, client: AsyncClient):
        """`User.username` is unique, so this used to reach Postgres and come back
        as a bare 500 - the register form could only say "something went wrong"
        about the one field the user could have fixed themselves."""
        username = generate_random_string(15)
        payload = {
            "email": f"{generate_random_string(20)}@{generate_random_string(10)}.com",
            "password": "Sup3rSecret!23",
            "username": username,
        }
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 201, resp.text

        payload["email"] = (
            f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        )
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "username_taken"

    async def test_username_race_lost_at_the_constraint_is_the_same_error(
        self, client: AsyncClient, monkeypatch
    ):
        """The pre-check is a probe, not a reservation: two signups claiming one
        name in the same instant both pass it and one loses at the unique index.
        Forcing the probe to always answer "free" is what that race looks like from
        here, and the answer has to be the same refusal rather than a 500."""
        username = generate_random_string(15)
        payload = {
            "email": f"{generate_random_string(20)}@{generate_random_string(10)}.com",
            "password": "Sup3rSecret!23",
            "username": username,
        }
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 201, resp.text

        monkeypatch.setattr(
            users_module, "is_username_taken", AsyncMock(return_value=False)
        )
        payload["email"] = (
            f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        )
        resp = await client.post(settings.API_PATH + "/auth/register", json=payload)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "username_taken"

    async def test_weak_password_is_rejected_with_a_reason(
        self, client: AsyncClient, monkeypatch
    ):
        """REQUIRE_STRONG_PASSWORD defaults off (see app.core.config) - force it on
        to exercise the rejection path, see app.core.password_policy."""
        monkeypatch.setattr(settings, "REQUIRE_STRONG_PASSWORD", True)
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "weak",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "register_invalid_password"
        # "weak" is both too short and single-character-class against the
        # default PASSWORD_MIN_LENGTH/PASSWORD_MIN_CHARACTER_CLASSES, so both
        # structured violation codes should be present (see
        # app.core.password_policy.strength_violations).
        codes = {v["code"] for v in detail["reason"]}
        assert codes == {"password_too_short", "password_missing_variety"}

    async def test_weak_password_is_accepted_when_policy_off(
        self, client: AsyncClient
    ):
        """Off is the default - any password, however weak, must be accepted."""
        assert settings.REQUIRE_STRONG_PASSWORD is False
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "weak",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text

    async def test_verification_email_failure_does_not_fail_registration(
        self, client: AsyncClient, redis: Redis, monkeypatch
    ):
        """A transient SMTP failure while sending the first verification code must
        not turn into a 500 on `/auth/register` - the account is already created
        by that point, so the user would be stuck (a retry just hits
        `register_user_already_exists`). It also must not start the resend
        cooldown, since no email actually went out."""
        assert settings.REQUIRE_EMAIL_VERIFICATION is True
        monkeypatch.setattr(
            users_module, "send_email", AsyncMock(side_effect=OSError("smtp down"))
        )
        email = f"{generate_random_string(20)}@{generate_random_string(10)}.com"
        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": email,
                "password": "Sup3rSecret!23",
                "username": generate_random_string(15),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]
        assert await ev.resend_cooldown_remaining(redis, user_id) == 0


class TestLogin:
    async def test_wrong_password_is_a_structured_error(
        self, client: AsyncClient, create_user: Callable
    ):
        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": "definitely-not-it"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"

    async def test_unknown_email_is_the_same_structured_error(
        self, client: AsyncClient
    ):
        """Deliberately indistinguishable from a wrong password, to avoid leaking
        which emails have accounts."""
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={
                "username": f"{generate_random_string(20)}@nowhere.com",
                "password": "whatever123",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"

    async def test_unverified_user_can_still_log_in(
        self, client: AsyncClient, create_user: Callable, default_password: str
    ):
        """Login must succeed while unverified - the client needs the token to call
        the verification endpoints at all. See app.deps.users.CurrentVerifiedUser
        for where unverified users actually get blocked."""
        user = await create_user(is_verified=False)
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()


class TestListUsers:
    """GET /users - superuser-only, react-admin-style listing (app/api/users.py)."""

    async def test_not_logged_in(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/users")
        assert resp.status_code == 401

    async def test_non_superuser_rejected(
        self, client: AsyncClient, create_user: Callable
    ):
        user = await create_user()
        resp = await client.get(
            settings.API_PATH + "/users", headers=get_jwt_header(user)
        )
        assert resp.status_code == 403

    async def test_superuser_lists_users_with_content_range(
        self, client: AsyncClient, db: AsyncSession, create_user: Callable
    ):
        superuser: User = await create_user()
        superuser.is_superuser = True
        db.add(superuser)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/users",
            # A large limit: many users accumulate across the test session and the
            # endpoint applies no ordering, so the default page might not include
            # the one just created.
            params={"limit": 100_000},
            headers=get_jwt_header(superuser),
        )
        assert resp.status_code == 200, resp.text
        assert "Content-Range" in resp.headers
        ids = {u["id"] for u in resp.json()}
        assert str(superuser.id) in ids

    async def test_superuser_pagination(
        self, client: AsyncClient, db: AsyncSession, create_user: Callable
    ):
        superuser: User = await create_user()
        superuser.is_superuser = True
        db.add(superuser)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/users",
            params={"skip": 0, "limit": 1},
            headers=get_jwt_header(superuser),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 1
        start, rest = resp.headers["Content-Range"].split("-", 1)
        end, total = rest.split("/")
        assert start == "0"
        assert end == "1"
        assert int(total) >= 1


class TestUpdateMe:
    async def test_completing_onboarding_persists(self, client: AsyncClient, create_user):
        user = await create_user()
        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"onboarding_completed": True},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["onboarding_completed"] is True

        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert resp.json()["onboarding_completed"] is True


class TestUsernameConflicts:
    """A taken username is refused with a code the client can put under the field,
    on both writers (see app/deps/users.py: UserManager)."""

    async def _set_username(self, client: AsyncClient, user, username: str):
        return await client.patch(
            settings.API_PATH + "/users/me",
            json={"username": username},
            headers=get_jwt_header(user),
        )

    async def test_taking_someone_elses_username_is_a_structured_error(
        self, client: AsyncClient, create_user: Callable
    ):
        username = generate_random_string(15)
        holder = await create_user()
        assert (await self._set_username(client, holder, username)).status_code == 200

        other = await create_user()
        resp = await self._set_username(client, other, username)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "username_taken"

    async def test_refusal_leaves_the_account_usable(
        self, client: AsyncClient, create_user: Callable
    ):
        """The refusal must not poison the session or half-apply the PATCH: the
        next request has to work, and the old username has to still be there."""
        username = generate_random_string(15)
        holder = await create_user()
        assert (await self._set_username(client, holder, username)).status_code == 200

        other = await create_user()
        mine = generate_random_string(15)
        assert (await self._set_username(client, other, mine)).status_code == 200
        assert (await self._set_username(client, other, username)).status_code == 409

        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(other)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == mine

    async def test_race_lost_at_the_constraint_leaves_the_account_usable(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        """Same race as on register, on the UPDATE side - where the rollback also
        has to undo the username already assigned to the in-memory user."""
        username = generate_random_string(15)
        holder = await create_user()
        assert (await self._set_username(client, holder, username)).status_code == 200

        other = await create_user()
        mine = generate_random_string(15)
        assert (await self._set_username(client, other, mine)).status_code == 200

        monkeypatch.setattr(
            users_module, "is_username_taken", AsyncMock(return_value=False)
        )
        resp = await self._set_username(client, other, username)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "username_taken"

        monkeypatch.undo()
        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(other)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == mine

    async def test_resending_your_own_username_is_not_a_conflict(
        self, client: AsyncClient, create_user: Callable
    ):
        """The onboarding step PATCHes what the server already has when the user
        edits and then reverts - that must not read as a clash with themselves."""
        username = generate_random_string(15)
        user = await create_user()
        assert (await self._set_username(client, user, username)).status_code == 200
        resp = await self._set_username(client, user, username)
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == username


class TestProfilePicture:
    """PUT/DELETE /users/me/profile-picture (app/api/users.py).

    There is no longer a GET route: the image lives in the media bucket and
    `profile_picture_url` is a presigned link straight to it (see
    app/core/storage.py), so the bytes never pass through this backend.
    """

    async def test_upload_persists_and_is_fetchable(
        self, client: AsyncClient, media_client: AsyncClient, create_user
    ):
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(), "image/png")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        url = resp.json()["profile_picture_url"]
        assert url.startswith(str(settings.STORAGE_PUBLIC_ENDPOINT_URL))

        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert resp.json()["profile_picture_url"] == url

        # No bearer token: the signature in the URL is the whole authorization.
        fetched = await media_client.get(url)
        assert fetched.status_code == 200, fetched.text
        stored = Image.open(io.BytesIO(fetched.content))
        assert stored.size == (64, 64)
        # An opaque source is re-encoded to JPEG, and the *stored* type is what the
        # bucket serves - derived here, never the client's claim.
        assert stored.format == "JPEG"
        assert fetched.headers["content-type"] == "image/jpeg"

    async def test_replacing_a_picture_changes_the_url(
        self, client: AsyncClient, media_client: AsyncClient, create_user
    ):
        """Every upload writes a new object key. That is what invalidates client
        caches - the old code reused one URL per user, so a replaced picture kept
        showing the old face until the app was restarted."""
        user = await create_user()
        first = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(64, 64), "image/png")},
            headers=get_jwt_header(user),
        )
        first_url = first.json()["profile_picture_url"]

        second = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(32, 32), "image/png")},
            headers=get_jwt_header(user),
        )
        second_url = second.json()["profile_picture_url"]
        assert second_url != first_url
        replaced = await media_client.get(second_url)
        assert Image.open(io.BytesIO(replaced.content)).size == (32, 32)
        # The superseded object is deleted, so the old URL stops resolving even
        # while its signature is still within its validity window.
        assert (await media_client.get(first_url)).status_code == 404

    async def test_upload_rejects_bytes_that_are_not_an_image(
        self, client: AsyncClient, create_user
    ):
        """A truthful-looking header over arbitrary bytes used to be enough to park
        anything in storage under `image/png`. The decoder is the check now, not the
        claim."""
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", b"not-really-a-png", "image/png")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "profile_picture_invalid_type"

    async def test_upload_strips_metadata(
        self, client: AsyncClient, media_client: AsyncClient, create_user
    ):
        """An avatar is not anonymous the way a post can be, so this is not the
        deanonymization guard post media needs - it is a location leak the user never
        opted into, on a URL that is now shareable."""
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(with_exif=True), "image/png")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        fetched = await media_client.get(resp.json()["profile_picture_url"])
        assert Image.open(io.BytesIO(fetched.content)).getexif() == {}

    async def test_upload_downscales_an_oversized_image(
        self, client: AsyncClient, media_client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "PROFILE_PICTURE_MAX_DIMENSION_PX", 32)
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(200, 200), "image/png")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        fetched = await media_client.get(resp.json()["profile_picture_url"])
        assert Image.open(io.BytesIO(fetched.content)).size == (32, 32)

    async def test_upload_rejects_disallowed_content_type(
        self, client: AsyncClient, create_user
    ):
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.txt", b"not-an-image", "text/plain")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "profile_picture_invalid_type"

    async def test_upload_rejects_oversized_file(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "PROFILE_PICTURE_MAX_BYTES", 10)
        user = await create_user()
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", b"this-is-way-too-large", "image/png")},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "profile_picture_too_large"

    async def test_delete_clears_picture_and_removes_the_object(
        self, client: AsyncClient, media_client: AsyncClient, create_user
    ):
        user = await create_user()
        uploaded = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(), "image/png")},
            headers=get_jwt_header(user),
        )
        url = uploaded.json()["profile_picture_url"]

        resp = await client.delete(
            settings.API_PATH + "/users/me/profile-picture",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_picture_url"] is None
        assert (await media_client.get(url)).status_code == 404

    async def test_no_picture_means_no_url(self, client: AsyncClient, create_user):
        user = await create_user()
        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_picture_url"] is None

    async def test_upload_not_logged_in(self, client: AsyncClient):
        resp = await client.put(
            settings.API_PATH + "/users/me/profile-picture",
            files={"file": ("avatar.png", make_test_png(), "image/png")},
        )
        assert resp.status_code == 401
