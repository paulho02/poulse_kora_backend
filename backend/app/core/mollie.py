"""Thin async wrapper around the Mollie REST API (https://docs.mollie.com/reference).

Deliberately not the official `mollie-api-python` SDK — that client is synchronous
(requests-based), which would block the event loop in this all-async codebase. Mollie's
API is small enough that a few httpx calls are simpler than bridging a sync SDK with
`run_in_threadpool`.

Every call raises `api_error(502, "mollie_error", ...)` on a non-2xx response, since a
Mollie outage/misconfiguration is not something the caller can recover from — it
should surface as a clean 502 rather than an unhandled httpx exception.
"""

import httpx

from app.core.config import settings
from app.core.errors import api_error


async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=settings.MOLLIE_API_BASE_URL) as client:
        response = await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {settings.MOLLIE_API_KEY}"},
            **kwargs,
        )
    if response.status_code >= 400:
        raise api_error(
            502,
            "mollie_error",
            mollie_status=response.status_code,
            mollie_detail=response.json().get("detail") if response.content else None,
        )
    return response.json() if response.content else {}


async def create_customer(email: str, name: str | None = None) -> dict:
    return await _request("POST", "/customers", json={"email": email, "name": name})


async def create_first_payment(
    *, customer_id: str, amount: str, currency: str, description: str,
    redirect_url: str, webhook_url: str, metadata: dict,
) -> dict:
    """Kicks off the recurring flow: a regular checkout payment that, once paid,
    leaves the customer with a valid mandate to charge later (see
    https://docs.mollie.com/docs/recurring-payments). The actual Subscription
    resource is only created after this first payment succeeds — see
    `app/api/subscriptions.py`'s webhook handler."""
    return await _request(
        "POST",
        "/payments",
        json={
            "amount": {"currency": currency, "value": amount},
            "description": description,
            "customerId": customer_id,
            "sequenceType": "first",
            "redirectUrl": redirect_url,
            "webhookUrl": webhook_url,
            "metadata": metadata,
        },
    )


async def get_payment(payment_id: str) -> dict:
    return await _request("GET", f"/payments/{payment_id}")


async def create_subscription(
    *, customer_id: str, amount: str, currency: str, interval: str,
    description: str, webhook_url: str, metadata: dict,
) -> dict:
    return await _request(
        "POST",
        f"/customers/{customer_id}/subscriptions",
        json={
            "amount": {"currency": currency, "value": amount},
            "interval": interval,
            "description": description,
            "webhookUrl": webhook_url,
            "metadata": metadata,
        },
    )


async def get_subscription(customer_id: str, subscription_id: str) -> dict:
    return await _request(
        "GET", f"/customers/{customer_id}/subscriptions/{subscription_id}"
    )


async def cancel_subscription(customer_id: str, subscription_id: str) -> dict:
    return await _request(
        "DELETE", f"/customers/{customer_id}/subscriptions/{subscription_id}"
    )
