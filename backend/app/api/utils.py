from fastapi import APIRouter

from app.core.config import settings
from app.schemas.app_config import PublicAppConfig
from app.schemas.msg import Msg

router = APIRouter()


@router.get("/config", response_model=PublicAppConfig, status_code=200)
def get_public_config():
    """Feature flags the client needs before it can know how to behave: whether
    email verification is enforced (whether to show the code-entry step at all,
    since is_verified can legitimately stay false forever with the flag off) and
    the password rule to hint client-side. The server remains the source of truth
    either way - this only avoids the client guessing or hardcoding either."""
    return PublicAppConfig(
        require_email_verification=settings.REQUIRE_EMAIL_VERIFICATION,
        require_strong_password=settings.REQUIRE_STRONG_PASSWORD,
        password_min_length=settings.PASSWORD_MIN_LENGTH,
        password_min_character_classes=settings.PASSWORD_MIN_CHARACTER_CLASSES,
        email_verification_resend_cooldown_seconds=(
            settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS
        ),
    )


@router.get(
    "/hello-world",
    response_model=Msg,
    status_code=200,
    include_in_schema=False,
)
def test_hello_world():
    return {"msg": "Hello world!"}


@router.get("/health", response_model=Msg, status_code=200)
def health_check():
    """Liveness probe for the mobile client's reconnect loop.

    Deliberately touches nothing — no auth, no DB, no Redis. The client polls this
    on a backoff while it believes it is offline, so it has to keep answering even
    when the rest of the stack is degraded, and it must not cost a query per poll.
    """
    return {"msg": "ok"}
