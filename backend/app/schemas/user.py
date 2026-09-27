import uuid

from fastapi_users import schemas
from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.core.config import settings
from app.core.username_policy import normalize_username


def _normalized(value: object) -> object:
    """`mode="before"` validator body: fold case/width before `max_length` counts,
    so the bound applies to the name that is stored. Refusing a name that breaks
    the rule is `UserManager`'s job (`username_invalid`), not a 422 here."""
    return normalize_username(value) if isinstance(value, str) else value


class UserRead(schemas.BaseUser[uuid.UUID]):
    username: str | None
    bio: str | None
    dark_mode: bool
    onboarding_completed: bool
    # Read-only: bumped server-side on settings changes, never accepted from clients
    # (hence absent from UserUpdate). See User.settings_revision.
    settings_revision: int
    # "password" | "google", derived from User.oauth_accounts. The client hides the
    # change-password entry and runs the onboarding username step off this, rather
    # than letting the user find out by being refused.
    auth_provider: str
    # Address of the linked Google account. Not the same as `email` once an
    # account links a Google account with a different address - `email` is the
    # contact address, this is what signs you in. None for password accounts.
    google_email: str | None
    # URL to fetch the raw image bytes from, or None if unset. Read-only here -
    # set via PUT/DELETE /users/me/profile-picture (app/api/users.py), not this
    # PATCH, since it's a binary upload rather than a JSON field.
    profile_picture_url: str | None
    # Languages this reader accepts posts in. Read-only here and absent from
    # UserUpdate: changing it has to rewrite Redis audience memberships, so it has its
    # own route (PUT /users/me/content-languages) rather than riding the generic PATCH,
    # where fastapi-users' router would write the column with no way to sync Redis.
    content_languages: list[str]


class ContentLanguagesUpdate(BaseModel):
    """Body of `PUT /users/me/content-languages`.

    The whole set, not a delta: the route rewrites audience memberships absolutely
    rather than diffing, so sending the desired end state is both what the server needs
    and what makes the call idempotent.

    Must be non-empty and a subset of `Settings.CONTENT_LANGUAGES` — an empty set is an
    audience of nowhere and therefore a permanently empty feed, which is never what a
    client means to ask for and is refused rather than stored.
    """

    languages: list[str]


class UserCreate(schemas.BaseUserCreate):
    username: str = Field(min_length=1, max_length=settings.USERNAME_MAX_LENGTH)
    # BaseUserCreate exposes these as client-settable, and the register router already
    # discards them (it calls user_manager.create(..., safe=True)) so they can't be set
    # in practice. SkipJsonSchema removes them from the OpenAPI schema too, so a
    # generated FE client never even offers the fields on the register form.
    is_superuser: SkipJsonSchema[bool | None] = Field(default=False, exclude=True)
    is_verified: SkipJsonSchema[bool | None] = Field(default=False, exclude=True)

    _normalize_username = field_validator("username", mode="before")(_normalized)


class UserUpdate(schemas.BaseUserUpdate):
    username: str | None = Field(
        default=None, min_length=1, max_length=settings.USERNAME_MAX_LENGTH
    )
    bio: str | None = Field(default=None, max_length=settings.BIO_MAX_LENGTH)
    dark_mode: bool | None = None
    onboarding_completed: bool | None = None

    _normalize_username = field_validator("username", mode="before")(_normalized)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class AccountDelete(BaseModel):
    """Body of `DELETE /users/me` - the two things the confirmation flow asks.

    `delete_posts` is the choice made on the dialog's first slide, and it is
    required rather than defaulted: the two outcomes differ in whether other
    people keep seeing this person's posts, which is not a decision to make on
    a client's behalf when the field is missing.

    `current_password` is the second slide, and only for password accounts - a
    Google account's stored hash is a random value nobody holds (see
    app/api/google_auth.py), so there is nothing it could prove. See the route
    for why proving anything is asked for at all.
    """

    delete_posts: bool
    current_password: str | None = None


class GoogleAuthRequest(BaseModel):
    id_token: str
    #: Second pass of the two-step upgrade. False the first time: if the address
    #: already belongs to a password account, the route answers 409
    #: `google_link_required` instead of linking, the client explains that the
    #: upgrade is permanent, and only a confirmed user comes back with this true.
    link_existing: bool = False


class GoogleLinkRequest(BaseModel):
    """Body of `POST /auth/google/link`.

    `current_password` is required for the same reason `AccountDelete` asks for
    it: linking destroys the password, so without it a stolen bearer token is
    enough to bind the attacker's Google account and lock the owner out for
    good. Optional in the schema rather than required so a client that omits it
    gets the structured `google_link_password_required` instead of a 422. Every
    account that can reach the check is a password account - a Google one is
    refused as `google_already_linked` first.
    """

    id_token: str
    current_password: str | None = None
