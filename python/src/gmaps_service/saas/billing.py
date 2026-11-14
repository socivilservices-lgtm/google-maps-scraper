"""Stripe Checkout, the customer portal, and webhook updates.

Billing stays off when the Stripe environment variables are missing. The rest
of the app still runs, and the account page says billing is not configured.
"""

from __future__ import annotations

from typing import Any

from gmaps_service.config import Config

PLAN_NAME = "Pro"
PLAN_PRICE_USD = 29
PLAN_MONTHLY_RESULTS = 3000
PLAN_MONTHLY_SEARCHES = 100
PLAN_MAX_RESULTS_PER_SEARCH = 100


class BillingDisabled(Exception):
    """Stripe is not configured."""


def billing_enabled(config: Config) -> bool:
    return bool(config.stripe_secret_key and config.stripe_price_id)


def billing_message(config: Config) -> str:
    if billing_enabled(config):
        return "Subscribe to the Pro plan to keep searching after a trial."
    return (
        "Billing is not configured. Set STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, "
        "and STRIPE_PRICE_ID to enable Checkout. Until then, only the admin account "
        "and unexpired invite trials can run searches."
    )


def create_checkout(config: Config, user: dict[str, Any]) -> str:
    if not billing_enabled(config):
        raise BillingDisabled(billing_message(config))
    import stripe

    stripe.api_key = config.stripe_secret_key
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": config.stripe_price_id, "quantity": 1}],
        success_url=config.app_base_url + "/account?billing=success",
        cancel_url=config.app_base_url + "/account?billing=cancel",
        customer=user.get("stripe_customer_id") or None,
        customer_email=None if user.get("stripe_customer_id") else user["email"],
        client_reference_id=user["id"],
        metadata={"user_id": user["id"]},
        subscription_data={"metadata": {"user_id": user["id"]}},
    )
    url = session.get("url") if isinstance(session, dict) else session.url
    if not url:
        raise BillingDisabled("Stripe did not return a Checkout URL.")
    return str(url)


def create_portal(config: Config, user: dict[str, Any]) -> str:
    if not billing_enabled(config):
        raise BillingDisabled(billing_message(config))
    customer = user.get("stripe_customer_id")
    if not customer:
        raise BillingDisabled("Subscribe before opening the billing portal.")
    import stripe

    stripe.api_key = config.stripe_secret_key
    session = stripe.billing_portal.Session.create(
        customer=customer,
        return_url=config.app_base_url + "/account",
    )
    url = session.get("url") if isinstance(session, dict) else session.url
    if not url:
        raise BillingDisabled("Stripe did not return a portal URL.")
    return str(url)


def construct_event(payload: bytes, signature: str, secret: str) -> dict[str, Any]:
    import stripe

    event = stripe.Webhook.construct_event(payload, signature, secret)
    if isinstance(event, dict):
        return event
    return {"id": event.id, "type": event.type, "data": {"object": event.data.object}}


def apply_event(engine: Any, event: dict[str, Any]) -> str:
    """Copy subscription status from a Stripe event onto the user row."""
    from gmaps_service.saas import db

    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        obj = {}
    if event_type == "checkout.session.completed":
        user_id = str(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id") or "")
        customer = obj.get("customer")
        subscription = obj.get("subscription")
        if user_id:
            db.set_subscription(
                engine,
                user_id=user_id,
                customer_id=str(customer) if customer else None,
                subscription_id=str(subscription) if subscription else None,
                status="active",
            )
            return "activated"
        return "ignored"
    if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        status = str(obj.get("status") or "canceled")
        if event_type.endswith("deleted"):
            status = "canceled"
        customer = obj.get("customer")
        metadata = obj.get("metadata") or {}
        user_id = str(metadata.get("user_id") or "") or None
        db.set_subscription(
            engine,
            user_id=user_id,
            customer_id=str(customer) if customer else None,
            subscription_id=str(obj.get("id") or "") or None,
            status=status,
        )
        return status
    return "ignored"
