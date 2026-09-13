from pydantic import BaseModel, ConfigDict


class ChannelRead(BaseModel):
    """A channel, plus what it currently costs to publish in it.

    There is no single price any more: a channel is several *routes* (one per content
    language, plus the no-language one), each priced by its own congestion, so what a
    channel can quote is a range. `post_price_min`/`post_price_max` bracket the routes
    of this channel for the current price window, and the exact number `POST /posts`
    will charge is `GET /posts/price` once the author has picked a language.

    `FEED_PRICE_CHANNEL_BAND` holds every route within ±50% of the shared base price,
    so a base price of 4 spans 2-6. The two ends still collapse to one number at the
    very bottom of the scale, where ±50% of 1 rounds back to 1 - correct rather than
    broken, since a price that low means the system wants content (see
    pricing.route_price).

    Returned unconditionally, though the client only renders it while the user has
    price display switched on: it is a couple of bytes, and a query parameter that
    changed the response shape to match a UI toggle would be the worse trade.
    """

    id: int
    name: str
    color: str
    description: str
    is_subscribed: bool
    post_price_min: int
    post_price_max: int

    model_config = ConfigDict(from_attributes=True)
