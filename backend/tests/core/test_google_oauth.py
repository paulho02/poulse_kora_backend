"""ID token verification.

`google-auth` owns signature/issuer/expiry checking and is not re-tested here; what
is tested is everything layered on top of it, above all the `email_verified`
requirement — account linking matches on email, so accepting an unverified claim
would be an account-takeover path.
"""

import pytest
from fastapi import HTTPException

from app.core import google_oauth
from app.core.config import settings

CLIENT_ID = "test-client.apps.googleusercontent.com"


def valid_claims(**overrides):
    claims = {
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "ada@example.com",
        "email_verified": True,
        "name": "Ada Lovelace",
    }
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def accepted_audience(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_IDS", [CLIENT_ID])


@pytest.fixture
def stub_claims(monkeypatch):
    def inner(claims):
        def fake_verify(token, request, audience):
            # `audience=None` is what lets us accept any of GOOGLE_CLIENT_IDS
            # ourselves; if that ever changes, this assertion fails loudly rather
            # than silently narrowing which tokens are accepted.
            assert audience is None
            return claims

        monkeypatch.setattr(
            google_oauth.google_id_token, "verify_oauth2_token", fake_verify
        )

    return inner


async def test_valid_token_yields_the_identity(stub_claims):
    stub_claims(valid_claims())
    identity = await google_oauth.verify_google_id_token("tok")
    assert identity.subject == "1234567890"
    assert identity.email == "ada@example.com"
    assert identity.name == "Ada Lovelace"


async def test_unverified_email_is_rejected(stub_claims):
    stub_claims(valid_claims(email_verified=False))
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "google_email_unverified"


async def test_missing_email_verified_claim_is_rejected(stub_claims):
    claims = valid_claims()
    del claims["email_verified"]
    stub_claims(claims)
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.detail["error"] == "google_email_unverified"


async def test_token_for_another_client_is_rejected(stub_claims):
    """A perfectly valid Google token minted for somebody else's OAuth client —
    nothing google-auth checks would catch this."""
    stub_claims(valid_claims(aud="someone-elses.apps.googleusercontent.com"))
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "google_invalid_id_token"


async def test_no_configured_audience_accepts_nothing(stub_claims, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_IDS", [])
    stub_claims(valid_claims())
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.detail["error"] == "google_invalid_id_token"


async def test_name_is_optional(stub_claims):
    claims = valid_claims()
    del claims["name"]
    stub_claims(claims)
    identity = await google_oauth.verify_google_id_token("tok")
    assert identity.name is None


async def test_bad_token_is_rejected(monkeypatch):
    def fake_verify(token, request, audience):
        raise ValueError("Token expired")

    monkeypatch.setattr(
        google_oauth.google_id_token, "verify_oauth2_token", fake_verify
    )
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "google_invalid_id_token"


async def test_unreachable_google_is_a_503_not_a_bad_token(monkeypatch):
    """Kept distinct so the client says "try again" instead of telling the user
    their perfectly good token is invalid."""
    from google.auth.exceptions import TransportError

    def fake_verify(token, request, audience):
        raise TransportError("connection refused")

    monkeypatch.setattr(
        google_oauth.google_id_token, "verify_oauth2_token", fake_verify
    )
    with pytest.raises(HTTPException) as exc:
        await google_oauth.verify_google_id_token("tok")
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "google_verification_unavailable"
