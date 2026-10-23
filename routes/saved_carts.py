"""Saved carts: customers save their gallery selection with a first name and email and load it
again on any device; the studio sees them under Customers > Saved Carts."""
import json

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import main

router = APIRouter()


def _visitor(request: Request) -> str:
    return main._client_ip(request)


@router.post("/api/saved-cart")
def save_cart(request: Request, body: dict = Body(...)):
    """Save this visitor's cart under their email (an empty cart removes it)."""
    if not main._saved_cart_save_limiter.is_allowed(_visitor(request)):
        return JSONResponse(status_code=429, content={"error": "Too many saves — please try again in a few minutes"})
    email = str(body.get("email") or "").strip()
    first = str(body.get("first_name") or "").strip()
    cart = body.get("cart")
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Please enter a valid email"})
    if not first:
        return JSONResponse(status_code=400, content={"error": "Please enter your first name"})
    if not isinstance(cart, dict):
        return JSONResponse(status_code=400, content={"error": "Nothing to save"})
    if len(json.dumps(cart)) > main.SAVED_CART_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "Your selection is too large to save"})
    summary = main._save_cart_record(email, first, cart)
    return {"ok": True, "items": summary["items"]}


@router.post("/api/saved-cart/load")
def load_cart(request: Request, body: dict = Body(...)):
    """Load the cart saved under an email (limited per visitor, so emails can't be guessed)."""
    if not main._saved_cart_load_limiter.is_allowed(_visitor(request)):
        return JSONResponse(status_code=429, content={"error": "Too many tries — please wait a few minutes"})
    email = str(body.get("email") or "").strip()
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Please enter a valid email"})
    rec = main._load_saved_cart(main._saved_cart_id(email))
    if not rec or not (rec.get("summary") or {}).get("items"):
        return JSONResponse(status_code=404, content={"error": "No saved photos for that email"})
    return {"first_name": rec.get("first_name", ""), "email": rec["email"], "cart": rec["cart"]}


# ── Studio: Customers > Saved Carts ───────────────────────────────────────────

@router.get("/api/admin/saved-carts")
def admin_saved_carts(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rows = []
    for r in main._saved_cart_list():
        s = r.get("summary") or {}
        rows.append({"id": r["id"], "email": r.get("email", ""), "first_name": r.get("first_name", ""),
                     "updated": r.get("updated", ""), "created": r.get("created", ""),
                     "photos": s.get("photos", 0), "poses": s.get("poses", 0), "prints": s.get("prints", 0),
                     "frames": s.get("frames", 0), "items": s.get("items", 0), "folders": s.get("folders", [])})
    return {"carts": rows}


def _cart_detail(rec: dict) -> dict:
    """A saved cart laid out for the studio and the POS: per folder its date, location and
    family (from the gallery's folder key), selected photos, poses and prints."""
    folders = []
    for key, fc in (rec.get("cart", {}).get("folderCarts") or {}).items():
        if not isinstance(fc, dict):
            continue
        parts = str(key).split("||")
        poses = []
        pose_list = fc.get("poses") if isinstance(fc.get("poses"), list) else []
        for color, state in (fc.get("poseState") or {}).items():
            pose = next((p for p in pose_list if isinstance(p, dict) and p.get("color") == color), {})
            pp = [p for p in (pose.get("paths") or []) if isinstance(p, str)]
            if state == "pose":
                poses.append({"label": pose.get("label", "Pose"), "whole": True, "paths": pp})
            elif isinstance(state, list):
                poses.append({"label": pose.get("label", "Pose"), "whole": False,
                              "paths": [pp[i] for i in state if isinstance(i, int) and 0 <= i < len(pp)]})
        folders.append({"label": fc.get("label") or key, "location": fc.get("location", "") or (parts[1] if len(parts) > 1 else ""),
                        "date": parts[0] if parts else "", "family": parts[2] if len(parts) > 2 else "",
                        "photos": [p for p in (fc.get("selectedPaths") or []) if isinstance(p, str)],
                        "poses": poses,
                        "prints": [{"path": p.get("photoPath", ""), "size": p.get("size", ""), "size_idx": p.get("sizeIdx", 0),
                                    "frame": p.get("frame", ""), "logo": bool(p.get("_logo"))}
                                   for p in (fc.get("printCart") or []) if isinstance(p, dict)]})
    return {"id": rec["id"], "email": rec["email"], "first_name": rec.get("first_name", ""),
            "created": rec.get("created", ""), "updated": rec.get("updated", ""),
            "summary": rec.get("summary", {}), "folders": folders, "reminders": rec.get("reminders") or [],
            "frames": [f for f in (rec.get("cart", {}).get("frameCart") or []) if isinstance(f, dict)]}


@router.get("/api/admin/saved-carts/{cart_id}")
def admin_saved_cart(cart_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rec = main._load_saved_cart(cart_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return _cart_detail(rec)


@router.get("/api/pos/saved-cart")
def pos_saved_cart(request: Request, email: str = ""):
    """POS: the online saved cart of the customer staff just picked (by their email)."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    email = (email or "").strip()
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=404, content={"error": "No saved cart"})
    rec = main._load_saved_cart(main._saved_cart_id(email))
    if not rec or not (rec.get("summary") or {}).get("items"):
        return JSONResponse(status_code=404, content={"error": "No saved cart"})
    # The same person in the store's customer list, if they're in it (so the sale gets their
    # phone and address too when the cart is opened from the Saved Online tab)
    known = next((c for c in main._load_customers()
                  if (c.get("email") or "").strip().lower() == rec["email"].lower()), None)
    return {**_cart_detail(rec), "customer": known}


@router.get("/api/pos/saved-carts")
def pos_saved_carts(request: Request, q: str = ""):
    """POS 'Saved Online' tab: customers who saved photos on the website, found by first name or
    email (when a customer says they saved some), most recently changed first."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    q = (q or "").strip().lower()
    rows = []
    for r in main._saved_cart_list():
        s = r.get("summary") or {}
        if not s.get("items"):
            continue
        if q and q not in f"{r.get('first_name', '')} {r.get('email', '')}".lower():
            continue
        rows.append({"first_name": r.get("first_name", ""), "email": r.get("email", ""), "updated": r.get("updated", ""),
                     "summary": {k: s.get(k, 0) for k in ("photos", "poses", "prints", "frames")}})
        if len(rows) >= 50:
            break
    return {"carts": rows}


@router.delete("/api/admin/saved-carts/{cart_id}")
def admin_delete_saved_cart(cart_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._load_saved_cart(cart_id):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    main._delete_saved_cart(cart_id)
    return {"deleted": True}


@router.post("/api/admin/saved-carts/clear-all")
def admin_clear_saved_carts(request: Request, body: dict = Body(...)):
    """End of season: remove every saved cart. Needs the word CLEAR typed to confirm."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if body.get("confirm") != "CLEAR":
        return JSONResponse(status_code=400, content={"error": "Type CLEAR to confirm"})
    ids = [r["id"] for r in main._saved_cart_list()]
    for cid in ids:
        main._delete_saved_cart(cid)
    return {"cleared": len(ids)}


def _money(v, what):
    try:
        n = round(float(v), 2)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if not 0 <= n <= 10000:
        raise ValueError(f"{what} must be between $0 and $10,000")
    return n


@router.get("/api/admin/saved-carts/{cart_id}/invoice-lines")
def admin_invoice_lines(cart_id: str, request: Request):
    """The invoice form: each item of the saved cart at its normal price (bundle savings
    included), and the shipping, so the studio can change any price before sending."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rec = main._load_saved_cart(cart_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    order = main._saved_cart_order_body(rec)
    try:
        priced = main._price_order(order)
    except main.CheckoutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"lines": [{"name": ln["name"].strip(), "price": float(ln["total"])} for ln in priced["line_items"]],
            "shipping": float(priced["summary"]["shipping"] or 0), "total": priced["summary"]["total"],
            "ships": bool(order["prints"] or order["frames"])}


@router.post("/api/admin/saved-carts/{cart_id}/invoice")
def admin_send_invoice(cart_id: str, request: Request, body: dict = Body(...)):
    """Turn a saved cart into an online order and email the customer a 'Pay now' link to the
    normal checkout. Each item's price can be changed and items added; the checkout lists them
    at those prices. When it's paid, WooCommerce's paid message delivers it like any online order."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rec = main._load_saved_cart(cart_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    order = main._saved_cart_order_body(rec)
    email = str(body.get("email") or rec["email"]).strip()
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Enter a valid email"})
    first = str(body.get("first_name") or rec.get("first_name") or "").strip()
    last = str(body.get("last_name") or "").strip()
    billing = {"first_name": first, "last_name": last, "email": email}
    shipping = {"first_name": first, "last_name": last}
    if order["prints"] or order["frames"]:
        addr = {k: str(body.get(k) or "").strip() for k in ("address_1", "city", "state", "postcode")}
        if not all(addr.values()):
            return JSONResponse(status_code=400, content={"error": "Prints ship: enter the street, city, state and ZIP"})
        billing.update(addr, country="US")
        shipping.update(addr, country="US")
    try:
        prices = [_money(p, "Each price") for p in body["prices"]] if isinstance(body.get("prices"), list) else None
        extra = []
        for x in body.get("extra") or []:
            name = str((x or {}).get("name") or "").strip()[:120]
            if not name:
                raise ValueError("Each added item needs a description")
            extra.append({"name": name, "price": _money(x.get("price"), f"The price of {name}")})
        ship = _money(body["shipping"], "Shipping") if str(body.get("shipping", "")).strip() != "" else None
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    made = main._create_checkout({**order, "email": email, "billing": billing, "shipping": shipping},
                                 line_prices=prices, extra_lines=extra, shipping_price=ship)
    if isinstance(made, JSONResponse):
        return made
    sent = main._send_invoice_email(email, first, made["checkout_url"], float(made["total"]))
    main._save_invoice({"order_id": made["order_id"], "cart_id": cart_id, "first_name": first, "last_name": last,
                        "email": email, "total": made["total"], "normal_total": made["normal_total"],
                        "pay_url": made["checkout_url"], "emailed": sent,
                        "created": main.datetime.now(main.timezone.utc).isoformat()})
    return {"pay_url": made["checkout_url"], "total": made["total"], "emailed": sent,
            "normal_total": made["normal_total"], "order_id": made["order_id"]}


@router.get("/api/admin/invoices")
def admin_invoices(request: Request):
    """Customers > Invoices: every invoice sent and whether it's been paid."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"invoices": main._load_invoices()}


@router.post("/api/admin/invoices/{order_id}/cancel")
def admin_cancel_invoice(order_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not order_id.isdigit():
        return JSONResponse(status_code=404, content={"error": "Not found"})
    problem = main._cancel_invoice(order_id)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    return {"cancelled": True}


@router.post("/api/admin/saved-carts/{cart_id}/remind")
def admin_send_reminder(cart_id: str, request: Request):
    """Studio: email this customer a reminder about their saved photos (sent by hand, not automatic)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rec = main._load_saved_cart(cart_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    problem = main._send_saved_cart_reminder(rec)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    return {"sent": True, "reminders": rec["reminders"]}


@router.get("/api/saved-cart/link")
def load_cart_from_link(request: Request, t: str = ""):
    """The 'View your photos' link in a reminder: opens that saved cart directly."""
    if not main._saved_cart_load_limiter.is_allowed(_visitor(request)):
        return JSONResponse(status_code=429, content={"error": "Too many tries — please wait a few minutes"})
    cart_id = main._saved_cart_from_link(t)
    rec = main._load_saved_cart(cart_id) if cart_id else None
    if not rec or not (rec.get("summary") or {}).get("items"):
        return JSONResponse(status_code=404, content={"error": "These saved photos aren't available anymore"})
    return {"first_name": rec.get("first_name", ""), "email": rec["email"], "cart": rec["cart"]}


@router.post("/api/admin/saved-carts/bulk")
def admin_saved_carts_bulk(request: Request, body: dict = Body(...)):
    """Studio: send reminders to, or delete, several saved carts at once."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    action = body.get("action")
    ids = [i for i in dict.fromkeys(body.get("ids") or []) if isinstance(i, str)][:500]
    if action not in ("remind", "delete") or not ids:
        return JSONResponse(status_code=400, content={"error": "Pick some saved carts and an action"})
    sent, skipped, deleted, missing = 0, [], 0, 0
    for cid in ids:
        rec = main._load_saved_cart(cid)
        if not rec:
            missing += 1
            continue
        if action == "delete":
            main._delete_saved_cart(cid)
            deleted += 1
            continue
        problem = main._send_saved_cart_reminder(rec)
        if problem:
            skipped.append({"email": rec.get("email", ""), "reason": problem})
        else:
            sent += 1
    return {"sent": sent, "skipped": skipped, "deleted": deleted, "missing": missing}
