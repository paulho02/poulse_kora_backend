from pydantic import BaseModel, ConfigDict


class ChannelRead(BaseModel):
    """A channel, plus what it currently costs to publish in it.

    `post_price` is the exact number `POST /posts` will charge for this channel until
    the price window rolls over (see app/feed/service.py: channel_prices) — it is a
    quote, not an estimate. Returned unconditionally, though the client only renders it
    while the user has price display switched on: it is a couple of bytes, and a query
    parameter that changed the response shape to match a UI toggle would be the worse
    trade.
    """

    id: int
    name: str
    color: str
    description: str
    is_subscribed: bool
    post_price: int

    model_config = ConfigDict(from_attributes=True)
