"""Routes: pos. Moved from main.py unchanged; shared helpers still live in main.py."""
import hmac
import secrets
import time as _time
from datetime import datetime
from datetime import timezone
from fastapi import Body
from fastapi import Query
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/pos-dates")
def pos_dates(request: Request):
    """POS: return unique (date, location) pairs for the date/location dropdowns."""
    if not main._pos_device_ok(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return main._folder_index()["pos_dates"]


@router.get("/api/pos-families")
def pos_families(request: Request, date: str = Query(""), location: str = Query("")):
    """POS: return unique families/groups for a given date+location with preview paths."""
    from collections import defaultdict
    if not main._pos_device_ok(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    families: dict = {}
    previews: dict = defaultdict(list)
    for item in main._folder_photos(date, location, pos=True):
        ln = item.get("last_name", "").strip()
        grp = item.get("group", "").strip()
        name = ln or grp
        key = name.lower() if name else "__flat__"
        if not name:
            if "__flat__" not in families:
                families["__flat__"] = {"name": "", "last_name": "", "group": "", "date": date, "location": location}
        else:
            if key not in families:
                families[key] = {"name": name, "last_name": ln, "group": grp, "date": date, "location": location}
        if len(previews[key]) < 4:
            path = item.get("path", "")
            if path:
                previews[key].append(path)
    results = [
        {**v, "previews": previews[k]}
        for k, v in sorted(families.items(), key=lambda x: x[0] if x[0] != "__flat__" else "")
    ]
    return {"families": results}


@router.post("/api/pos-checkout")
async def pos_checkout(request: Request):
    """Record a completed in-person (Square) sale and deliver the download link."""
    try:
        body = await request.json()
        if not main._pos_device_approved(request):
            return JSONResponse(status_code=401, content={"error": "This device isn't approved for the POS"})
        if not main._pin_photographer(request, body.get("pin", "")):
            return main._pin_refusal(request)
        return await run_in_threadpool(main._record_pos_sale, body)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/api/pos/square-pending")
def pos_square_pending(request: Request, body: dict = Body(...)):
    """Save a sale just before the POS hands it to Square."""
    state = str(body.get("state") or "")
    sale = body.get("sale") or {}
    if not main._POS_STATE_RE.match(state):
        return JSONResponse(status_code=400, content={"error": "Invalid sale reference"})
    photog = main._pos_pin_photographer(request)
    if not photog:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    if not (sale.get("email") or "").strip():
        return JSONResponse(status_code=400, content={"error": "Customer email required"})
    _, problem = main._check_unlimited_cart(sale.get("items") or [])
    if problem:   # caught here, before Square takes the payment
        return JSONResponse(status_code=400, content={"error": problem})
    existing = main._load_pos_pending(state)
    if existing and existing.get("status") == "completed":
        return JSONResponse(status_code=409, content={"error": "This sale was already completed"})
    sale = {**sale, "pin": request.headers.get("X-POS-PIN", "").strip()}
    sale.pop("square_payment_id", None)
    main._save_pos_pending(state, {"state": state, "status": "pending", "sale": sale,
                              "photographer_id": photog.get("id", ""),
                              "created": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@router.post("/api/pos/square-complete")
def pos_square_complete(body: dict = Body(...)):
    """Finish a sale when Square returns. No PIN is needed: the state is a random id
    only the POS and Square know, and the payment is verified with Square."""
    state = str(body.get("state") or "")
    transaction_id = str(body.get("transaction_id") or "")
    if not main._POS_STATE_RE.match(state):
        return JSONResponse(status_code=404, content={"error": "This sale wasn't found — check the Square app"})
    # If Safari and the POS app both finish the same sale, the second waits and
    # then gets the first one's result instead of recording it again
    with main._sale_locks.hold("state:" + state):
        return main._complete_pending_sale(state, transaction_id)


@router.get("/api/pos/square-pending/{state}")
def pos_square_pending_status(state: str, request: Request):
    """Lets the Home Screen POS learn that its sale was finished in Safari."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    pending = main._load_pos_pending(state)
    if not pending:
        return JSONResponse(status_code=404, content={"status": "unknown"})
    return {"status": pending.get("status"), "result": pending.get("result")}


@router.post("/api/pos/square-prepare")
def pos_square_prepare(request: Request, body: dict = Body(...)):
    """Everything the POS needs to open the Square app for a sale: app ID + customer id."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    if not main.SQUARE_APP_ID:
        return JSONResponse(status_code=503, content={"error": "Square isn't set up (SQUARE_APP_ID is missing)"})
    if not main.SQUARE_ACCESS_TOKEN:
        return JSONResponse(status_code=503, content={
            "error": "Square isn't set up (SQUARE_ACCESS_TOKEN is missing) — payments couldn't be confirmed"})
    customer_id, customer_problem = None, None
    try:
        customer_id, customer_problem = main._square_customer_id(body.get("customer") or {})
    except main.SquareTokenRejected:
        return JSONResponse(status_code=503, content={
            "error": "Square rejected the access token, so payments couldn't be confirmed. "
                     "Check it in Dashboard > Settings > Test Square connection before charging."})
    except Exception as e:
        print(f"Square customer lookup failed: {e}")
        customer_problem = "Couldn't reach Square to add the customer"
    return {"client_id": main.SQUARE_APP_ID, "customer_id": customer_id, "customer_problem": customer_problem}


@router.get("/api/pos-theme")
def api_pos_theme():
    cfg = main._load_settings()
    c = cfg.get("frontend", {})
    return {
        "accent":  main._safe_color(c.get("accent",   "#F5C430"), "#F5C430"),
        "bg":      main._safe_color(c.get("page_bg",  "#093A52"), "#093A52"),
        "surface": main._safe_color(c.get("panel_bg", "#093A53"), "#093A53"),
    }


@router.get("/api/pos-products")
def api_pos_products():
    return main._load_pos_products()


@router.get("/api/pos-recent-sales")
def api_pos_recent_sales(request: Request, limit: int = Query(50)):
    if not (main._admin_authed(request) or main._pos_pin_photographer(request)):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    orders = main._fetch_pos_orders()
    return {"orders": orders[:limit]}


@router.get("/api/pos-customers")
def api_pos_customers(request: Request, q: str = Query("")):
    """Search POS customer directory. Returns up to 60 matches.
    Admin (studio) may list everything; the POS needs a PIN and a search term."""
    is_admin = main._admin_authed(request)
    if not is_admin:
        if not main._pos_pin_photographer(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        if len(q.strip()) < 2:
            return {"customers": [], "total": 0, "search_required": True}
    customers = main._load_customers()
    if not q:
        return {"customers": customers[:60], "total": len(customers)}
    results = [c for c in customers if main._customer_name_matches(c, q)]
    return {"customers": results[:60], "total": len(results)}


@router.post("/api/pos-resend")
def api_pos_resend(request: Request, body: dict = Body(...)):
    """Recent Sales: send the customer their receipt again (the same single email
    they got at checkout, with the download button when photos were bought)."""
    if not main._pos_device_approved(request):
        return JSONResponse(status_code=401, content={"error": "This device isn't approved for the POS"})
    if not main._pin_photographer(request, body.get("pin", "")):
        return main._pin_refusal(request)
    order = main._find_pos_order(str(body.get("order_id") or ""), str(body.get("date") or ""),
                            str(body.get("token") or ""))
    if not order:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    note = main._resend_pos_receipt(order)
    return {"ok": True, "sent_to": [order.get("email")], "note": note}


@router.post("/api/photographer/auth")
def photographer_auth(request: Request, body: dict = Body(...)):
    p = main._pin_photographer(request, body.get("pin", ""))
    if not p:
        return main._pin_refusal(request)
    device_ok = main._pos_device_approved(request)
    if not device_ok:
        # The POS or the time clock on a new device: the owner gets a code to approve it
        challenge = secrets.token_urlsafe(16)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        now = _time.time()
        for k in [k for k, v in main._pos_challenges.items() if v["expires"] < now]:
            main._pos_challenges.pop(k, None)
        info = main._request_info(request)
        main._pos_challenges[challenge] = {"code": main._token_hash(code), "expires": now + main.LOGIN_CODE_MINUTES * 60,
                                           "tries": 0, "photographer_id": p["id"], "info": info}
        if not main._send_pos_device_code_email(code, info, p):
            main._pos_challenges.pop(challenge, None)
            return JSONResponse(status_code=503, content={"error": "Couldn't email the code for this new device — "
                                                                   "try again in a minute"})
        return JSONResponse(status_code=403, content={"device_approval": True, "challenge": challenge,
                                                      "email": main._masked_email(main.OWNER_EMAIL)})
    return _photographer_signed_in(p, device_ok)


def _photographer_signed_in(p: dict, device_ok: bool, device_token: str = ""):
    records = main._load_clock_records()
    active = next((r for r in reversed(records)
                   if r["photographer_id"] == p["id"] and not r.get("clock_out")), None)
    resp = JSONResponse({"photographer": {k: v for k, v in p.items() if k != "pin"},
                         "clocked_in": active is not None, "active_record": active})
    if device_token:
        resp.set_cookie(main.POS_DEVICE_COOKIE, device_token, httponly=True, samesite="lax",
                        max_age=main.POS_DEVICE_DAYS * 86400)
    if device_ok:
        # Lets this device load staff (no-watermark) thumbnails; renewed each time a PIN is entered
        expires = int(_time.time()) + main.POS_COOKIE_SECONDS
        resp.set_cookie(main.POS_COOKIE, main._pos_token(expires), httponly=True, samesite="lax",
                        max_age=main.POS_COOKIE_SECONDS)
    return resp


@router.post("/api/pos/device-verify")
def pos_device_verify(request: Request, body: dict = Body(...)):
    """The code emailed for a new POS device: approves the device and signs the photographer in."""
    challenge = str(body.get("challenge") or "")
    code = str(body.get("code") or "").strip()
    pending = main._pos_challenges.get(challenge)
    if not pending or pending["expires"] < _time.time():
        main._pos_challenges.pop(challenge, None)
        return JSONResponse(status_code=401, content={"error": "That code has expired — enter your PIN again",
                                                      "restart": True})
    pending["tries"] += 1
    if not hmac.compare_digest(main._token_hash(code), pending["code"]):
        if pending["tries"] >= main.LOGIN_CODE_TRIES:
            main._pos_challenges.pop(challenge, None)
            return JSONResponse(status_code=401, content={"error": "Too many wrong codes — enter your PIN again",
                                                          "restart": True})
        return JSONResponse(status_code=401, content={"error": "Incorrect code"})
    main._pos_challenges.pop(challenge, None)
    p = next((x for x in main._load_photographers() if x.get("id") == pending["photographer_id"]), None)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Enter your PIN again", "restart": True})
    return _photographer_signed_in(p, True, main._approve_pos_device(pending["info"], p))


@router.post("/api/clock/in")
def clock_in(request: Request, body: dict = Body(...)):
    if not main._pos_device_approved(request):
        return main._device_refusal()
    p = main._pin_photographer(request, body.get("pin", ""))
    if not p:
        return main._pin_refusal(request)
    with main._clock_lock:
        return main._clock_in(p, body)


@router.post("/api/clock/out")
def clock_out(request: Request, body: dict = Body(...)):
    if not main._pos_device_approved(request):
        return main._device_refusal()
    p = main._pin_photographer(request, body.get("pin", ""))
    if not p:
        return main._pin_refusal(request)
    with main._clock_lock:
        return main._clock_out(p)


@router.post("/api/pos-unlock")
async def api_pos_unlock(request: Request):
    body = await request.json()
    code = body.get("passcode", "").strip()
    if not main.POS_PASSCODE:
        return {"ok": True}
    if code == main.POS_PASSCODE:
        return {"ok": True}
    return JSONResponse(status_code=401, content={"ok": False, "error": "Incorrect passcode"})


@router.get("/pos", response_class=HTMLResponse)
def pos_page():
    return HTMLResponse(open("templates/pos.html").read())


@router.get("/pos/manifest.json")
def pos_manifest():
    return JSONResponse({
        "name": "Crystal Images POS",
        "short_name": "CI POS",
        "start_url": "/pos",
        "display": "standalone",
        "background_color": "#082f44",
        "theme_color": "#082f44",
        "icons": [
            {"src": "/static/pos-icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/pos-icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    })


@router.get("/api/pos/download-qr")
def pos_download_qr(request: Request, token: str = Query("")):
    """Sale Complete screen: a QR code of the customer's download page, so they can scan it with
    their phone and save their photos right away. No POS sign-in is needed: Square often finishes
    the sale in Safari, which doesn't share the POS app's sign-in, and the picture only shows a
    link whoever asks already has (the token is the link)."""
    import re
    from io import BytesIO
    import segno
    from fastapi.responses import Response
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", token or "") or not main._load_pkg(token):
        return JSONResponse(status_code=404, content={"error": "Download link not found"})
    buf = BytesIO()
    segno.make(f"{main.SITE_URL}/package/{token}", error="m").save(buf, kind="svg", scale=8, border=2,
                                                                  dark="#000000", light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})
