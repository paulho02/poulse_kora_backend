from pydantic import BaseModel, ConfigDict


class ChannelRead(BaseModel):
    id: int
    name: str
    color: str
    description: str
    is_subscribed: bool

    model_config = ConfigDict(from_attributes=True)
