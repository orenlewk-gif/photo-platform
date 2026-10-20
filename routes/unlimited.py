"""Unlimited Action Photo Packages (POS only): pricing settings now; the package sale, the POS
package button, adding photos and the commission split at close come next."""
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import main

router = APIRouter()


@router.get("/api/unlimited-packages")
def unlimited_packages():
    """Package names, days and prices (the POS and the Pricing page read this)."""
    return main._load_unlimited_packages()


@router.post("/api/admin/unlimited-packages")
def save_unlimited_packages(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    try:
        cleaned = main._clean_unlimited_packages(body)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    main._save_unlimited_packages(cleaned)
    return cleaned
