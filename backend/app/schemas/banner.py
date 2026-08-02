from pydantic import BaseModel, field_validator

from app.core.config import settings


class BannerSet(BaseModel):
    """Input for `POST /banner` — one message per locale."""

    messages: dict[str, str]

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, v: dict[str, str]) -> dict[str, str]:
        if not v.get("en", "").strip():
            raise ValueError("messages['en'] is required")
        unsupported = sorted(set(v) - set(settings.SUPPORTED_LOCALES))
        if unsupported:
            raise ValueError(f"unsupported locale(s): {', '.join(unsupported)}")
        return v


class BannerRead(BaseModel):
    """Public shape (`GET /banner`) — resolved to the requester's locale."""

    id: str
    message: str
    set_at: float


class BannerAdminRead(BaseModel):
    """Superuser shape (`POST /banner` response) — every locale that was
    stored, so an admin can confirm what actually saved."""

    id: str
    messages: dict[str, str]
    set_at: float
