"""The budgets on the signed-out writers (login, register) and on an email change,
plus the identity every signed-out budget keys on behind a proxy.

See app/deps/rate_limit.py for why login spends two budgets and why registration
and an email change are budgeted at all (both send mail to an address the caller
picked). The feed budgets have their own tests next to their routes.
"""

from collections.abc import Callable

from httpx import AsyncClient

from app.core.config import settings
from app.deps import rate_limit as rate_limit_module
from tests.utils import generate_random_string, get_jwt_header


def _random_email() -> str:
    return f"{generate_random_string(20)}@{generate_random_string(10)}.com"


async def _login(client: AsyncClient, email: str, password: str = "not-it-12345"):
    return await client.post(
        settings.API_PATH + "/auth/jwt/login",
        data={"username": email, "password": password},
    )


async def _register(client: AsyncClient):
    return await client.post(
        settings.API_PATH + "/auth/register",
        json={
            "email": _random_email(),
            "password": generate_random_string(24),
            "username": generate_random_string(15),
        },
    )


class TestLoginRateLimit:
    async def test_per_account_budget_bounds_guesses_at_one_password(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_ACCOUNT", 2)
        user = await create_user()
        for _ in range(2):
            assert (await _login(client, user.email)).status_code == 400

        resp = await _login(client, user.email)
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"
        assert "Retry-After" in resp.headers

    async def test_per_account_budget_is_case_insensitive(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        """`Alice@` and `alice@` are one account, so they draw on one budget."""
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_ACCOUNT", 1)
        user = await create_user()
        assert (await _login(client, user.email)).status_code == 400
        assert (await _login(client, user.email.upper())).status_code == 429

    async def test_per_account_budget_does_not_need_the_account_to_exist(
        self, client: AsyncClient, monkeypatch
    ):
        """Unknown emails cost a dummy argon2 hash too, so they are budgeted the
        same - and answering differently would disclose which emails exist."""
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_ACCOUNT", 1)
        email = _random_email()
        assert (await _login(client, email)).status_code == 400
        assert (await _login(client, email)).status_code == 429

    async def test_per_ip_budget_bounds_attempts_across_accounts(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_IP", 2)
        for _ in range(2):
            assert (await _login(client, _random_email())).status_code == 400
        assert (await _login(client, _random_email())).status_code == 429

    async def test_a_successful_login_spends_a_slot_too(
        self,
        client: AsyncClient,
        create_user: Callable,
        default_password: str,
        monkeypatch,
    ):
        """The budget is on attempts, not failures: the cost being bounded is the
        hash, which a correct password pays for as well."""
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_ACCOUNT", 1)
        user = await create_user()
        assert (await _login(client, user.email, default_password)).status_code == 200
        assert (await _login(client, user.email, default_password)).status_code == 429

    async def test_zero_disables_each_half(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_IP", 0)
        monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_ACCOUNT", 0)
        user = await create_user()
        for _ in range(5):
            assert (await _login(client, user.email)).status_code == 400


class TestRegisterRateLimit:
    async def test_registrations_are_limited_per_ip(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "REGISTER_RATE_LIMIT", 1)
        assert (await _register(client)).status_code == 201
        resp = await _register(client)
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"

    async def test_a_refused_registration_creates_nothing(
        self, client: AsyncClient, monkeypatch
    ):
        """The dependency runs before the handler, so the second attempt never
        reaches the user manager - its email is still free afterwards."""
        monkeypatch.setattr(settings, "REGISTER_RATE_LIMIT", 1)
        assert (await _register(client)).status_code == 201
        email = _random_email()
        payload = {
            "email": email,
            "password": generate_random_string(24),
            "username": generate_random_string(15),
        }
        assert (
            await client.post(settings.API_PATH + "/auth/register", json=payload)
        ).status_code == 429
        monkeypatch.setattr(settings, "REGISTER_RATE_LIMIT", 0)
        assert (
            await client.post(settings.API_PATH + "/auth/register", json=payload)
        ).status_code == 201


class TestForgotPasswordRateLimit:
    """The account-keyed half and its enumeration-safety property have their own
    tests next to the route (tests/api/test_password_reset.py); this only pins
    the per-IP half and the "0 disables it" convention every budget shares."""

    async def test_per_ip_budget_bounds_requests_across_addresses(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "PASSWORD_RESET_REQUEST_RATE_LIMIT_PER_IP", 2)
        for _ in range(2):
            resp = await client.post(
                settings.API_PATH + "/auth/forgot-password",
                json={"email": _random_email()},
            )
            assert resp.status_code == 200
        resp = await client.post(
            settings.API_PATH + "/auth/forgot-password",
            json={"email": _random_email()},
        )
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"

    async def test_zero_disables_each_half(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_RESET_REQUEST_RATE_LIMIT_PER_IP", 0)
        monkeypatch.setattr(
            settings, "PASSWORD_RESET_REQUEST_RATE_LIMIT_PER_ACCOUNT", 0
        )
        email = _random_email()
        for _ in range(5):
            resp = await client.post(
                settings.API_PATH + "/auth/forgot-password", json={"email": email}
            )
            assert resp.status_code == 200


class TestEmailChangeRateLimit:
    async def _patch(self, client: AsyncClient, user, **body):
        return await client.patch(
            settings.API_PATH + "/users/me", json=body, headers=get_jwt_header(user)
        )

    async def test_email_changes_are_limited_per_account(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        monkeypatch.setattr(settings, "EMAIL_CHANGE_RATE_LIMIT", 1)
        user = await create_user()
        assert (
            await self._patch(client, user, email=_random_email())
        ).status_code == 200
        resp = await self._patch(client, user, email=_random_email())
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"

    async def test_other_profile_writes_are_not_budgeted(
        self, client: AsyncClient, create_user: Callable, monkeypatch
    ):
        """Only a write that actually changes the address sends mail, so only
        that one spends a slot - re-sending the same address included."""
        monkeypatch.setattr(settings, "EMAIL_CHANGE_RATE_LIMIT", 1)
        user = await create_user()
        new_email = _random_email()
        assert (await self._patch(client, user, email=new_email)).status_code == 200
        assert (await self._patch(client, user, bio="hello")).status_code == 200
        assert (await self._patch(client, user, email=new_email)).status_code == 200


class TestSignedOutIdentityBehindProxy:
    """Behind Railway's edge the socket peer is the proxy for every request, so a
    budget keyed on it was one budget for every signed-out caller on the
    deployment. The identity is the *rightmost* `X-Forwarded-For` entry there -
    the one the trusted hop wrote - and never the leftmost, which is the caller's
    to invent (see app/core/request_logging.py: caller_address)."""

    async def test_rightmost_forwarded_entry_is_the_identity(
        self, client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(rate_limit_module, "_BEHIND_PROXY", True)
        monkeypatch.setattr(settings, "FEEDBACK_RATE_LIMIT", 1)
        form = {"kind": "feedback", "message": "hi", "consent": "true"}

        first = await client.post(
            "/api/v1/feedback",
            data=form,
            headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.7"},
        )
        assert first.status_code == 201

        # A different *leftmost* entry is the same caller as far as we are
        # concerned: the budget is spent.
        spoofed = await client.post(
            "/api/v1/feedback",
            data=form,
            headers={"X-Forwarded-For": "203.0.113.2, 198.51.100.7"},
        )
        assert spoofed.status_code == 429

        # A different rightmost entry is a different caller.
        other = await client.post(
            "/api/v1/feedback",
            data=form,
            headers={"X-Forwarded-For": "203.0.113.2, 198.51.100.8"},
        )
        assert other.status_code == 201

    async def test_forwarded_header_is_ignored_in_front_of_no_proxy(
        self, client: AsyncClient, monkeypatch
    ):
        """Not behind a proxy, the header is just text the caller sent - honouring
        it would make the budget the caller's to reset."""
        monkeypatch.setattr(rate_limit_module, "_BEHIND_PROXY", False)
        monkeypatch.setattr(settings, "FEEDBACK_RATE_LIMIT", 1)
        form = {"kind": "feedback", "message": "hi", "consent": "true"}
        assert (await client.post("/api/v1/feedback", data=form)).status_code == 201
        resp = await client.post(
            "/api/v1/feedback",
            data=form,
            headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"},
        )
        assert resp.status_code == 429
