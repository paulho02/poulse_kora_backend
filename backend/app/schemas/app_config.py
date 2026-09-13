from pydantic import BaseModel


class PublicAppConfig(BaseModel):
    require_email_verification: bool
    require_strong_password: bool
    password_min_length: int
    password_min_character_classes: int
    email_verification_resend_cooldown_seconds: int
    google_oauth_enabled: bool
    # The languages a post may be written in, and the reserved value meaning "no
    # language at all". Served rather than hardcoded in the client so adding a
    # language is a backend setting plus a stopword list in the client's detector,
    # not a client release - and so the picker, the detector's candidate set and the
    # values the API will accept can never drift apart.
    content_languages: list[str]
    language_unspecified: str
