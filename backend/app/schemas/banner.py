from pydantic import BaseModel


class BannerSet(BaseModel):
    message: str


class BannerRead(BaseModel):
    id: str
    message: str
    set_at: float
