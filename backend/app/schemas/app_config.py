from pydantic import BaseModel


class PublicAppConfig(BaseModel):
    require_email_verification: bool
    require_strong_password: bool
    password_min_length: int
    password_min_character_classes: int
    email_verification_resend_cooldown_seconds: int
