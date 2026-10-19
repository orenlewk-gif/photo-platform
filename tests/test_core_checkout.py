"""core/checkout.py: coupon checks, bundle detection, discount math and shipping as plain functions."""
from datetime import datetime, timedelta, timezone

import pytest

from core.checkout import (CheckoutError, bundle_for_cart, check_coupon, coupon_item_discount, coupon_scope,
                           parse_int, shipping_for)

SIZES = [{"ship_print": 8, "ship_framed": 15}, {"ship_print": 10, "ship_framed": 20}]
iso = lambda d: (datetime.now(timezone.utc) + timedelta(days=d)).isoformat()


@pytest.mark.parametrize("code,email,reason", [
    ("", "", "No code provided"),
    ("NOPE", "", "Invalid coupon code"),
    ("OFF", "", "This code is no longer active"),
    ("LATER", "", "This code is not yet active"),
    ("OLD", "", "This code has expired"),
    ("USED", "", "This code has reached its usage limit"),
    ("ONCE", "a@x.com", "You have already used this code"),
])
def test_coupon_refusals(code, email, reason):
    codes = [{"code": "OFF", "active": False}, {"code": "LATER", "start_date": iso(2)}, {"code": "OLD", "expires": iso(-1)},
             {"code": "USED", "usage_limit": 2, "usage_count": 2},
             {"code": "ONCE", "usage_limit_per_user": 1, "uses": [{"email": "A@x.com"}]}]
    assert check_coupon(codes, code, email) == (None, reason)


def test_good_coupon_any_case():
    c = {"code": "PARTNER10", "type": "percent", "amount": 10}
    assert check_coupon([c], " partner10 ") == (c, None)


def test_coupon_scope_and_discount():
    assert coupon_scope({"applies_to": "portraits"}) == {"digital"}
    assert coupon_scope({"applies_to": "frames"}) == {"frames"}
    assert coupon_scope({}) == {"digital", "prints", "frames"}
    assert coupon_item_discount({"type": "percent", "amount": 10}, 115) == 11.5
    assert coupon_item_discount({"type": "fixed", "amount": 50}, 30) == 30
    assert coupon_item_discount({"type": "free_shipping"}, 100) == 0


def test_shipping_rules():
    assert shipping_for([{"size_idx": 1}], [], SIZES, 3) == (10.0, False)                        # one print
    assert shipping_for([{"size_idx": 0}] * 3, [], SIZES, 3) == (0.0, True)                      # free from 3
    framed = [{"size_idx": 1, "frame": "Black"}, {"size_idx": 0, "frame": "Black"}]
    assert shipping_for(framed, [0], SIZES, 3) == (20 + 7.5 + 7.5, False)                        # dearest full, rest half
    assert shipping_for([], [], SIZES, 3) == (0.0, False)


def test_bundle_needs_every_group_and_upsells_the_missing_one():
    pricing = {"activity_categories": [{"name": "Winter Action", "acts": ["El Dorado Action"]},
                                       {"name": "Winter Portraits", "acts": ["Lone Peak Portraits"]}],
               "combos": [{"label": "Combo", "activity_groups": ["Winter Action", "Winter Portraits"], "pct_off": 10}]}
    assert bundle_for_cart(pricing, {"el dorado action"}, True) == ({"label": "Combo", "pct": 10.0}, None)
    assert bundle_for_cart(pricing, {"el dorado action"}, False) == (None, {"missing": ["Portraits"], "pct": 10.0})
    assert bundle_for_cart(pricing, set(), False) == (None, None)


def test_parse_int_refuses_silly_values():
    assert parse_int("3", 0, 1, 5, "x") == 3
    with pytest.raises(CheckoutError):
        parse_int("99", 0, 1, 5, "x")
    with pytest.raises(CheckoutError):
        parse_int("abc", 0, 1, 5, "x")
