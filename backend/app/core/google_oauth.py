"""Verification of Google ID tokens.

The client (google_sign_in on Android/web) hands us a Google-signed ID token rather
than an OAuth authorization code, so there is no redirect, no deep link and no
client secret on this side - all we do is check the signature and read the claims.

Verification is delegated to Google's own `google-auth` library: it fetches and
caches Google's signing certificates, and checks signature, issuer and expiry.
Everything this module adds on top is the part `google-auth` cannot know about -
which audiences *we* accept, and the email_verified requirement.
"""

from dataclasses import dataclass

from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger

log = get_logger(__name__)

#: Reused across calls so the library's certificate cache survives between requests
#: - a fresh transport would re-fetch Google's certs on every single sign-in.
_transport = google_requests.Request()


@dataclass(frozen=True)
class GoogleIdentity:
    """The only three claims we take off a verified ID token."""

    #: Google's stable per-account identifier (`sub`). This, not the email, is what
    #: `OAuthAccount.account_id` stores - an email can change, `sub` cannot.
    subject: str
    email: str
    #: Display name, used only as the seed for a generated username. Often absent.
    name: str | None


async def verify_google_id_token(token: str) -> GoogleIdentity:
    """Verify `token` and return the identity it asserts.

    Raises the API errors the client keys off: `google_invalid_id_token` (bad
    signature/issuer/expiry, or an audience we don't accept),
    `google_email_unverified`, and `google_verification_unavailable` when Google
    itself is unreachable.
    """
    try:
        # `audience=None` skips the library's own single-audience check so we can
        # accept any of GOOGLE_CLIENT_IDS below; signature, issuer and expiry are
        # still fully validated. Blocking (it may fetch Google's certs), hence the
        # threadpool.
        claims = await run_in_threadpool(
            google_id_token.verify_oauth2_token, token, _transport, None
        )
    except GoogleAuthError as exc:
        # Transport-level: we couldn't reach Google to fetch its certificates. Kept
        # distinct from the ValueError below so the client says "try again" instead
        # of telling the user their perfectly good token is invalid.
        log.warning("auth.google_verification_unavailable", error=str(exc))
        raise api_error(503, "google_verification_unavailable") from exc
    except ValueError as exc:
        # Deliberately not logged at error level or echoed back: an invalid token is
        # an ordinary client-side outcome (expired while the user hesitated), not a
        # server fault.
        log.info("auth.google_token_rejected", reason=str(exc))
        raise api_error(400, "google_invalid_id_token") from exc

    if claims.get("aud") not in settings.GOOGLE_CLIENT_IDS:
        # A token minted for somebody else's Google client is a perfectly valid
        # Google token, so nothing above would have caught it. Without this check any
        # app could trade its own users' tokens for accounts here.
        # Worth a WARNING with the value: either GOOGLE_CLIENT_IDS is missing an
        # entry after a client was added (a whole platform silently cannot sign
        # in), or someone is presenting another app's tokens here.
        log.warning(
            "auth.google_token_rejected",
            reason="unaccepted_aud",
            aud=claims.get("aud"),
        )
        raise api_error(400, "google_invalid_id_token")

    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise api_error(400, "google_invalid_id_token")

    # Security-critical, not a formality. Account linking matches on email, so an
    # unverified `email` claim would let whoever controls such a Google account take
    # over the existing password account that owns that address.
    if claims.get("email_verified") is not True:
        log.warning("auth.google_token_rejected", reason="email_unverified")
        raise api_error(400, "google_email_unverified")

    return GoogleIdentity(
        subject=str(subject), email=str(email), name=claims.get("name")
    )
