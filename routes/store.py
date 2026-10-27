"""Routes: store mode (/store), the in-store screens where customers browse the gallery and heart
their favorite photos. The same gallery page as the website, with no prices, shop or watermarks.

Only approved store screens get in (see "Store screens" in main.py): any other device sees a
locked screen, the owner is told, and it can ask for access with a code emailed to the owner."""
import hmac
import json
import secrets
import time as _time

from fastapi import APIRouter, BackgroundTasks, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

import main
from core.pages import render_page

router = APIRouter()

_NO_INDEX = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
# Store mode is the gallery page plus these, put in where the page marks the spot
_STORE_MARK = "<!--store-mode-->"
_STORE_ASSETS = ('<link rel="stylesheet" href="/static/store-mode.css?v=9">\n'
                 '<script src="/static/store-mode.js?v=9"></script>')


def _store_page() -> str:
    html = open("templates/index.html").read()
    if _STORE_MARK not in html:
        raise RuntimeError("templates/index.html is missing the store-mode mark")
    # The promo lines' numbers come from the same settings the sale uses, so they stay true;
    # partner programs with an open page (Eagle Mount) get a header button to their store page
    promo = {"print_off": main.PRINT_WITH_DIGITAL_PCT,
             "partners": [{"name": p["name"], "slug": p["slug"]} for p in main._load_partners()
                          if p.get("slug") and p.get("password")]}
    additions = f'<script>window.STORE_MODE = true; window.STORE_PROMO = {json.dumps(promo)};</script>\n' + _STORE_ASSETS
    return html.replace(_STORE_MARK, additions, 1)


@router.get("/store", response_class=HTMLResponse)
def store_page(request: Request, background: BackgroundTasks):
    screen = main._store_screen(request)
    if screen:
        background.add_task(main._touch_store_screen, screen[0])
        return HTMLResponse(_store_page(), headers=_NO_INDEX)
    if main._admin_authed(request):
        return HTMLResponse(_store_page(), headers=_NO_INDEX)
    background.add_task(main._note_blocked_store_visit, main._request_info(request))
    return HTMLResponse(open("templates/store_locked.html").read(), status_code=403, headers=_NO_INDEX)


# ── Partner photos (Eagle Mount) on the store screens ──────────────────────
# The partner's page without its password: families pick their photos, give their name and
# email, and get them free through a download link (a QR code on the screen, and an email).
# No downloads on the store screen itself and no checkout; prints are bought at the counter.

def _store_partner_header(p: dict) -> str:
    return """<header class="st-head">
  <a class="st-logo" href="/store" aria-label="Back to the store photos"><img src="https://bigskyphotos.com/wp-content/uploads/2026/05/New-Logo-Transparent.png" alt="Crystal Images"></a>
  <div class="st-title"></div>
  <nav class="st-tools" aria-label="Store">
    <a class="st-btn" href="/store">All photos</a>
    <button type="button" class="st-btn" onclick="storeStartOver()">Start over</button>
  </nav>
</header>"""


@router.get("/store/{slug}", response_class=HTMLResponse)
def store_partner_page(slug: str, request: Request, background: BackgroundTasks):
    if not main._store_page_allowed(request):
        return store_page(request, background)                  # the locked screen (and the owner is told)
    p = main._partner_by_slug(slug)
    if not p or not p.get("password"):                          # the program's page is closed
        return HTMLResponse(main._pkg_error("This page isn't available.", 404), status_code=404, headers=_NO_INDEX)
    from routes.partners import _page_head
    return HTMLResponse(render_page("partner.html", **_page_head(p, _store_partner_header(p))), headers=_NO_INDEX)


@router.post("/api/store/partner/{slug}/send")
def store_partner_send(slug: str, request: Request, background: BackgroundTasks, body: dict = Body(...)):
    """The photos a family picked: a download link for them (QR code + email)."""
    screen = _sending_screen(request)
    if not screen:
        return JSONResponse(status_code=403, content={"error": "This screen isn't approved for store mode"})
    p = main._partner_by_slug(slug)
    if not p or not p.get("password"):
        return JSONResponse(status_code=404, content={"error": "This page isn't available"})
    first = " ".join(str(body.get("first_name") or "").split())[:40]
    last = " ".join(str(body.get("last_name") or "").split())[:40]
    email = str(body.get("email") or "").strip()[:120]
    if not first or not last:
        return JSONResponse(status_code=400, content={"error": "Please enter your first and last name"})
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Please enter a valid email"})
    wanted = body.get("paths") if isinstance(body.get("paths"), list) else []
    items = [i for i in (main._partner_photo(p, x) for x in dict.fromkeys(wanted) if isinstance(x, str)) if i]
    if not items:
        return JSONResponse(status_code=400, content={"error": "Pick some photos first"})
    if len(items) > main.STORE_PARTNER_MAX_PHOTOS:
        return JSONResponse(status_code=400, content={"error": f"Up to {main.STORE_PARTNER_MAX_PHOTOS} photos at a time"})
    if not main._store_partner_limiter.is_allowed(screen[0]):
        return JSONResponse(status_code=429, content={"error": "Too many in a short time — please wait a few minutes"})
    main._fill_taken_times(items)
    items.sort(key=main._taken_order)
    return main._store_partner_send(p, items, first, last, email, background)


@router.post("/api/store/request")
def store_request(request: Request, body: dict = Body(...)):
    """A screen that isn't approved asks for access: the owner is emailed a code for it."""
    if main._store_page_allowed(request):
        return {"approved": True}
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        return JSONResponse(status_code=400, content={"error": "Give this screen a name first"})
    if len(name) > main.STORE_NAME_MAX:
        return JSONResponse(status_code=400, content={"error": f"Keep the name to {main.STORE_NAME_MAX} letters or fewer"})
    info = main._request_info(request)
    if not main._store_request_limiter.is_allowed(info["ip"]):
        return JSONResponse(status_code=429, content={"error": "Too many requests — try again in an hour"})
    now = _time.time()
    for k in [k for k, v in main._store_challenges.items() if v["expires"] < now]:
        main._store_challenges.pop(k, None)
    challenge = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    main._store_challenges[challenge] = {"code": main._token_hash(code), "tries": 0, "name": name, "info": info,
                                         "expires": now + main.LOGIN_CODE_MINUTES * 60}
    main._send_store_screen_email(info, name, code)
    return {"challenge": challenge, "email": main._masked_email(main.OWNER_EMAIL),
            "minutes": main.LOGIN_CODE_MINUTES}


@router.post("/api/store/verify")
def store_verify(body: dict = Body(...)):
    """The emailed code, typed on the screen: approves it and remembers it."""
    challenge = str(body.get("challenge") or "")
    code = str(body.get("code") or "").strip()
    pending = main._store_challenges.get(challenge)
    if not pending or pending["expires"] < _time.time():
        main._store_challenges.pop(challenge, None)
        return JSONResponse(status_code=401, content={"error": "That code has expired — ask for a new one",
                                                      "restart": True})
    pending["tries"] += 1
    if not hmac.compare_digest(main._token_hash(code), pending["code"]):
        if pending["tries"] >= main.LOGIN_CODE_TRIES:
            main._store_challenges.pop(challenge, None)
            return JSONResponse(status_code=401, content={"error": "Too many wrong codes — ask for a new one",
                                                          "restart": True})
        return JSONResponse(status_code=401, content={"error": "Incorrect code"})
    main._store_challenges.pop(challenge, None)
    token = main._approve_store_screen(pending["name"], pending["info"])
    resp = JSONResponse({"ok": True, "name": pending["name"]})
    resp.set_cookie(main.STORE_SCREEN_COOKIE, token, httponly=True, samesite="lax",
                    max_age=main.STORE_SCREEN_DAYS * 86400)
    return resp


# ── Send to sale: the customer's favorites, picked up at the POS with a code ──

def _sending_screen(request: Request) -> tuple[str, str] | None:
    """(id, name) of the screen sending: an approved store screen, or the studio looking at it."""
    screen = main._store_screen(request)
    if screen:
        return screen[0][:16], screen[1].get("name", "")
    return ("studio", "Studio preview") if main._admin_authed(request) else None


@router.post("/api/store/send")
def store_send(request: Request, body: dict = Body(...)):
    """Send (or update) this screen's favorites for the POS. Returns the code to show."""
    screen = _sending_screen(request)
    if not screen:
        return JSONResponse(status_code=403, content={"error": "This screen isn't approved for store mode"})
    if not main._store_send_limiter.is_allowed(screen[0]):
        return JSONResponse(status_code=429, content={"error": "Too many sends — wait a few minutes"})
    cart = body.get("cart")
    if not isinstance(cart, dict):
        return JSONResponse(status_code=400, content={"error": "Nothing to send"})
    if len(json.dumps(cart)) > main.SAVED_CART_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "Too many favorites to send at once"})
    code = str(body.get("code") or "").strip().upper()
    if not code and not main._cart_summary(cart)["items"]:
        return JSONResponse(status_code=400, content={"error": "Heart some photos first"})
    rec = main._store_send(screen[0], screen[1], cart, code)
    return {"code": rec["code"], "status": rec["status"]}


@router.get("/api/store/send/{code}")
def store_send_status(code: str, request: Request):
    """The screen checks on its code: waiting, opened at the POS, sold (then it starts over)."""
    screen = _sending_screen(request)
    if not screen:
        return JSONResponse(status_code=403, content={"error": "This screen isn't approved for store mode"})
    rec = main._load_store_send(code)
    if not rec or rec.get("screen") != screen[0]:
        return JSONResponse(status_code=404, content={"status": "gone"})
    return {"code": rec["code"], "status": rec["status"]}


@router.get("/api/pos/store-code")
def pos_store_code(request: Request, code: str = ""):
    """POS: the favorites a store screen sent under this code, laid out like a saved cart."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    rec = main._load_store_send(code)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "No favorites under that code today"})
    if rec["status"] == "waiting":
        main._mark_store_send(rec["code"], "opened")
    from routes.saved_carts import _cart_detail
    detail = _cart_detail({"id": rec["code"], "email": "", "first_name": "", "cart": rec.get("cart") or {},
                           "summary": rec.get("summary") or {}, "created": rec.get("created", ""),
                           "updated": rec.get("updated", "")})
    return {**detail, "store_code": rec["code"], "screen_name": rec.get("screen_name", ""),
            "status": rec["status"]}


# ── Studio: Settings > Security > Store screens ─────────────────────────────

@router.get("/api/admin/store-screens")
def admin_store_screens(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    now = main._now_iso()
    screens = [{"id": k[:16], "name": v.get("name", ""), "describe": v.get("describe", ""),
                "approved": v.get("approved", ""), "last_used": v.get("last_used", ""), "ip": v.get("ip", "")}
               for k, v in main._load_admin_auth()["store_screens"].items() if v.get("expires", "") > now]
    return {"screens": sorted(screens, key=lambda x: x["approved"], reverse=True)}


def _screen_key(d: dict, screen_id: str) -> str | None:
    keys = [k for k in d["store_screens"] if k[:16] == screen_id]
    return keys[0] if len(keys) == 1 else None


@router.put("/api/admin/store-screens/{screen_id}")
def admin_rename_store_screen(screen_id: str, request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    name = " ".join(str(body.get("name") or "").split())
    if not name or len(name) > main.STORE_NAME_MAX:
        return JSONResponse(status_code=400, content={"error": f"Give it a name of up to {main.STORE_NAME_MAX} letters"})
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        key = _screen_key(d, screen_id)
        if not key:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        d["store_screens"][key]["name"] = name
        main._save_admin_auth(d)
    return {"ok": True}


@router.delete("/api/admin/store-screens/{screen_id}")
def admin_remove_store_screen(screen_id: str, request: Request):
    """Forget a store screen: it's locked out until it's approved with a new code."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        key = _screen_key(d, screen_id)
        if not key:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        d["store_screens"].pop(key)
        main._save_admin_auth(d)
    return {"ok": True}
