"""Unlimited Action Photo Packages (POS only): pricing settings, the POS Packages list, and
adding photos to a package. The sale itself goes through the normal POS checkout."""
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Query, Request
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


def _staff(request: Request):
    return main._pos_pin_photographer(request)


@router.get("/api/pos/unlimited")
def pos_unlimited_list(request: Request, q: str = Query("")):
    """The POS Packages button: packages still taking photos first, then ones whose days are
    over but whose link still works. Search by name or phone."""
    if not _staff(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    main._close_due_unlimited()
    today = main._mountain_today()
    now_iso = datetime.now(timezone.utc).isoformat()
    q = q.strip().lower()
    digits = re.sub(r"\D", "", q)
    out = []
    for rec in main._unlimited_records().values():
        if (rec.get("expires") or "") < now_iso:
            continue
        if q and q not in (rec.get("name") or "").lower() and q not in (rec.get("email") or "").lower() \
                and not (len(digits) >= 3 and digits in re.sub(r"\D", "", rec.get("phone") or "")):
            continue
        out.append(main._unlimited_summary(rec, today))
    out.sort(key=lambda r: (not r["open"], r["last_day"] if r["open"] else "", (r["name"] or "").lower()))
    return {"packages": out, "today": today}


@router.get("/api/pos/unlimited/{token}")
def pos_unlimited_detail(token: str, request: Request):
    if not _staff(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    rec = main._load_unlimited_sale(token)
    pkg = main._load_pkg(token) if rec else None
    if not rec or not pkg:
        return JSONResponse(status_code=404, content={"error": "Package not found"})
    live = main._folder_index()["all_by_path"]
    photos = [{"path": p, "filename": os.path.basename(p), "date": (live.get(p) or {}).get("date", ""),
               "location": (live.get(p) or {}).get("location", "")} for p in pkg.get("paths") or []]
    return {**main._unlimited_summary({**rec, "photo_count": len(photos)}), "photos": photos,
            "download_url": f"{main.SITE_URL}/package/{token}"}


@router.post("/api/pos/unlimited/{token}/photos")
def pos_unlimited_photos(token: str, request: Request, body: dict = Body(...)):
    """Add (and, for mistakes, remove) photos in a package. The customer's page shows them right away."""
    if not _staff(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    try:
        r = main._unlimited_change_photos(token, body.get("add") or [], body.get("remove") or [])
    except LookupError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"added": r["added"], "removed": r["removed"], "photo_count": len(r["paths"]), "paths": r["paths"]}
