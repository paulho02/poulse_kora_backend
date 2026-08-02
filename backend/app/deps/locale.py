from typing import Annotated

from fastapi import Depends, Request

from app.core.locale import parse_accept_language


def get_locale(request: Request) -> str:
    return parse_accept_language(request.headers.get("accept-language"))


CurrentLocale = Annotated[str, Depends(get_locale)]
