"""Saved carts: customers save their gallery selection with a first name and email and load it
again on any device; the studio sees them under Customers > Saved Carts."""
import json

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import main

router = APIRouter()


def _visitor(request: Request) -> str:
    return main._client_ip(request)


@router.post("/api/saved-cart")
def save_cart(request: Request, body: dict = Body(...)):
    """Save this visitor's cart under their email (an empty cart removes it)."""
    if not main._saved_cart_save_limiter.is_allowed(_visitor(request)):
        return JSONResponse(status_code=429, content={"error": "Too many saves — please try again in a few minutes"})
    email = str(body.get("email") or "").strip()
    first = str(body.get("first_name") or "").strip()
    cart = body.get("cart")
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Please enter a valid email"})
    if not first:
        return JSONResponse(status_code=400, content={"error": "Please enter your first name"})
    if not isinstance(cart, dict):
        return JSONResponse(status_code=400, content={"error": "Nothing to save"})
    if len(json.dumps(cart)) > main.SAVED_CART_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "Your selection is too large to save"})
    summary = main._save_cart_record(email, first, cart)
    return {"ok": True, "items": summary["items"]}


@router.post("/api/saved-cart/load")
def load_cart(request: Request, body: dict = Body(...)):
    """Load the cart saved under an email (limited per visitor, so emails can't be guessed)."""
    if not main._saved_cart_load_limiter.is_allowed(_visitor(request)):
        return JSONResponse(status_code=429, content={"error": "Too many tries — please wait a few minutes"})
    email = str(body.get("email") or "").strip()
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={"error": "Please enter a valid email"})
    rec = main._load_saved_cart(main._saved_cart_id(email))
    if not rec or not (rec.get("summary") or {}).get("items"):
        return JSONResponse(status_code=404, content={"error": "No saved photos for that email"})
    return {"first_name": rec.get("first_name", ""), "email": rec["email"], "cart": rec["cart"]}


# ── Studio: Customers > Saved Carts ───────────────────────────────────────────

@router.get("/api/admin/saved-carts")
def admin_saved_carts(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rows = []
    for r in main._saved_cart_list():
        s = r.get("summary") or {}
        rows.append({"id": r["id"], "email": r.get("email", ""), "first_name": r.get("first_name", ""),
                     "updated": r.get("updated", ""), "created": r.get("created", ""),
                     "photos": s.get("photos", 0), "poses": s.get("poses", 0), "prints": s.get("prints", 0),
                     "frames": s.get("frames", 0), "items": s.get("items", 0), "folders": s.get("folders", [])})
    return {"carts": rows}


def _cart_detail(rec: dict) -> dict:
    """A saved cart laid out for the studio and the POS: per folder its date, location and
    family (from the gallery's folder key), selected photos, poses and prints."""
    folders = []
    for key, fc in (rec.get("cart", {}).get("folderCarts") or {}).items():
        if not isinstance(fc, dict):
            continue
        parts = str(key).split("||")
        poses = []
        pose_list = fc.get("poses") if isinstance(fc.get("poses"), list) else []
        for color, state in (fc.get("poseState") or {}).items():
            pose = next((p for p in pose_list if isinstance(p, dict) and p.get("color") == color), {})
            pp = [p for p in (pose.get("paths") or []) if isinstance(p, str)]
            if state == "pose":
                poses.append({"label": pose.get("label", "Pose"), "whole": True, "paths": pp})
            elif isinstance(state, list):
                poses.append({"label": pose.get("label", "Pose"), "whole": False,
                              "paths": [pp[i] for i in state if isinstance(i, int) and 0 <= i < len(pp)]})
        folders.append({"label": fc.get("label") or key, "location": fc.get("location", "") or (parts[1] if len(parts) > 1 else ""),
                        "date": parts[0] if parts else "", "family": parts[2] if len(parts) > 2 else "",
                        "photos": [p for p in (fc.get("selectedPaths") or []) if isinstance(p, str)],
                        "poses": poses,
                        "prints": [{"path": p.get("photoPath", ""), "size": p.get("size", ""), "size_idx": p.get("sizeIdx", 0),
                                    "frame": p.get("frame", ""), "logo": bool(p.get("_logo"))}
                                   for p in (fc.get("printCart") or []) if isinstance(p, dict)]})
    return {"id": rec["id"], "email": rec["email"], "first_name": rec.get("first_name", ""),
            "created": rec.get("created", ""), "updated": rec.get("updated", ""),
            "summary": rec.get("summary", {}), "folders": folders,
            "frames": [f for f in (rec.get("cart", {}).get("frameCart") or []) if isinstance(f, dict)]}


@router.get("/api/admin/saved-carts/{cart_id}")
def admin_saved_cart(cart_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rec = main._load_saved_cart(cart_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return _cart_detail(rec)


@router.get("/api/pos/saved-cart")
def pos_saved_cart(request: Request, email: str = ""):
    """POS: the online saved cart of the customer staff just picked (by their email)."""
    if not main._pos_pin_photographer(request):
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    email = (email or "").strip()
    if not main._EMAIL_RE.match(email):
        return JSONResponse(status_code=404, content={"error": "No saved cart"})
    rec = main._load_saved_cart(main._saved_cart_id(email))
    if not rec or not (rec.get("summary") or {}).get("items"):
        return JSONResponse(status_code=404, content={"error": "No saved cart"})
    return _cart_detail(rec)


@router.delete("/api/admin/saved-carts/{cart_id}")
def admin_delete_saved_cart(cart_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._load_saved_cart(cart_id):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    main._delete_saved_cart(cart_id)
    return {"deleted": True}


@router.post("/api/admin/saved-carts/clear-all")
def admin_clear_saved_carts(request: Request, body: dict = Body(...)):
    """End of season: remove every saved cart. Needs the word CLEAR typed to confirm."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if body.get("confirm") != "CLEAR":
        return JSONResponse(status_code=400, content={"error": "Type CLEAR to confirm"})
    ids = [r["id"] for r in main._saved_cart_list()]
    for cid in ids:
        main._delete_saved_cart(cid)
    return {"cleared": len(ids)}
