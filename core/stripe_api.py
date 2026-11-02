"""Stripe (online checkout payments), called over HTTPS with the account's secret key.

Only what the site uses: Checkout Sessions (Stripe's embedded payment form on our checkout page),
reading a payment back (its fee and refunds), and checking that a webhook message really came
from Stripe. Stripe's API takes form-encoded fields, nested like line_items[0][price_data][currency].
The key is passed in by the caller (an environment variable on Railway); it's never stored or logged."""
import hashlib
import hmac
import time

import requests

API_BASE = "https://api.stripe.com/v1"
API_VERSION = "2024-06-20"


class StripeError(Exception):
    """A request Stripe refused or couldn't answer; the message is Stripe's own (safe to show staff)."""


def _flatten(value, prefix: str, out: list) -> list:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{prefix}[{k}]" if prefix else str(k), out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(v, f"{prefix}[{i}]", out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif value is not None:
        out.append((prefix, str(value)))
    return out


def form_fields(params: dict) -> list:
    """{"a": {"b": [1, 2]}} -> [("a[b][0]", "1"), ("a[b][1]", "2")] (Stripe's field names)."""
    return _flatten(params or {}, "", [])


def call(method: str, path: str, key: str, params: dict | None = None, idempotency_key: str = "",
         timeout: int = 20) -> dict:
    """One Stripe API request. Returns Stripe's object; raises StripeError when Stripe says no."""
    if not key:
        raise StripeError("Stripe isn't set up on the server (no secret key)")
    headers = {"Authorization": f"Bearer {key}", "Stripe-Version": API_VERSION}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key       # a retried request never charges twice
    url = f"{API_BASE}/{path.lstrip('/')}"
    try:
        if method.upper() == "GET":
            r = requests.get(url, params=form_fields(params or {}), headers=headers, timeout=timeout)
        else:
            r = requests.post(url, data=form_fields(params or {}), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise StripeError(f"Couldn't reach Stripe: {e}") from e
    try:
        data = r.json()
    except ValueError:
        raise StripeError(f"Stripe answered {r.status_code} without details")
    if not r.ok:
        raise StripeError((data.get("error") or {}).get("message") or f"Stripe error {r.status_code}")
    return data


def mode_of(key: str) -> str:
    """'test' or 'live' from a key's prefix (sk_test_ / pk_live_ ...), '' when there's no key."""
    if not key:
        return ""
    return "live" if "_live_" in key else "test"


def verify_webhook(payload: bytes, sig_header: str, secret: str, tolerance: int = 300,
                   now: float | None = None) -> bool:
    """Is this webhook message really from Stripe? The Stripe-Signature header carries a time (t)
    and one or more v1 signatures of "<t>.<body>" made with the endpoint's signing secret; a
    message older than `tolerance` seconds is refused (a replayed copy)."""
    if not secret or not sig_header:
        return False
    parts = {}
    for item in sig_header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))
