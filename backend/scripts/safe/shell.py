"""Open an IPython shell with a **synchronous** SQLAlchemy session bound as `db`.

Deliberately sync (`create_engine`, not the app's `async_session_maker`): the point
is to type `db.query(User).all()` and read the result, which in an async session
would need an `await` in front of every statement at a REPL that does not take one
by default.

Running it does nothing on its own — what you then type does, so treat the session
as you would `psql` against the same database. `autoreload` is on, so editing a
model file is picked up without restarting the shell.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.safe.shell
"""

from IPython.terminal import embed
from sqlalchemy.engine import create_engine
from sqlalchemy.orm.session import sessionmaker

from app.core.config import settings
from app.models.user import User

if __name__ == "__main__":
    engine = create_engine(settings.DATABASE_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    terminal = embed.InteractiveShellEmbed()
    terminal.extension_manager.load_extension("autoreload")
    terminal.run_line_magic("autoreload", "2")

    db = SessionLocal(future=True)
    terminal.mainloop()
