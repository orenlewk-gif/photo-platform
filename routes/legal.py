"""Privacy policy, terms of sale / photo license, refund policy, and the business details the
site footer shows (from Settings > Business details)."""
import re

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse
from html import escape as _html_escape

import main
from core.pages import render_page, site_header

router = APIRouter()
POLICIES_UPDATED = "October 24, 2026"


def _business() -> dict:
    b = main._business_details()
    where = b.get("address") or "Big Sky, Montana"
    return {k: _html_escape(str(v)) for k, v in {
        "biz_name": b.get("name", ""), "biz_where": where, "biz_phone": b.get("phone", ""),
        "biz_phone_link": re.sub(r"[^\d+]", "", b.get("phone", "")), "biz_email": b.get("email", "")}.items()}


def _policy(name: str, title: str) -> HTMLResponse:
    body = render_page(name, updated=POLICIES_UPDATED, **_business())
    if "[[CONFIRM" in body:          # never show a policy with an unanswered question
        return HTMLResponse("<h1>Coming soon</h1>", status_code=404)
    head = render_page("_legal_head.html", title=title, site_header=site_header())
    foot = render_page("_legal_foot.html", **_business())
    return HTMLResponse(head + body + foot)


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return _policy("privacy.html", "Privacy Policy")


@router.get("/terms", response_class=HTMLResponse)
def terms_page():
    return _policy("terms.html", "Terms of Sale & Photo License")


@router.get("/refunds", response_class=HTMLResponse)
def refunds_page():
    return _policy("refunds.html", "Refund Policy")


@router.get("/api/site-info")
def site_info():
    """Business details for the site footer (public)."""
    b = main._business_details()
    return {"name": b.get("name", ""), "address": b.get("address", ""), "phone": b.get("phone", ""),
            "email": b.get("email", "")}


@router.get("/photo-removal", response_class=HTMLResponse)
def photo_removal_page():
    return _policy("photo_removal.html", "Request a Photo Removal")


_removal_limiter = main._RateLimiter(max_calls=5, window=3600)     # per visitor


@router.post("/api/photo-removal")
def photo_removal_request(request: Request, body: dict = Body(...)):
    """The photo removal form: emails the request to the business address, with the visitor's
    email as the reply-to address so answering it reaches them."""
    if str(body.get("website") or "").strip():           # the hidden field only robots fill in
        return {"sent": True}
    if not _removal_limiter.is_allowed(main._client_ip(request)):
        return JSONResponse(status_code=429, content={"error": "Too many requests. Please email us instead."})
    f = {k: str(body.get(k) or "").strip()[:n] for k, n in
         (("name", 80), ("email", 120), ("date", 40), ("location", 80), ("photos", 300), ("message", 2000))}
    if not main._EMAIL_RE.match(f["email"]):
        return JSONResponse(status_code=400, content={"error": "Please enter your email."})
    if not f["photos"] and not f["message"]:
        return JSONResponse(status_code=400, content={"error": "Please tell us which photo, or describe it."})
    to = main._business_details().get("email") or main.OWNER_EMAIL
    rows = "".join(f'<tr><td style="padding:3px 10px 3px 0;color:#666">{label}</td><td>{_html_escape(f[k]) or "—"}</td></tr>'
                   for label, k in (("Name", "name"), ("Email", "email"), ("Photo date", "date"),
                                    ("Location", "location"), ("Photo number(s)", "photos")))
    html = main._wrap_email_html(
        '<p style="margin:0 0 .8rem"><strong>Photo removal request</strong> from the website.</p>'
        f'<table style="font-size:.9rem;border-collapse:collapse">{rows}</table>'
        + (f'<p style="margin:.8rem 0 0;white-space:pre-wrap">{_html_escape(f["message"])}</p>' if f["message"] else "")
        + '<p style="margin:.8rem 0 0;font-size:.8rem;color:#666">Reply to this email to answer them.</p>')
    if not main.RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "The form isn't working right now. Please email us instead."})
    try:
        r = main.http_requests.post("https://api.resend.com/emails",
                                    headers={"Authorization": f"Bearer {main.RESEND_API_KEY}", "Content-Type": "application/json"},
                                    json={"from": main.RESEND_FROM, "to": [to], "reply_to": f["email"],
                                          "subject": f"Photo removal request — {f['photos'] or f['name'] or f['email']}"[:150],
                                          "html": html}, timeout=10)
        if not r.ok:
            raise RuntimeError(r.text[:200])
    except Exception as e:
        print(f"Photo removal request not sent ({f['email']}): {e}")
        return JSONResponse(status_code=502, content={"error": "Could not send. Please email us instead."})
    return {"sent": True}
