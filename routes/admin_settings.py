"""Routes: admin_settings. Moved from main.py unchanged; shared helpers still live in main.py."""
import requests as http_requests
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/admin/square/test")
def admin_square_test(request: Request):
    """Studio check: does the Square access token work? Never returns the token."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    result = {"app_id_set": bool(main.SQUARE_APP_ID), "token_set": bool(main.SQUARE_ACCESS_TOKEN),
              "location_id_set": bool(main.SQUARE_LOCATION_ID)}
    if not main.SQUARE_ACCESS_TOKEN:
        return {**result, "ok": False, "error": "SQUARE_ACCESS_TOKEN is missing"}
    try:
        r = http_requests.get(f"{main.SQUARE_API_BASE}/locations", headers=main._square_headers(), timeout=8)
    except Exception as e:
        return {**result, "ok": False, "error": f"Could not reach Square: {e}"}
    if not r.ok:
        reason = "token rejected — check it's the Production access token" if r.status_code == 401 else r.text[:200]
        return {**result, "ok": False, "error": f"Square answered {r.status_code}: {reason}"}
    locations = r.json().get("locations") or []
    return {**result, "ok": True,
            "locations": [l.get("name", "") for l in locations],
            "location_id_matches": any(l.get("id") == main.SQUARE_LOCATION_ID for l in locations) if main.SQUARE_LOCATION_ID else None}


@router.get("/api/admin/settings")
async def get_admin_settings(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return main._load_settings()


@router.post("/api/admin/settings")
async def save_admin_settings(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    main._save_settings(body)
    return {"ok": True}
