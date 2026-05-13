"""Commerce integrations: payment links, refunds, price management.

Today: Stripe Payment Links via direct REST calls (no stripe-python SDK
to keep our dep surface tight). Other backends (Lemon Squeezy, Paddle,
Gumroad) plug in behind the same ``CommerceClient`` shape.
"""
from korpha.commerce.stripe_client import (
    PaymentLink,
    StripeClient,
    StripeError,
)

__all__ = ["PaymentLink", "StripeClient", "StripeError"]
