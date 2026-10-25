"""Routes: auth. Moved from main.py unchanged; shared helpers still live in main.py."""
import hmac
import secrets
import time as _time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from html import escape as _html_escape

from fastapi import APIRouter

import main
from core.pages import render_page

router = APIRouter()


@router.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    pw   = form.get("password", "")
    next_url = main._safe_next(form.get("next"))
    def again(msg, status=401):
        return HTMLResponse(render_page("admin_login.html", error=f'<p class="err">{msg}</p>',
                                                    next=_html_escape(next_url)), status_code=status)
    if main._admin_pw_fail_limiter.count(main._client_ip(request)) >= main._admin_pw_fail_limiter.max_calls:
        return again("Too many wrong passwords — wait 10 minutes.", 429)
    if not main.ADMIN_PASSWORD or not hmac.compare_digest(pw, main.ADMIN_PASSWORD):
        main._admin_pw_fail_limiter.is_allowed(main._client_ip(request))
        return again("Incorrect password.")
    info = main._request_info(request)
    device_hash = main._device_approved(request)
    if device_hash or not main.ADMIN_LOGIN_CODES_ON:
        return main._signed_in_response(next_url, device_hash=device_hash or "", info=info)
    # A new device: email a code before signing in
    challenge = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    now = _time.time()
    for k in [k for k, v in main._login_challenges.items() if v["expires"] < now]:
        main._login_challenges.pop(k, None)
    main._login_challenges[challenge] = {"code": main._token_hash(code), "expires": now + main.LOGIN_CODE_MINUTES * 60,
                                    "tries": 0, "next": next_url, "info": info}
    if not main._send_login_code_email(code, info):
        main._login_challenges.pop(challenge, None)
        return again("Couldn't email the sign-in code — try again in a minute.", 503)
    return HTMLResponse(render_page("admin_code.html", email=_html_escape(main._masked_email(main.OWNER_EMAIL)),
                                               challenge=challenge, error=""))


@router.post("/admin/verify")
async def admin_verify(request: Request):
    form = await request.form()
    challenge = str(form.get("challenge") or "")
    code = str(form.get("code") or "").strip()
    pending = main._login_challenges.get(challenge)
    if not pending or pending["expires"] < _time.time():
        main._login_challenges.pop(challenge, None)
        return HTMLResponse(render_page("admin_login.html", 
            error='<p class="err">That code has expired — sign in again.</p>', next="/admin/dashboard"), status_code=401)
    pending["tries"] += 1
    if not hmac.compare_digest(main._token_hash(code), pending["code"]):
        if pending["tries"] >= main.LOGIN_CODE_TRIES:
            main._login_challenges.pop(challenge, None)
            return HTMLResponse(render_page("admin_login.html", 
                error='<p class="err">Too many wrong codes — sign in again.</p>', next="/admin/dashboard"), status_code=401)
        return HTMLResponse(render_page("admin_code.html", email=_html_escape(main._masked_email(main.OWNER_EMAIL)),
                                                   challenge=challenge, error='<p class="err">Incorrect code.</p>'),
                            status_code=401)
    main._login_challenges.pop(challenge, None)
    # Approve this device for 90 days, then sign in
    device_token = secrets.token_urlsafe(32)
    device_hash = main._token_hash(device_token)
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        d["devices"][device_hash] = {
            "approved": main._now_iso(), "describe": pending["info"].get("device", ""), "ip": pending["info"].get("ip", ""),
            "expires": (datetime.now(timezone.utc) + timedelta(days=main.ADMIN_DEVICE_DAYS)).isoformat()}
        main._save_admin_auth(d)
    return main._signed_in_response(pending["next"], device_token, device_hash, pending["info"])


@router.get("/admin/logout")
def admin_logout(request: Request):
    token = request.cookies.get(main.ADMIN_COOKIE, "")
    if token:
        d = main._load_admin_auth()
        with main._admin_auth_lock:
            if d["sessions"].pop(main._token_hash(token), None) is not None:
                main._save_admin_auth(d)
    resp = RedirectResponse("/admin")
    resp.delete_cookie(main.ADMIN_COOKIE)        # this device stays approved: next time, just the password
    resp.delete_cookie(main.OWNER_COOKIE)     # signing out also locks the PIN-protected pages
    return resp


@router.get("/api/admin/devices")
def admin_devices(request: Request):
    """Approved devices, for Settings > Security."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    mine = main._device_approved(request)
    devices = [{"describe": v.get("describe", ""), "approved": v.get("approved", ""),
                "last_signin": v.get("last_signin", v.get("approved", "")), "ip": v.get("ip", ""),
                "this_device": k == mine}
               for k, v in main._load_admin_auth()["devices"].items() if v.get("expires", "") > main._now_iso()]
    return {"devices": sorted(devices, key=lambda x: x["last_signin"], reverse=True),
            "codes_on": main.ADMIN_LOGIN_CODES_ON, "email": main._masked_email(main.OWNER_EMAIL)}


@router.get("/api/admin/pos-devices")
def admin_pos_devices(request: Request):
    """Devices approved for the POS, for Settings > Security."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    now = main._now_iso()
    devices = [{"id": k[:16], "describe": v.get("describe", ""), "approved": v.get("approved", ""),
                "by": v.get("by", ""), "ip": v.get("ip", "")}
               for k, v in main._load_admin_auth()["pos_devices"].items() if v.get("expires", "") > now]
    return {"devices": sorted(devices, key=lambda x: x["approved"], reverse=True),
            "codes_on": main.POS_DEVICE_CODES_ON}


@router.delete("/api/admin/pos-devices/{device_id}")
def admin_remove_pos_device(device_id: str, request: Request):
    """Forget a POS device: it needs a new emailed code before it can use the POS again."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        keys = [k for k in d["pos_devices"] if k[:16] == device_id]
        if len(keys) != 1:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        d["pos_devices"].pop(keys[0])
        main._save_admin_auth(d)
    return {"ok": True}


@router.post("/api/admin/sign-out-everywhere")
def admin_sign_out_everywhere(request: Request):
    """End every studio session and forget every approved device (each needs a code again)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        d["sessions"], d["devices"] = {}, {}
        main._save_admin_auth(d)
    resp = JSONResponse({"ok": True})
    for c in (main.ADMIN_COOKIE, main.ADMIN_DEVICE_COOKIE, main.OWNER_COOKIE):
        resp.delete_cookie(c)
    return resp
