"""Google sign-in.

`verify_google_id_token` is stubbed throughout: these tests are about what we do
with a verified identity, and the verification itself (signature, issuer, audience,
email_verified) belongs to `google-auth` plus tests/core/test_google_oauth.py. The
stub is patched onto `app.api.google_auth`, where the route imported the name.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.api import google_auth
from app.core.config import settings
from app.core.google_oauth import GoogleIdentity
from app.models.user import User
from tests.utils import generate_random_string, get_jwt_header

GOOGLE_URL = settings.API_PATH + "/auth/google"
LINK_URL = settings.API_PATH + "/auth/google/link"


@pytest.fixture(autouse=True)
def google_enabled(monkeypatch):
    """Every test here assumes the feature is on; the one test that cares about it
    being off turns it back off itself."""
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_ENABLED", True)


@pytest.fixture
def stub_google(monkeypatch):
    """Make the next `/auth/google` call see a given Google identity."""

    def inner(email: str, subject: str | None = None, name: str | None = None):
        identity = GoogleIdentity(
            subject=subject or generate_random_string(21),
            email=email,
            name=name,
        )

        async def fake_verify(token: str) -> GoogleIdentity:
            return identity

        monkeypatch.setattr(google_auth, "verify_google_id_token", fake_verify)
        return identity

    return inner


@pytest.fixture
def link_body(default_password: str) -> dict:
    """`POST /auth/google/link` body for an account made by `create_user`."""
    return {"id_token": "tok", "current_password": default_password}


def random_email() -> str:
    return f"{generate_random_string(20)}@{generate_random_string(10)}.com"


class TestNewGoogleUser:
    async def test_signup_creates_a_verified_user_with_a_username(
        self, client: AsyncClient, stub_google
    ):
        email = random_email()
        # Randomized, not a fixed "Ada Lovelace": the test database persists across
        # runs (see CLAUDE.md - rollback is best-effort, not isolation), so a fixed
        # name would be taken by the previous run and come back suffixed.
        first, last = generate_random_string(6), generate_random_string(8)
        stub_google(email, name=f"{first} {last}")

        resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        assert resp.json()["token_type"] == "bearer"

        me = await client.get(
            settings.API_PATH + "/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["email"] == email
        assert body["auth_provider"] == "google"
        # Google vouched for the address, so no verification code is ever involved.
        assert body["is_verified"] is True
        # Derived from the Google display name (slugified), not left NULL for the
        # client to stumble over.
        assert body["username"] == f"{first}{last}"

    async def test_username_falls_back_to_the_email_local_part(
        self, client: AsyncClient, stub_google
    ):
        local = generate_random_string(12)
        stub_google(f"{local}@example.com", name=None)

        resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert resp.status_code == 200, resp.text

        me = await client.get(
            settings.API_PATH + "/users/me",
            headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
        )
        assert me.json()["username"] == local

    async def test_colliding_usernames_get_a_suffix(
        self, client: AsyncClient, stub_google
    ):
        name = generate_random_string(15)
        usernames = []
        for _ in range(2):
            stub_google(random_email(), name=name)
            resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
            assert resp.status_code == 200, resp.text
            me = await client.get(
                settings.API_PATH + "/users/me",
                headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
            )
            usernames.append(me.json()["username"])

        assert usernames[0] == name
        assert usernames[1] == f"{name}2"

    async def test_returning_user_signs_in_without_creating_a_second_account(
        self, client: AsyncClient, stub_google
    ):
        email = random_email()
        subject = generate_random_string(21)

        stub_google(email, subject=subject)
        first = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert first.status_code == 200, first.text

        stub_google(email, subject=subject)
        second = await client.post(GOOGLE_URL, json={"id_token": "tok2"})
        assert second.status_code == 200, second.text

        ids = []
        for resp in (first, second):
            me = await client.get(
                settings.API_PATH + "/users/me",
                headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
            )
            ids.append(me.json()["id"])
        assert ids[0] == ids[1]


class TestUpgradeFromPassword:
    async def test_first_attempt_asks_for_confirmation(
        self, client: AsyncClient, create_user, stub_google
    ):
        user: User = await create_user()
        stub_google(user.email)

        resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "google_link_required"
        # The client puts the address in the confirmation dialog.
        assert detail["email"] == user.email

    async def test_refused_confirmation_leaves_the_account_untouched(
        self, client: AsyncClient, create_user, stub_google, default_password: str
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok"})

        # Declining the dialog means never sending the second request, so the
        # password account must still be exactly that.
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert resp.status_code == 200, resp.text

    async def test_confirmed_upgrade_links_and_destroys_the_password(
        self, client: AsyncClient, create_user, stub_google, default_password: str
    ):
        user: User = await create_user()
        stub_google(user.email)

        resp = await client.post(
            GOOGLE_URL, json={"id_token": "tok", "link_existing": True}
        )
        assert resp.status_code == 200, resp.text

        me = await client.get(
            settings.API_PATH + "/users/me",
            headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
        )
        assert me.json()["auth_provider"] == "google"
        # Same account, not a second one.
        assert me.json()["id"] == str(user.id)

        # Rule 2: the old password is gone for good, and the refusal says why.
        login = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert login.status_code == 400
        assert login.json()["detail"]["error"] == "login_use_google"

    async def test_upgrade_verifies_an_unverified_account(
        self, client: AsyncClient, create_user, stub_google
    ):
        user: User = await create_user(is_verified=False)
        stub_google(user.email)

        resp = await client.post(
            GOOGLE_URL, json={"id_token": "tok", "link_existing": True}
        )
        assert resp.status_code == 200, resp.text

        me = await client.get(
            settings.API_PATH + "/users/me",
            headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
        )
        # Google confirmed the address, so the pending code is moot.
        assert me.json()["is_verified"] is True

    async def test_address_held_by_a_different_google_identity_is_refused(
        self, client: AsyncClient, create_user, stub_google
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok", "link_existing": True})

        # Same address, different Google `sub` — attaching a second link would give
        # two Google accounts access to one account here.
        stub_google(user.email, subject="a-different-google-subject")
        resp = await client.post(
            GOOGLE_URL, json={"id_token": "tok", "link_existing": True}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_account_mismatch"


class TestNoWayBackToPasswordAuth:
    async def test_change_password_is_refused(
        self, client: AsyncClient, create_user, stub_google, default_password: str
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok", "link_existing": True})

        resp = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={
                "current_password": default_password,
                "new_password": "something-new-1",
            },
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_account_no_password"

    async def test_email_change_is_still_allowed(
        self, client: AsyncClient, create_user, stub_google
    ):
        """`email` is the contact address, not the credential - the account is
        bound to Google by `sub` - so it stays as editable as on any account."""
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok", "link_existing": True})

        new_email = random_email()
        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"email": new_email},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == new_email
        assert resp.json()["auth_provider"] == "google"

    async def test_other_profile_fields_still_update(
        self, client: AsyncClient, create_user, stub_google
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok", "link_existing": True})

        resp = await client.patch(
            settings.API_PATH + "/users/me",
            json={"bio": "hello"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bio"] == "hello"

    async def test_registering_the_address_again_gets_the_ordinary_error(
        self, client: AsyncClient, create_user, stub_google
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(GOOGLE_URL, json={"id_token": "tok", "link_existing": True})

        resp = await client.post(
            settings.API_PATH + "/auth/register",
            json={
                "email": user.email,
                "password": "whatever-1234",
                "username": generate_random_string(12),
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "register_user_already_exists"

    async def test_password_account_reports_no_google_email(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert resp.json()["auth_provider"] == "password"
        assert resp.json()["google_email"] is None

    async def test_wrong_password_on_a_password_account_is_unchanged(
        self, client: AsyncClient, create_user
    ):
        """The new `login_use_google` branch must not swallow the ordinary case."""
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": "not-the-password"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"

    async def test_unknown_email_is_unchanged(self, client: AsyncClient):
        resp = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": random_email(), "password": "irrelevant"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "login_bad_credentials"


class TestLinkFromSettings:
    async def test_signed_in_user_can_link(
        self,
        client: AsyncClient,
        create_user,
        stub_google,
        default_password: str,
        link_body,
    ):
        user: User = await create_user()
        stub_google(user.email)

        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auth_provider"] == "google"

        login = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert login.json()["detail"]["error"] == "login_use_google"

    async def test_linking_a_different_address_keeps_the_account_email(
        self,
        client: AsyncClient,
        create_user,
        stub_google,
        default_password: str,
        link_body,
    ):
        """The point of the settings path: sign in with a personal Google account
        while the account keeps the address it already receives mail on."""
        user: User = await create_user()
        original_email = user.email
        stub_google(random_email())

        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auth_provider"] == "google"
        # Contact address untouched...
        assert resp.json()["email"] == original_email
        # ...and the sign-in address is reported separately, so the UI can show
        # which Google account actually gets you in.
        assert resp.json()["google_email"] != original_email
        assert "@" in resp.json()["google_email"]

        # And it is a real upgrade: the password is gone either way.
        login = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": original_email, "password": default_password},
        )
        assert login.json()["detail"]["error"] == "login_use_google"

    async def test_linking_a_different_address_does_not_verify_the_account(
        self, client: AsyncClient, create_user, stub_google, link_body
    ):
        """Google vouched for *its* address. Marking a different, unproven address
        verified would be a free pass around email verification."""
        user: User = await create_user(is_verified=False)
        stub_google(random_email())

        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_verified"] is False

    async def test_linking_the_same_address_does_verify_the_account(
        self, client: AsyncClient, create_user, stub_google, link_body
    ):
        user: User = await create_user(is_verified=False)
        stub_google(user.email)

        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_verified"] is True

    async def test_a_linked_account_signs_in_by_subject_not_email(
        self, client: AsyncClient, create_user, stub_google, link_body
    ):
        """The invariant the whole decoupling rests on: after linking a Google
        account with a different address, `POST /auth/google` still resolves to
        this account rather than creating a second one for the Google address."""
        user: User = await create_user()
        google_email = random_email()
        subject = generate_random_string(21)
        stub_google(google_email, subject=subject)
        await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )

        stub_google(google_email, subject=subject)
        resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert resp.status_code == 200, resp.text
        me = await client.get(
            settings.API_PATH + "/users/me",
            headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
        )
        assert me.json()["id"] == str(user.id)
        assert me.json()["email"] == user.email

    async def test_linking_twice_is_refused(
        self, client: AsyncClient, create_user, stub_google, link_body
    ):
        user: User = await create_user()
        stub_google(user.email)
        await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )

        stub_google(user.email)
        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(user)
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_already_linked"

    async def test_google_identity_already_used_elsewhere_is_refused(
        self, client: AsyncClient, create_user, stub_google, link_body
    ):
        subject = generate_random_string(21)
        stub_google(random_email(), subject=subject)
        await client.post(GOOGLE_URL, json={"id_token": "tok"})

        # A second, unrelated account tries to claim the same Google identity.
        other: User = await create_user()
        stub_google(other.email, subject=subject)
        resp = await client.post(
            LINK_URL, json=link_body, headers=get_jwt_header(other)
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_account_in_use"

    async def test_missing_password_is_refused(
        self, client: AsyncClient, create_user, stub_google
    ):
        """The token-theft path: a bearer token alone must not be enough to
        destroy the password and bind somebody else's Google account."""
        user: User = await create_user()
        stub_google(random_email())

        resp = await client.post(
            LINK_URL, json={"id_token": "tok"}, headers=get_jwt_header(user)
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_link_password_required"

    async def test_wrong_password_leaves_the_account_untouched(
        self, client: AsyncClient, create_user, stub_google, default_password: str
    ):
        user: User = await create_user()
        stub_google(random_email())

        resp = await client.post(
            LINK_URL,
            json={"id_token": "tok", "current_password": "not-the-password"},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_link_wrong_password"

        me = await client.get(
            settings.API_PATH + "/users/me", headers=get_jwt_header(user)
        )
        assert me.json()["auth_provider"] == "password"
        login = await client.post(
            settings.API_PATH + "/auth/jwt/login",
            data={"username": user.email, "password": default_password},
        )
        assert login.status_code == 200, login.text

    async def test_wrong_password_is_checked_before_the_google_token(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        async def must_not_verify(token: str) -> GoogleIdentity:
            raise AssertionError("Google token verified before the password")

        monkeypatch.setattr(google_auth, "verify_google_id_token", must_not_verify)
        user: User = await create_user()

        resp = await client.post(
            LINK_URL,
            json={"id_token": "tok", "current_password": "not-the-password"},
            headers=get_jwt_header(user),
        )
        assert resp.json()["detail"]["error"] == "google_link_wrong_password"

    async def test_shares_the_change_password_budget(
        self, client: AsyncClient, create_user, stub_google, monkeypatch
    ):
        """Same secret as change-password and delete-account, so guesses must not
        get a fresh allowance by switching to this route."""
        monkeypatch.setattr(settings, "PASSWORD_CHANGE_RATE_LIMIT", 1)
        user: User = await create_user()
        stub_google(random_email())
        wrong = {"id_token": "tok", "current_password": "not-the-password"}

        first = await client.post(LINK_URL, json=wrong, headers=get_jwt_header(user))
        assert first.json()["detail"]["error"] == "google_link_wrong_password"
        second = await client.post(
            settings.API_PATH + "/auth/change-password",
            json={"current_password": "x", "new_password": "y"},
            headers=get_jwt_header(user),
        )
        assert second.status_code == 429

    async def test_requires_authentication(self, client: AsyncClient):
        resp = await client.post(LINK_URL, json={"id_token": "tok"})
        assert resp.status_code == 401


class TestFeatureFlag:
    async def test_routes_refuse_when_disabled(
        self, client: AsyncClient, monkeypatch, stub_google
    ):
        monkeypatch.setattr(settings, "GOOGLE_OAUTH_ENABLED", False)
        stub_google(random_email())

        resp = await client.post(GOOGLE_URL, json={"id_token": "tok"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "google_oauth_disabled"

    async def test_config_advertises_the_flag(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/config")
        assert resp.status_code == 200
        assert resp.json()["google_oauth_enabled"] is True
