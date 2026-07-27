from fastapi import APIRouter

from app.schemas.msg import Msg

router = APIRouter()


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
