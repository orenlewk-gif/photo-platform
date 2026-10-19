"""Checkout rules that don't depend on the web app: coupons, bundles, discounts, shipping.
The order itself is built in main.py (_price_order), which calls these."""
import re
from datetime import datetime, timezone


class CheckoutError(ValueError):
    """A cart the server can't price: an unknown item, prices not set up, or
    WooCommerce unavailable. The message is shown to the customer."""


CATEGORIES = ("digital", "prints", "frames")


def activity_groups(pricing: dict) -> dict:
    """Activity group name -> its activities (location names): the studio's categories,
    with explicit pricing["activity_groups"] entries winning."""
    derived = {cat["name"]: cat.get("acts", []) for cat in pricing.get("activity_categories", []) if cat.get("name")}
    return {**derived, **pricing.get("activity_groups", {})}


def group_in_cart(name: str, acts: list, locations: set, has_portraits: bool) -> bool:
    """Same test as the checkout page used: a folder from one of the group's activities
    is in the cart, or (for a portrait group) portrait photos are."""
    acts_lower = {a.lower() for a in acts}
    if acts_lower & locations:
        return True
    is_portrait_group = "portrait" in name.lower() or any("portrait" in a for a in acts_lower)
    return is_portrait_group and has_portraits


def bundle_for_cart(pricing: dict, locations: set, has_portraits: bool):
    """(active bundle or None, upsell or None). A bundle is active when the cart has
    something from every one of its activity groups; the upsell names what's missing
    when only some are there."""
    groups = activity_groups(pricing)
    upsell = None
    for combo in pricing.get("combos", []):
        names, pct = combo.get("activity_groups") or [], combo.get("pct_off")
        if not names or pct is None:
            continue
        present = [n for n in names if group_in_cart(n, groups.get(n, []), locations, has_portraits)]
        if len(present) == len(names):
            return {"label": combo.get("label") or "Bundle deal", "pct": float(pct)}, None
        if present and upsell is None:
            short = lambda g: re.sub(r"^(Winter|Summer|Fall|Spring|Autumn)\s+", "", g, flags=re.I)
            upsell = {"missing": [short(n) for n in names if n not in present], "pct": float(pct)}
    return None, upsell


def check_coupon(codes: list, code: str, email: str = ""):
    """(discount code, None) if `code` can be used now, else (None, reason). codes = the studio's discount codes."""
    code_clean = (code or "").strip().upper()
    if not code_clean:
        return None, "No code provided"
    c = next((x for x in codes if x.get("code", "").upper() == code_clean), None)
    if not c:
        return None, "Invalid coupon code"
    if not c.get("active", True):
        return None, "This code is no longer active"

    def _when(value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    try:
        if c.get("start_date") and now < _when(c["start_date"]):
            return None, "This code is not yet active"
    except Exception:
        pass
    try:
        if c.get("expires") and now > _when(c["expires"]):
            return None, "This code has expired"
    except Exception:
        pass
    limit = c.get("usage_limit")
    if limit and int(c.get("usage_count", 0)) >= int(limit):
        return None, "This code has reached its usage limit"
    per_user = c.get("usage_limit_per_user")
    if per_user and email:
        if sum(1 for u in c.get("uses", []) if u.get("email", "").lower() == email.lower()) >= int(per_user):
            return None, "You have already used this code"
    return c, None


def coupon_scope(code_obj: dict) -> set:
    at = code_obj.get("applies_to", "all")
    if at in ("digital", "portraits"):
        return {"digital"}
    if at in ("prints", "frames"):
        return {at}
    return set(CATEGORIES)


def coupon_item_discount(code_obj: dict, base: float) -> float:
    """What a code takes off items worth `base` (a free-shipping code takes nothing off items)."""
    ctype, amount = code_obj.get("type", "percent"), float(code_obj.get("amount", 0) or 0)
    if ctype == "percent":
        return round(base * amount / 100, 2)
    if ctype == "fixed":
        return round(min(amount, base), 2)
    return 0.0


def parse_int(value, default: int, lo: int, hi: int, what: str) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise CheckoutError(f"Something in your cart isn't right ({what})")
    if not lo <= n <= hi:
        raise CheckoutError(f"Something in your cart isn't right ({what})")
    return n


def shipping_for(prints: list, frame_ship_sizes: list, sizes: list, free_threshold) -> tuple:
    """(shipping, free_shipping) for the prints and frames in a cart: the dearest framed item
    in full and every other framed item at half; unframed prints ship at the dearest print's
    rate, or free from `free_threshold` prints."""
    print_ship = [s.get("ship_print", 8) for s in sizes]
    frame_ship = [s.get("ship_framed", 15) for s in sizes]
    framed = [p for p in prints if p.get("frame") not in ("", None, "No Frame")]
    unframed = [p for p in prints if p.get("frame") in ("", None, "No Frame")]
    frame_rates = sorted([frame_ship[int(p.get("size_idx", 0))] for p in framed] +
                         [frame_ship[i] for i in frame_ship_sizes], reverse=True)
    shipping, free_shipping = 0.0, False
    if frame_rates:
        shipping = round(frame_rates[0] + sum(r * 0.5 for r in frame_rates[1:]), 2)
    elif unframed:
        if len(unframed) >= free_threshold:
            free_shipping = True
        else:
            shipping = float(max(print_ship[int(p.get("size_idx", 0))] for p in unframed))
    return shipping, free_shipping
