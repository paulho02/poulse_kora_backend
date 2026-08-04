"""Authenticated password change - requires proving the current password, unlike
the stock fastapi-users `PATCH /users/me` route (see `UserManager.update` in
app/deps/users.py, which now refuses a bare `password` there so this is the only
path a password can change through).
"""

from fastapi import APIRouter, Depends, status
from fastapi_users.exceptions import InvalidPasswordException

from app.core.errors import api_error
from app.deps.db import CurrentAsyncSession
from app.deps.rate_limit import limit_password_change
from app.deps.users import CurrentUser, UserManager, get_user_manager
from app.schemas.user import PasswordChange

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_password_change)],
)
async def change_password(
    body: PasswordChange,
    user: CurrentUser,
    session: CurrentAsyncSession,
    user_manager: UserManager = Depends(get_user_manager),
):
    verified, _ = user_manager.password_helper.verify_and_update(
        body.current_password, user.hashed_password
    )
    if not verified:
        raise api_error(400, "change_password_wrong_current_password")

    try:
        await user_manager.validate_password(body.new_password, user)
    except InvalidPasswordException as exc:
        raise api_error(
            400, "change_password_invalid_password", reason=exc.reason
        ) from exc

    user.hashed_password = user_manager.password_helper.hash(body.new_password)
    await session.commit()
