import uuid

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    username: str | None
    bio: str | None
    dark_mode: bool
    onboarding_completed: bool
    # Read-only: bumped server-side on settings changes, never accepted from clients
    # (hence absent from UserUpdate). See User.settings_revision.
    settings_revision: int


class UserCreate(schemas.BaseUserCreate):
    username: str


class UserUpdate(schemas.BaseUserUpdate):
    username: str | None = None
    bio: str | None = None
    dark_mode: bool | None = None
    onboarding_completed: bool | None = None
