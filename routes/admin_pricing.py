"""Routes: admin_pricing. Moved from main.py unchanged; shared helpers still live in main.py."""
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.post("/api/admin/frames/prices")
async def update_frame_prices(request: Request):
    """Big Sky Rustic frame prices, one per size: {"sizes": [{"size_idx": 1, "price": "60"}, ...]}.
    Kept in Pricing (meta/frame_prices.json); the shop, checkout and POS use them right away."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    updates = body.get("sizes") if isinstance(body, dict) else None
    if not isinstance(updates, list) or not updates:
        return JSONResponse(status_code=400, content={"error": "No prices sent"})
    try:
        sizes = dict(main._frame_sizes() or {})
    except main.CheckoutError:
        sizes = {}
    for u in updates:
        try:
            idx = int(u.get("size_idx"))
            price = round(float(u.get("price")), 2)
        except (TypeError, ValueError, AttributeError):
            return JSONResponse(status_code=400, content={"error": "Enter a price above $0 for each size"})
        if not 0 <= idx < len(main._FRAME_SIZE_LABELS) or not 0 < price < 10000:
            return JSONResponse(status_code=400, content={"error": "Enter a price above $0 for each size"})
        sizes[str(idx)] = {"price": price}
    main._frame_prices_doc.save({"sizes": sizes, "updated": main._now_iso()})
    return {"ok": True, "sizes": sizes}


@router.post("/api/admin/pos-products")
async def api_save_pos_products(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_pos_products(body)
    return {"ok": True}


@router.post("/api/admin/print-pricing")
async def api_save_print_pricing(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_print_pricing(body)
    return {"status": "ok"}


@router.get("/api/admin/portrait-locations")
def api_get_portrait_locations(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    main._load_portrait_locations()
    return {"locations": sorted(main.PORTRAIT_LOCATIONS)}


@router.post("/api/admin/portrait-locations")
async def api_save_portrait_locations(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body  = await request.json()
    locs  = [l.lower().strip() for l in body.get("locations", []) if l.strip()]
    main._save_portrait_locations(locs)
    main._load_portrait_locations()
    return {"ok": True, "locations": sorted(main.PORTRAIT_LOCATIONS)}


@router.post("/api/admin/portrait-pricing")
async def api_save_portrait_pricing(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_portrait_pricing(body)
    return {"status": "ok"}


@router.post("/api/admin/seasons")
async def api_save_seasons(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    seasons = body.get("seasons", [])
    main._save_seasons(seasons)
    return {"ok": True, "seasons": seasons}


@router.get("/api/admin/activity-groups")
def api_activity_groups_get(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pricing = main._load_pricing()
    _derived = {
        cat["name"]: cat.get("acts", [])
        for cat in pricing.get("activity_categories", [])
        if cat.get("name")
    }
    merged = {**_derived, **pricing.get("activity_groups", {})}
    return {"activity_groups": merged}


@router.post("/api/admin/activity-groups")
async def api_activity_groups_save(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    groups = body.get("activity_groups", {})
    pricing = main._load_pricing()
    pricing["activity_groups"] = groups
    main._save_pricing(pricing)
    return {"status": "ok"}


@router.get("/api/admin/pricing")
def api_admin_pricing_get(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return main._load_pricing()


@router.post("/api/admin/pricing")
async def api_admin_pricing_save(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Invalid pricing data"})
    main._save_pricing(body)
    return {"status": "ok"}


@router.post("/api/admin/private-session-pricing")
async def api_save_private_session_pricing(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_private_session_pricing(body)
    return {"ok": True}


@router.post("/api/admin/items-bundles")
async def api_save_items_bundles(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    main._save_items_bundles(body)
    return {"ok": True}
