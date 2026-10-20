"""Routes: admin_pages. Moved from main.py unchanged; shared helpers still live in main.py."""
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from html import escape as _html_escape

from fastapi import APIRouter

import main
from core.pages import render_page

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_get(request: Request):
    if not main._admin_authed(request):
        next_url = main._safe_next(request.query_params.get("next"))
        return HTMLResponse(render_page("admin_login.html", error="", next=_html_escape(next_url)))
    return RedirectResponse("/admin/dashboard")


@router.get("/admin/discount-codes", response_class=HTMLResponse)
def admin_discount_codes_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/discount-codes")
    return HTMLResponse(open("templates/admin_discounts.html").read())


@router.get("/admin/trash", response_class=HTMLResponse)
def trash_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/trash")
    return HTMLResponse(open("templates/trash.html").read())


@router.get("/admin/zip-pricing")
def zip_pricing_page():
    return RedirectResponse("/admin/pricing#zip")


@router.get("/admin/zip-folders")
def zip_folders_page():
    return RedirectResponse("/admin/pricing#zip")


@router.get("/admin/zip-preview/{date}/{last_name}")
def zip_preview_page(date: str, last_name: str):
    return RedirectResponse("/admin/pricing#zip")


@router.get("/admin/zip-compare")
def zip_compare_page():
    return RedirectResponse("/admin/pricing#zip")


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/settings")
    return HTMLResponse(open("templates/admin_settings.html").read())


@router.get("/admin/indexing", response_class=HTMLResponse)
async def admin_indexing_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/indexing")
    return HTMLResponse(open("templates/admin_indexing.html").read())


@router.get("/admin/activities", response_class=HTMLResponse)
async def admin_activities_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/activities")
    return HTMLResponse(open("templates/admin_activities.html").read())


@router.get("/admin/customers", response_class=HTMLResponse)
async def admin_customers_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/customers")
    return HTMLResponse(open("templates/admin_customers.html").read())


@router.get("/admin/folders", response_class=HTMLResponse)
def admin_folders_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/folders")
    return HTMLResponse(open("templates/admin_folders.html").read())


@router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/dashboard")
    return HTMLResponse(open("templates/admin_studio.html").read())


@router.get("/admin/dashboard/{section}", response_class=HTMLResponse)
def admin_dashboard_section(request: Request, section: str):
    if not main._admin_authed(request):
        return RedirectResponse(f"/admin?next=/admin/dashboard/{section}")
    return HTMLResponse(open("templates/admin_studio.html").read())


@router.get("/admin/studio")
def admin_studio_redirect():
    return RedirectResponse("/admin/dashboard")


@router.get("/admin/pricing", response_class=HTMLResponse)
def admin_pricing_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/pricing")
    return HTMLResponse(open("templates/admin_pricing.html").read())
