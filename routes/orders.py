"""Routes: orders. Moved from main.py unchanged; shared helpers still live in main.py."""
import re
from core.text import clean_location
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from fastapi import Body
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from zoneinfo import ZoneInfo

from fastapi import APIRouter

import main
from core import stripe_api
from routes import shipping
from core.pages import render_page

router = APIRouter()


@router.post("/api/admin/orders/refund")
def admin_record_refund(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key = str(body.get("key") or "")
    if not main._REFUND_KEY_RE.match(key):
        return JSONResponse(status_code=400, content={"error": "Unknown order"})
    try:
        amount = round(float(body.get("amount")), 2)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Enter the amount refunded"})
    with main._order_refunds_lock:
        refunds = main._load_order_refunds(strict=True)
        refunds[key] = {"amount": amount, "full": bool(body.get("full")),
                        "note": str(body.get("note") or "").strip()[:300],
                        "date": datetime.now(timezone.utc).isoformat()}
        main._save_order_refunds(refunds)
    return {"ok": True}


@router.delete("/api/admin/orders/refund")
def admin_undo_refund(request: Request, key: str = Query("")):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    with main._order_refunds_lock:
        refunds = main._load_order_refunds(strict=True)
        if refunds.pop(key, None) is not None:
            main._save_order_refunds(refunds)
    return {"ok": True}


@router.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/orders")
    return RedirectResponse("/admin/orders?tab=reports")


@router.get("/api/admin/reports")
def api_admin_reports(request: Request, days: int = 30,
                      date_from: str = "", date_to: str = "",
                      all_time: int = 0):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from collections import defaultdict
    if all_time:
        orders = main._fetch_online_orders()
    elif date_from and date_to:
        orders = main._fetch_online_orders(after=f"{date_from}T00:00:00",
                                           before=f"{date_to}T23:59:59")
    else:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = main._fetch_online_orders(after=after)

    rows = [main._order_row(o) for o in orders]

    # Summary
    total_rev  = sum(r["total"]      for r in rows)
    total_net  = sum(r["net"]        for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    n          = len(rows)
    avg_order  = total_rev / n if n else 0

    # Daily buckets
    daily: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        d = r["date"]
        if d:
            daily[d]["revenue"] += r["total"]
            daily[d]["net"]     += r["net"]
            daily[d]["fees"]    += r["stripe_fee"]
            daily[d]["orders"]  += 1
    daily_series = [{"date": k, "revenue": round(v["revenue"], 2),
                     "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                    for k, v in sorted(daily.items())]

    # Monthly buckets
    monthly: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        if r["date"] and len(r["date"]) >= 7:
            m = r["date"][:7]
            monthly[m]["revenue"] += r["total"]
            monthly[m]["net"]     += r["net"]
            monthly[m]["fees"]    += r["stripe_fee"]
            monthly[m]["orders"]  += 1
    monthly_series = [{"month": k, "revenue": round(v["revenue"], 2),
                       "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                      for k, v in sorted(monthly.items())]

    # By activity
    by_act: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "orders": 0})
    for r in rows:
        loc = clean_location(r["location"]) if r["location"] else "Unknown"
        by_act[loc]["revenue"] += r["total"]
        by_act[loc]["net"]     += r["net"]
        by_act[loc]["orders"]  += 1
    by_activity = [{"name": k, "revenue": round(v["revenue"], 2),
                    "net": round(v["net"], 2), "orders": v["orders"]}
                   for k, v in sorted(by_act.items(), key=lambda x: x[1]["revenue"], reverse=True)]

    # By day of week
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow: dict = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    for r in rows:
        try:
            dt  = datetime.strptime(r["date"], "%Y-%m-%d")
            day = dow_names[dt.weekday()]
            by_dow[day]["revenue"] += r["total"]
            by_dow[day]["orders"]  += 1
        except Exception:
            pass
    dow_series = [{"day": d, "revenue": round(by_dow[d]["revenue"], 2),
                   "orders": by_dow[d]["orders"]} for d in dow_names]

    return {
        "summary": {
            "revenue":   round(total_rev, 2),
            "net":       round(total_net, 2),
            "fees":      round(total_fees, 2),
            "orders":    n,
            "avg_order": round(avg_order, 2),
        },
        "daily":       daily_series,
        "monthly":     monthly_series,
        "by_activity": by_activity,
        "by_dow":      dow_series,
    }


@router.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request):
    """The Orders page; it loads its sales from /api/admin/sales (?tab=reports opens Reports)."""
    if not main._admin_authed(request):
        return RedirectResponse("/admin")
    return HTMLResponse(render_page("admin_orders.html"))


@router.get("/admin/export")
def admin_export(request: Request, days: int = 30,
                 date_from: str = "", date_to: str = ""):
    if not main._admin_authed(request):
        return RedirectResponse("/admin")
    import csv, io
    if date_from and date_to:
        orders = main._fetch_online_orders(after=f"{date_from}T00:00:00", before=f"{date_to}T23:59:59")
    else:
        after  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = main._fetch_online_orders(after=after)
    rows   = [main._order_row(o) for o in orders]
    rows  += [main._pos_order_row(o) for o in main._fill_pending_square_fees(main._fetch_pos_orders(
        after=(date_from if date_from and date_to else (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")),
        before=(date_to if date_from and date_to else None)))]
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    main._apply_refunds(rows, main._load_order_refunds())
    buf    = io.StringIO()
    writer = csv.writer(buf)
    _exp_photogs    = main._load_photographers()
    _exp_prefix_map = {p.get("file_prefix","").strip().lower(): p["name"]
                       for p in _exp_photogs if p.get("file_prefix","").strip()}
    def _exp_photog(fn):
        if not fn: return "Oren Lewkowski"
        names, seen = [], set()
        for f in fn.split(','):
            f = f.strip()
            if not f: continue
            name = _exp_prefix_map.get(f[:3].lower())
            if name and name not in seen:
                names.append(name); seen.add(name)
        return " & ".join(names) if names else "Oren Lewkowski"
    writer.writerow(["Date","Order #","First Name","Last Name","Email","Photographer","What Ordered",
                     "Photo Files","Location","Photo Date","Total","Tip","Discount",
                     "Card Fee","Net","Refunded","Coupon Code","Shipping Cost","Shipping Address"])
    _exp_unl = {}
    if any(r.get("unlimited") for r in rows):
        main._close_due_unlimited()
        _exp_unl = main._unlimited_records()
    for r in rows:
        photog = main._unlimited_photog_label(r, _exp_photogs, _exp_unl) or _exp_photog(r["filenames"])
        if r.get("source") == "pos":
            fee_csv = "pending" if r.get("fee_pending") else (f"${r['stripe_fee']:.2f}" if r["stripe_fee"] else "")
            order_csv = ("Square " + r["order_no"]) if r.get("order_no") else ""
        else:
            fee_csv, order_csv = f"${r['stripe_fee']:.2f}", r.get("order_no", "")
        writer.writerow([r["date"], order_csv, r["first"], r["last"], r["email"], photog, r["what"],
                         r["filenames"], r["location"], r["photo_date"],
                         "Comp" if r.get("is_comp") else f"${r['charged_original']:.2f}",
                         f"${r['tip']:.2f}" if r.get("tip") else "",
                         f"-${r['discount']:.2f}" if r["discount"] else "",
                         fee_csv, f"${r['net']:.2f}",
                         f"${r['refunded']:.2f}" if r.get("refunded") else "",
                         r["coupon_code"],
                         f"${r['shipping']:.2f}" if r["shipping"] else "",
                         r["ship_addr"]])
    filename = f"crystal-images-orders-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── All sales, online and in the store, for the Orders page and its reports ──────
# One list with everything about each sale; the page filters it and builds its reports from
# it, so what's listed and what's totalled always agree.

_MT = ZoneInfo("America/Denver")
SALE_CATEGORIES = {"digital": "Action photos", "portrait": "Portraits", "unlimited": "Unlimited package",
                   "prints": "Prints", "frames": "Frames", "private": "Private sessions", "other": "Other"}
_POS_CATEGORY = {"action_photos": "digital", "portrait": "portrait", "unlimited_package": "unlimited",
                 "prints": "prints", "frames": "frames", "private": "private", "base_area": "private"}


def _online_line_category(li: dict) -> str:
    pid, name = str(li.get("product_id") or ""), str(li.get("name") or "").strip().lower()
    if pid == str(main.WC_FRAME_PARENT_ID) or name.startswith("└") or "frame" in name:
        return "frames"
    if pid == str(main.WC_PRINT_PRODUCT_ID) or " print" in name:
        return "prints"
    if name.startswith("portrait"):
        return "portrait"
    return "digital"


def _photographer_by_files(filenames: str, prefix_map: dict) -> str:
    """Who took the photos, from their JPEG codes (the first three letters); the owner otherwise."""
    names, seen = [], set()
    for f in (filenames or "").split(","):
        name = prefix_map.get(f.strip()[:3].lower()) if f.strip() else None
        if name and name not in seen:
            names.append(name); seen.add(name)
    return " & ".join(names) if names else "Oren Lewkowski"


def _sale_from_online(o: dict) -> dict:
    r = main._order_row(o)
    meta = {m.get("key"): m.get("value") for m in o.get("meta_data", []) if isinstance(m, dict)}
    items = []
    for li in o.get("line_items") or []:
        name = str(li.get("name") or "").strip()
        items.append({"name": name.lstrip("└ ").strip() if name.startswith("└") else name,
                      "cat": _online_line_category(li), "qty": int(li.get("quantity") or 1),
                      "amount": round(float(li.get("total") or 0), 2)})
    created = str(o.get("date_created") or "")
    sa, ba = o.get("shipping") or {}, o.get("billing") or {}
    address = ", ".join(x for x in (ba.get("address_1", ""), ba.get("address_2", ""), ba.get("city", ""),
                                     ba.get("state", ""), ba.get("postcode", "")) if x)
    return {**r, "channel": "online", "provider": r["provider"],
            "time": created[11:16], "phone": (o.get("billing") or {}).get("phone", ""),
            "seller": "", "items": items, "tax": 0.0, "tip": 0.0, "charged": r["total"], "address": address,
            "bundle_savings": round(float(meta.get("_bundle_savings") or 0), 2),
            "ship_to": {k: sa.get(k, "") for k in ("first_name", "last_name", "address_1", "address_2",
                                                     "city", "state", "postcode", "country")} if sa.get("address_1") else None,
            "status": r.get("status") or o.get("status", ""),
            "status_note": (o.get("stripe") or {}).get("note", ""),
            "test": (o.get("stripe") or {}).get("mode") == "test", "is_comp": False, "unlimited": False}


def _sale_from_pos(o: dict) -> dict:
    r = main._pos_order_row(o)
    try:
        created = datetime.fromisoformat(str(o.get("created")))
        local = (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).astimezone(_MT)
        r["date"], time_str = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
    except Exception:
        time_str = ""
    items, shipping = [], 0.0
    for it in o.get("items") or []:
        cat = str(it.get("category") or "")
        amount = round(float(it.get("subtotal") if it.get("subtotal") is not None else it.get("price") or 0), 2)
        if cat == "_shipping":
            shipping += amount
            continue
        label = str(it.get("label") or "") + (f" ({it['detail']})" if it.get("detail") else "")
        items.append({"name": label, "cat": _POS_CATEGORY.get(cat, "other"),
                      "qty": int(it.get("qty") or 1), "amount": amount})
    return {**r, "channel": "store", "provider": "comp" if r["is_comp"] else "square",
            "time": time_str, "phone": o.get("phone", ""), "seller": o.get("photographer_name", ""),
            "items": items, "shipping": round(shipping, 2), "bundle_savings": 0.0,
            "ship_to": None, "status": "comp" if r["is_comp"] else "paid",
            "address": o.get("address", ""), "tenders": o.get("square_tenders") or []}


def _shift_day(day: str, n: int) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def _sales_between(date_from: str = "", date_to: str = "") -> list:
    """Every sale whose (Big Sky) day is in date_from..date_to (blank = no limit), newest first,
    refunds applied and photographers credited the same way as commission."""
    online = main._fetch_online_orders(after=f"{date_from}T00:00:00" if date_from else None,
                                       before=f"{date_to}T23:59:59" if date_to else None)
    # store sales are filed by their UTC day: read a day either side, then keep Big Sky days
    pos = main._fill_pending_square_fees(main._fetch_pos_orders(
        after=_shift_day(date_from, -1) if date_from else None, before=_shift_day(date_to, 1) if date_to else None))
    rows = [_sale_from_online(o) for o in online] + [_sale_from_pos(o) for o in pos]
    rows = [r for r in rows if (not date_from or r["date"] >= date_from) and (not date_to or r["date"] <= date_to)]
    rows.sort(key=lambda r: (r.get("date") or "", r.get("time") or ""), reverse=True)
    main._apply_refunds(rows, main._load_order_refunds())
    photogs = main._load_photographers()
    prefix_map = {p.get("file_prefix", "").strip().lower(): p["name"]
                  for p in photogs if p.get("file_prefix", "").strip()}
    unl = {}
    if any(r.get("unlimited") for r in rows):
        main._close_due_unlimited()
        unl = main._unlimited_records()
    shipments = shipping.all_shipments()
    now = datetime.now(timezone.utc)
    for r in rows:
        r["shipment"] = shipments.get(r["refund_key"])
        r["ships"] = bool(r.get("ship_to") or r.get("shipping") or r["shipment"])
        r["photographer"] = (main._unlimited_photog_label(r, photogs, unl)
                             or _photographer_by_files(r.get("filenames"), prefix_map))
        r["location"] = r.get("location") or ""
        r["location_clean"] = clean_location(r["location"]) if r["location"] else ""
        r["code"] = r.pop("coupon_code", "") or ""
        if r["channel"] == "online":
            try:
                recent = (now - datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)).days < main.DOWNLOAD_EXPIRE_DAYS
            except Exception:
                recent = False
            r["link"] = ("copy" if recent else "regen") if r.get("photo_paths") else ""   # prints only: no download
        else:
            r["link"] = "package" if r.get("package_token") else ""
    return rows


def _sales_range(days: int, date_from: str, date_to: str, all_time: int):
    if all_time:
        return "", ""
    if date_from and date_to:
        return date_from, date_to
    today = datetime.now(_MT).strftime("%Y-%m-%d")
    return _shift_day(today, -(max(1, min(int(days or 30), 3660)) - 1)), today


@router.get("/api/admin/sales")
def api_admin_sales(request: Request, days: int = 30, date_from: str = "", date_to: str = "", all_time: int = 0):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    for d in (date_from, date_to):
        if d and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return JSONResponse(status_code=400, content={"error": "Dates must be YYYY-MM-DD"})
    start, end = _sales_range(days, date_from, date_to, all_time)
    rows = _sales_between(start, end)
    partners = [{"id": p.get("id"), "name": p.get("name")} for p in main._load_partners() if p.get("id")]
    return {"date_from": start, "date_to": end, "rows": rows, "categories": SALE_CATEGORIES,
            "partners": partners, "stripe_mode": __import__("core.stripe_api", fromlist=["mode_of"]).mode_of(main.STRIPE_SECRET_KEY)}
