from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def test_user_model(db: AsyncSession):
    # Unique email: the session-scoped `db` commits persist in the apptest volume
    # across runs, so a hardcoded address collides with the unique constraint.
    user = User(id=uuid4(), email=f"{uuid4()}@example.com", hashed_password="1234")
    db.add(user)
    await db.commit()
    assert user.id
