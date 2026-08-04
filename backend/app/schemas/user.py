import uuid

from fastapi_users import schemas
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema


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
    # BaseUserCreate exposes these as client-settable, and the register router already
    # discards them (it calls user_manager.create(..., safe=True)) so they can't be set
    # in practice. SkipJsonSchema removes them from the OpenAPI schema too, so a
    # generated FE client never even offers the fields on the register form.
    is_superuser: SkipJsonSchema[bool | None] = Field(default=False, exclude=True)
    is_verified: SkipJsonSchema[bool | None] = Field(default=False, exclude=True)


class UserUpdate(schemas.BaseUserUpdate):
    username: str | None = None
    bio: str | None = None
    dark_mode: bool | None = None
    onboarding_completed: bool | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
