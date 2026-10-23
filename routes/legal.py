"""Privacy policy, terms of sale / photo license, refund policy, and the business details the
site footer shows (from Settings > Business details)."""
import re

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from html import escape as _html_escape

import main
from core.pages import render_page, site_header

router = APIRouter()
POLICIES_UPDATED = "October 19, 2026"


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
