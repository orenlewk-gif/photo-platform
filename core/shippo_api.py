"""Shippo (shipping labels for print and frame orders), called over HTTPS with the account's API token.

Only what the studio uses: a shipment (from our address to the customer's, one package) with the
carriers' rates, buying one of those rates as a label (a PDF plus a tracking number), and asking for
an unused label's money back. The token is passed in by the caller (an environment variable on
Railway); it's never stored or logged. shippo_test_ tokens make free test labels."""
import requests

API_BASE = "https://api.goshippo.com"


class ShippoError(Exception):
    """A request Shippo refused or couldn't answer; the message is Shippo's own (safe to show staff)."""


def _message(data) -> str:
    """Shippo's error text, from whichever shape it came in."""
    if isinstance(data, dict):
        if data.get("detail"):
            return str(data["detail"])
        texts = [m.get("text", "") for m in data.get("messages") or [] if isinstance(m, dict)]
        if any(texts):
            return "; ".join(t for t in texts if t)
        parts = []
        for k, v in data.items():                      # field errors: {"address_to": {"zip": ["..."]}}
            parts.append(f"{k}: {v if isinstance(v, str) else v!r}")
        return "; ".join(parts)[:300]
    return str(data)[:300]


def call(method: str, path: str, token: str, body: dict | None = None, timeout: int = 30) -> dict:
    """One Shippo API request. Returns Shippo's object; raises ShippoError when Shippo says no."""
    if not token:
        raise ShippoError("Shipping labels aren't set up on the server (no Shippo token)")
    headers = {"Authorization": f"ShippoToken {token}", "Content-Type": "application/json"}
    url = f"{API_BASE}/{path.strip('/')}/"
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, json=body or {}, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ShippoError(f"Couldn't reach Shippo: {e}") from e
    try:
        data = r.json()
    except ValueError:
        raise ShippoError(f"Shippo answered {r.status_code} without details")
    if not r.ok:
        raise ShippoError(_message(data) or f"Shippo error {r.status_code}")
    return data


def mode_of(token: str) -> str:
    """'test' or 'live' from the token's prefix, '' when there's no token."""
    if not token:
        return ""
    return "live" if token.startswith("shippo_live_") else "test"


def rate_summary(rate: dict) -> dict:
    """The parts of a rate the studio shows and keeps (Shippo has named the service two ways)."""
    sl = rate.get("servicelevel") or {}
    return {"id": rate.get("object_id", ""), "amount": float(rate.get("amount") or 0),
            "currency": rate.get("currency", "USD"), "carrier": rate.get("provider", ""),
            "service": sl.get("name") or rate.get("servicelevel_name") or "",
            "token": sl.get("token") or rate.get("servicelevel_token") or "",
            "days": rate.get("estimated_days"), "terms": rate.get("duration_terms") or ""}
