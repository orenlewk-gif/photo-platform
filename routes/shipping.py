"""Routes: shipping labels for print and frame orders (Shippo).

On an order that ships, the Orders page asks for rates (our ship-from address, the customer's
address, one of the saved packages), buys the chosen one as a label (a PDF to print, plus a
tracking number), and emails the customer the tracking link. A label bought by mistake can be
voided (Shippo refunds unused labels). Orders shipped some other way can be marked shipped with
their tracking number by hand.

Each sale's shipment is its own file, shipments/<wc|pos>_<order id>.json, so it works the same
for our online orders, old WooCommerce orders and store sales."""
import json
import re
import threading
from datetime import datetime, timezone
from html import escape as _html_escape

import requests as http_requests
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import main
from core import shippo_api
from core.storage import JsonDoc

router = APIRouter()

SHIPPING_SETTINGS_KEY = "meta/shipping_settings.json"
SHIPMENTS_PREFIX = "shipments/"
LABEL_FORMATS = ("PDF_4x6", "PDF")              # a 4x6 label printer, or a letter-size sheet
DEFAULT_FROM = {"name": "Crystal Images", "company": "Crystal Images Professional Photography",
                "street1": "PO Box 162712", "street2": "", "city": "Big Sky", "state": "MT",
                "zip": "59716", "country": "US", "phone": "", "email": "info@bigskyphotos.com"}
_ADDRESS_FIELDS = ("name", "company", "street1", "street2", "city", "state", "zip", "country", "phone", "email")

_settings_doc = JsonDoc(main.s3, main.R2_BUCKET, SHIPPING_SETTINGS_KEY, ttl=30, indent=1, default=dict,
                        valid=lambda d: isinstance(d, dict))
_cache: dict = {}                     # storage key -> (ETag, shipment)
_cache_lock = threading.Lock()


# ── settings ──────────────────────────────────────────────────────────────────

def shipping_settings() -> dict:
    d = _settings_doc.load()
    return {"from": {**DEFAULT_FROM, **(d.get("from") or {})},
            "packages": list(d.get("packages") or []),
            "label_format": d.get("label_format") if d.get("label_format") in LABEL_FORMATS else "PDF_4x6"}


def _num(v, lo: float, hi: float):
    try:
        n = round(float(v), 2)
    except (TypeError, ValueError):
        return None
    return n if lo < n <= hi else None


def _clean_package(p: dict) -> dict | None:
    name = str(p.get("name") or "").strip()[:60]
    dims = {k: _num(p.get(k), 0, 120) for k in ("length", "width", "height")}
    weight = _num(p.get("weight"), 0, 150)
    if not name or None in dims.values() or weight is None:
        return None
    pid = re.sub(r"[^a-z0-9-]", "", str(p.get("id") or "").lower())[:40] or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return {"id": pid or "box", "name": name, **dims, "weight": weight}


def _clean_address(a: dict, need_contact: bool = False) -> tuple[dict | None, str]:
    a = {k: str((a or {}).get(k) or "").strip()[:100] for k in _ADDRESS_FIELDS}
    a["country"] = (a["country"] or "US").upper()[:2]
    a["state"] = a["state"].upper() if a["country"] == "US" else a["state"]
    missing = [label for k, label in (("name", "name"), ("street1", "street address"), ("city", "city"),
                                      ("state", "state"), ("zip", "ZIP code")) if not a[k]]
    if missing:
        return None, "The address needs a " + ", ".join(missing)
    if need_contact and not (a["phone"] or a["email"]):
        return None, "The ship-from address needs a phone number or an email"
    return {k: v for k, v in a.items() if v}, ""


@router.get("/api/admin/shipping/settings")
def get_shipping_settings(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)       # Admin area: the owner's PIN
    if denied:
        return denied
    return {**shipping_settings(), "ready": bool(main.SHIPPO_API_KEY),
            "mode": shippo_api.mode_of(main.SHIPPO_API_KEY)}


@router.put("/api/admin/shipping/settings")
def put_shipping_settings(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)       # Admin area: the owner's PIN
    if denied:
        return denied
    frm, err = _clean_address(body.get("from") or {}, need_contact=True)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    packages, seen = [], set()
    for p in body.get("packages") or []:
        clean = _clean_package(p if isinstance(p, dict) else {})
        if not clean:
            return JSONResponse(status_code=400, content={"error": "Each package needs a name, its size in inches and its weight in pounds"})
        while clean["id"] in seen:
            clean["id"] += "-2"
        seen.add(clean["id"])
        packages.append(clean)
    fmt = body.get("label_format") if body.get("label_format") in LABEL_FORMATS else "PDF_4x6"
    _settings_doc.save({"from": frm, "packages": packages[:40], "label_format": fmt})
    return {"ok": True, **shipping_settings()}


# ── one shipment per sale ─────────────────────────────────────────────────────

def _file_for(key: str) -> str:
    return f"{SHIPMENTS_PREFIX}{key.replace(':', '_', 1)}.json"


def load_shipment(key: str) -> dict | None:
    try:
        return json.loads(main.s3.get_object(Bucket=main.R2_BUCKET, Key=_file_for(key))["Body"].read())
    except Exception:
        return None


def _save_shipment(rec: dict) -> None:
    rec["updated"] = datetime.now(timezone.utc).isoformat()
    main.s3.put_object(Bucket=main.R2_BUCKET, Key=_file_for(rec["key"]), Body=json.dumps(rec).encode(),
                       ContentType="application/json")


def all_shipments() -> dict:
    """Every sale's shipment, by its order key ("wc:CI-1001", "pos:pos-1a2b3c4d")."""
    listed = []
    try:
        for page in main.s3.get_paginator("list_objects_v2").paginate(Bucket=main.R2_BUCKET, Prefix=SHIPMENTS_PREFIX):
            listed += [(o["Key"], o.get("ETag", "")) for o in page.get("Contents", [])]
    except Exception:
        return {}
    with _cache_lock:
        stale = [k for k, e in listed if _cache.get(k, ("",))[0] != e or not e]
    for k in stale:
        try:
            rec = json.loads(main.s3.get_object(Bucket=main.R2_BUCKET, Key=k)["Body"].read())
        except Exception:
            continue
        with _cache_lock:
            _cache[k] = (dict(listed).get(k, ""), rec)
    with _cache_lock:
        return {_cache[k][1]["key"]: _public(_cache[k][1]) for k, _ in listed if k in _cache}


def _public(rec: dict) -> dict:
    """What the Orders page shows (not the rates offered along the way)."""
    return {k: rec.get(k) for k in ("key", "status", "carrier", "service", "tracking_number", "tracking_url",
                                     "label_url", "label_cost", "package", "to", "test", "label_at", "shipped_at",
                                     "emailed_at", "voided_at", "void_status", "manual")}


def _sale_contact(key: str) -> dict | None:
    """Who the sale is for and where it ships, from the order itself."""
    kind, _, oid = key.partition(":")
    if kind == "wc" and main._ONLINE_ID_RE.match(oid):
        o = main._load_online_order(oid)
        if not o:
            return None
        return _contact_from_wc_shape(o, oid)
    if kind == "wc":
        if not (main.WC_KEY and main.WC_SECRET):
            return None
        try:
            r = http_requests.get(f"{main.WC_BASE}/orders/{oid}", auth=(main.WC_KEY, main.WC_SECRET), timeout=15)
            return _contact_from_wc_shape(r.json(), f"#{oid}") if r.ok else None
        except Exception:
            return None
    o = next((x for x in main._fetch_pos_orders() if str(x.get("id")) == oid), None)
    if not o:
        return None
    first, _, last = str(o.get("name") or "").partition(" ")
    return {"order_no": o.get("square_receipt_number") or oid, "first": first, "last": last,
            "email": o.get("email", ""), "phone": o.get("phone", ""),
            "to": {"name": str(o.get("name") or ""), "street1": str(o.get("address") or ""), "country": "US",
                   "email": o.get("email", ""), "phone": o.get("phone", "")}}


def _contact_from_wc_shape(o: dict, order_no: str) -> dict:
    b, s = o.get("billing") or {}, o.get("shipping") or {}
    src = s if s.get("address_1") else b
    return {"order_no": order_no, "first": b.get("first_name", ""), "last": b.get("last_name", ""),
            "email": b.get("email", ""), "phone": b.get("phone", ""),
            "to": {"name": f"{src.get('first_name', '')} {src.get('last_name', '')}".strip(),
                   "company": src.get("company", ""), "street1": src.get("address_1", ""),
                   "street2": src.get("address_2", ""), "city": src.get("city", ""), "state": src.get("state", ""),
                   "zip": src.get("postcode", ""), "country": src.get("country") or "US",
                   "email": b.get("email", ""), "phone": b.get("phone", "")}}


def _key_or_error(key) -> tuple[str, JSONResponse | None]:
    key = str(key or "")
    if not main._REFUND_KEY_RE.match(key):
        return key, JSONResponse(status_code=400, content={"error": "Unknown order"})
    return key, None


TRACKING_URLS = {"usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}",
                 "ups": "https://www.ups.com/track?tracknum={n}",
                 "fedex": "https://www.fedex.com/fedextrack/?trknbr={n}"}


@router.get("/api/admin/shipping/order")
def shipping_for_order(request: Request, key: str = ""):
    """The order's shipment (if any), and the address to ship to for a new one."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(key)
    if err:
        return err
    contact, rec = _sale_contact(key), load_shipment(key)
    return {"shipment": _public(rec) if rec else None,
            "to": (contact or {}).get("to") or {}, "settings": shipping_settings(),
            "ready": bool(main.SHIPPO_API_KEY), "mode": shippo_api.mode_of(main.SHIPPO_API_KEY)}


@router.post("/api/admin/shipping/rates")
def shipping_rates(request: Request, body: dict = Body(...)):
    """Rates from the carriers for this order, address and package (cheapest first)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(body.get("key"))
    if err:
        return err
    settings = shipping_settings()
    to, why = _clean_address(body.get("to") or {})
    if not to:
        return JSONResponse(status_code=400, content={"error": why})
    frm, why = _clean_address(settings["from"], need_contact=True)
    if not frm:
        return JSONResponse(status_code=400, content={"error": why + " (Settings > Shipping labels)"})
    pkg = next((p for p in settings["packages"] if p["id"] == body.get("package_id")), None) \
        or _clean_package({"name": "Custom", **(body.get("parcel") or {})})
    if not pkg:
        return JSONResponse(status_code=400, content={"error": "Pick a package, or enter its size in inches and weight in pounds"})
    parcel = {"length": str(pkg["length"]), "width": str(pkg["width"]), "height": str(pkg["height"]),
              "distance_unit": "in", "weight": str(pkg["weight"]), "mass_unit": "lb"}
    with main._sale_locks.hold(f"ship:{key}"):
        rec = load_shipment(key) or {"key": key}
        if rec.get("status") in ("label", "shipped"):
            return JSONResponse(status_code=409, content={"error": "This order already has a label. Void it first to buy another."})
        try:
            shipment = shippo_api.call("POST", "shipments", main.SHIPPO_API_KEY,
                                       {"address_from": frm, "address_to": to, "parcels": [parcel], "async": False})
        except shippo_api.ShippoError as e:
            return JSONResponse(status_code=502, content={"error": str(e)})
        rates = sorted((shippo_api.rate_summary(r) for r in shipment.get("rates") or []), key=lambda r: r["amount"])
        notes = [m.get("text", "") for m in shipment.get("messages") or [] if isinstance(m, dict) and m.get("text")]
        if not rates:
            return JSONResponse(status_code=400, content={"error": "No rates came back for this address and package"
                                                          + (": " + "; ".join(notes[:3]) if notes else "")})
        rec.update(status="rated", shippo_shipment=shipment.get("object_id", ""), to=to,
                   package={"name": pkg["name"], **{k: pkg[k] for k in ("length", "width", "height", "weight")}},
                   rates={r["id"]: r for r in rates}, test=shippo_api.mode_of(main.SHIPPO_API_KEY) == "test")
        _save_shipment(rec)
    return {"rates": rates, "notes": notes[:5]}


@router.post("/api/admin/shipping/buy")
def shipping_buy(request: Request, body: dict = Body(...)):
    """Buy one of the rates just shown as a label; email the customer the tracking link."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(body.get("key"))
    if err:
        return err
    with main._sale_locks.hold(f"ship:{key}"):
        rec = load_shipment(key)
        if not rec or rec.get("status") != "rated":
            return JSONResponse(status_code=409, content={"error": "Get rates for this order first"})
        rate = (rec.get("rates") or {}).get(str(body.get("rate_id") or ""))
        if not rate:
            return JSONResponse(status_code=400, content={"error": "That rate isn't one shown for this order. Get rates again."})
        try:
            tx = shippo_api.call("POST", "transactions", main.SHIPPO_API_KEY,
                                 {"rate": rate["id"], "label_file_type": shipping_settings()["label_format"], "async": False})
        except shippo_api.ShippoError as e:
            return JSONResponse(status_code=502, content={"error": str(e)})
        if tx.get("status") != "SUCCESS" or not tx.get("label_url"):
            notes = "; ".join(m.get("text", "") for m in tx.get("messages") or [] if isinstance(m, dict))
            return JSONResponse(status_code=502, content={"error": "Shippo couldn't make the label" + (f": {notes}" if notes else "")})
        now = datetime.now(timezone.utc).isoformat()
        rec.update(status="label", transaction=tx.get("object_id", ""), carrier=rate["carrier"], service=rate["service"],
                   label_cost=rate["amount"], tracking_number=tx.get("tracking_number", ""),
                   tracking_url=tx.get("tracking_url_provider") or "", label_url=tx.get("label_url", ""),
                   label_at=now, shipped_at=now, manual=False, rates={})
        _save_shipment(rec)
    emailed = _email_customer(key, rec) if body.get("email_customer", True) else False
    return {"ok": True, "shipment": _public(load_shipment(key) or rec), "emailed": emailed}


@router.post("/api/admin/shipping/manual")
def shipping_manual(request: Request, body: dict = Body(...)):
    """An order shipped some other way (at the post office): its carrier and tracking number."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(body.get("key"))
    if err:
        return err
    carrier = str(body.get("carrier") or "").strip()[:30]
    number = re.sub(r"\s+", "", str(body.get("tracking_number") or ""))[:60]
    if not carrier:
        return JSONResponse(status_code=400, content={"error": "Enter the carrier (USPS, UPS, FedEx...)"})
    if number and not re.fullmatch(r"[A-Za-z0-9-]{4,60}", number):
        return JSONResponse(status_code=400, content={"error": "That doesn't look like a tracking number"})
    url = TRACKING_URLS.get(carrier.lower(), "").format(n=number) if number else ""
    with main._sale_locks.hold(f"ship:{key}"):
        rec = load_shipment(key) or {"key": key}
        if rec.get("status") == "label":
            return JSONResponse(status_code=409, content={"error": "This order has a Shippo label. Void it first."})
        now = datetime.now(timezone.utc).isoformat()
        rec.update(status="shipped", carrier=carrier, service="", tracking_number=number, tracking_url=url,
                   label_url="", label_cost=0, shipped_at=now, manual=True, rates={})
        _save_shipment(rec)
    emailed = _email_customer(key, rec) if body.get("email_customer", True) else False
    return {"ok": True, "shipment": _public(load_shipment(key) or rec), "emailed": emailed}


@router.post("/api/admin/shipping/void")
def shipping_void(request: Request, body: dict = Body(...)):
    """Take back a label (Shippo refunds it if it's never scanned), or undo a hand-entered shipment."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(body.get("key"))
    if err:
        return err
    with main._sale_locks.hold(f"ship:{key}"):
        rec = load_shipment(key)
        if not rec or rec.get("status") not in ("label", "shipped"):
            return JSONResponse(status_code=409, content={"error": "This order has no label to void"})
        void_status = ""
        if rec.get("transaction"):
            try:
                ref = shippo_api.call("POST", "refunds", main.SHIPPO_API_KEY, {"transaction": rec["transaction"], "async": False})
                void_status = str(ref.get("status") or "")
            except shippo_api.ShippoError as e:
                return JSONResponse(status_code=502, content={"error": str(e)})
            if void_status == "ERROR":
                return JSONResponse(status_code=409, content={"error": "Shippo won't refund this label: it has already been used"})
        rec.update(status="voided", void_status=void_status, voided_at=datetime.now(timezone.utc).isoformat(),
                   previous={k: rec.get(k) for k in ("carrier", "service", "tracking_number", "label_cost", "transaction")})
        for k in ("tracking_number", "tracking_url", "label_url", "transaction"):
            rec[k] = ""
        _save_shipment(rec)
    return {"ok": True, "shipment": _public(rec)}


@router.post("/api/admin/shipping/email")
def shipping_email_again(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key, err = _key_or_error(body.get("key"))
    if err:
        return err
    rec = load_shipment(key)
    if not rec or rec.get("status") not in ("label", "shipped"):
        return JSONResponse(status_code=409, content={"error": "This order hasn't shipped yet"})
    if not _email_customer(key, rec):
        return JSONResponse(status_code=502, content={"error": "The email couldn't be sent (no email on the order?)"})
    return {"ok": True}


# ── the customer's "on its way" email ─────────────────────────────────────────

def _email_customer(key: str, rec: dict) -> bool:
    contact = _sale_contact(key) or {}
    email = contact.get("email", "")
    if not main.RESEND_API_KEY or not main._EMAIL_RE.match(email or ""):
        return False
    tmpl = main._load_email_templates().get("order_shipped", main._EMAIL_TEMPLATE_DEFAULTS["order_shipped"])
    url = rec.get("tracking_url") or ""
    button = (f'<p style="margin:0 0 1rem"><a href="{_html_escape(url)}" style="background:#1a73e8;color:#fff;'
              f'padding:.55rem 1.25rem;border-radius:6px;font-size:.88rem;font-weight:600;text-decoration:none;'
              f'display:inline-block">Track Your Package</a></p>') if url else ""
    plain = {"first_name": (contact.get("first") or "").strip() or "there", "order_id": contact.get("order_no", ""),
             "carrier": rec.get("carrier", ""), "tracking_number": rec.get("tracking_number") or "(none)",
             "tracking_button": url}
    body_tmpl = tmpl["body"]
    if button and "{{tracking_button}}" not in body_tmpl:
        body_tmpl += "\n\n{{tracking_button}}"
    subject = tmpl["subject"]
    for k, v in plain.items():
        subject = subject.replace(f"{{{{{k}}}}}", str(v))
    body_html, body_text = main._render_email_body(body_tmpl, plain, {"tracking_button": button})
    try:
        r = http_requests.post("https://api.resend.com/emails",
                               headers={"Authorization": f"Bearer {main.RESEND_API_KEY}", "Content-Type": "application/json"},
                               json={"from": main.RESEND_FROM, "to": [email], "subject": subject,
                                     "html": main._wrap_email_html(body_html), "text": body_text}, timeout=10)
    except Exception as e:
        print(f"Shipping email failed ({key}): {e}")
        return False
    if not r.ok:
        print(f"Shipping email error ({key}): {r.text}")
        return False
    with main._sale_locks.hold(f"ship:{key}"):
        fresh = load_shipment(key)
        if fresh:
            fresh["emailed_at"] = datetime.now(timezone.utc).isoformat()
            fresh["emailed_to"] = email
            _save_shipment(fresh)
            rec.update(emailed_at=fresh["emailed_at"])
    return True
