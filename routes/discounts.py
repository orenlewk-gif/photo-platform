"""Routes: discounts. Moved from main.py unchanged; shared helpers still live in main.py."""
import uuid
from datetime import datetime
from datetime import timezone
from fastapi import Query
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/validate-coupon")
def validate_coupon(code: str = Query(""), email: str = Query("")):
    """Validate a discount code against our own code store."""
    c, problem = main._check_coupon(code, email)
    if not c:
        return JSONResponse(status_code=404 if problem == "Invalid coupon code" else 400,
                            content={"valid": False, "error": problem})
    return {
        "valid":          True,
        "code":           c["code"].lower(),
        "discount_type":  c.get("type", "percent"),
        "amount":         float(c.get("amount", 0)),
        "description":    c.get("description", ""),
        "free_shipping":  c.get("free_shipping", False),
        "min_order":      float(c.get("min_order", 0)),
        "min_qty":        int(c.get("min_qty", 0)),
        "applies_to":     c.get("applies_to", "all"),
    }


@router.get("/api/admin/discount-codes")
def api_list_discount_codes(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = main._load_discount_codes()
    # Return codes without the full uses history (use /api/admin/discount-codes/{id} for that)
    summary = []
    for c in codes:
        s = {k: v for k, v in c.items() if k != "uses"}
        s["recent_uses"] = c.get("uses", [])[-5:]
        summary.append(s)
    return {"codes": summary}


@router.post("/api/admin/discount-codes")
async def api_create_discount_code(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()

    auto = body.get("auto_generate", False)
    prefix = body.get("prefix", "").strip().upper()
    if auto:
        code = main._generate_code(prefix=prefix)
    else:
        code = body.get("code", "").strip().upper()

    if not code:
        return JSONResponse(status_code=400, content={"error": "Code is required"})

    codes = main._load_discount_codes()
    if any(c.get("code","").upper() == code for c in codes):
        return JSONResponse(status_code=400, content={"error": f"Code '{code}' already exists"})

    ctype = body.get("type", "percent")
    if ctype not in ("percent", "fixed", "free_shipping"):
        return JSONResponse(status_code=400, content={"error": "type must be percent, fixed, or free_shipping"})

    new_code = {
        "id":                    str(uuid.uuid4())[:8],
        "code":                  code,
        "created":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "description":           body.get("description", "").strip(),
        "source":                body.get("source", "promo"),  # promo | receipt | review | referral
        "type":                  ctype,
        "amount":                float(body.get("amount", 0)),
        "free_shipping":         bool(body.get("free_shipping", False)),
        "applies_to":            body.get("applies_to", "all"),  # all | digital | prints | portraits | frames
        "min_order":             float(body.get("min_order", 0)),
        "min_qty":               int(body.get("min_qty", 0)),
        "usage_limit":           body.get("usage_limit") or None,
        "usage_limit_per_user":  body.get("usage_limit_per_user") or None,
        "expires":               body.get("expires") or None,
        "start_date":            body.get("start_date") or None,
        "active":                True,
        "usage_count":           0,
        "total_saved":           0.0,
        "uses":                  [],
    }

    # Sync to WooCommerce so native coupon_lines work in checkout
    wc_result = main._wc_sync_coupon(new_code)
    if "wc_id" in wc_result:
        new_code["wc_id"] = wc_result["wc_id"]
    else:
        new_code["wc_sync_error"] = wc_result.get("error", "")

    codes.append(new_code)
    main._save_discount_codes(codes)
    return {"code": {k: v for k, v in new_code.items() if k != "uses"}}


@router.get("/api/admin/discount-codes/{cid}")
def api_get_discount_code(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = main._load_discount_codes()
    c = next((x for x in codes if x.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return {"code": c}


@router.put("/api/admin/discount-codes/{cid}")
async def api_update_discount_code(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    codes = main._load_discount_codes()
    WC_SYNC_FIELDS = {"description","source","type","amount","free_shipping",
                      "applies_to","min_order","min_qty","usage_limit",
                      "usage_limit_per_user","expires","start_date","active"}
    for c in codes:
        if c.get("id") == cid:
            changed_wc = any(f in body for f in WC_SYNC_FIELDS)
            for field in WC_SYNC_FIELDS:
                if field in body:
                    c[field] = body[field]
            if changed_wc:
                wc_result = main._wc_sync_coupon(c)
                if "wc_id" in wc_result:
                    c["wc_id"] = wc_result["wc_id"]
                    c.pop("wc_sync_error", None)
                else:
                    c["wc_sync_error"] = wc_result.get("error", "")
            main._save_discount_codes(codes)
            return {"code": {k: v for k, v in c.items() if k != "uses"}}
    return JSONResponse(status_code=404, content={"error": "Not found"})


@router.delete("/api/admin/discount-codes/{cid}")
async def api_delete_discount_code(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    all_codes = main._load_discount_codes()
    to_delete = next((c for c in all_codes if c.get("id") == cid), None)
    if to_delete and to_delete.get("wc_id"):
        main._wc_delete_coupon(to_delete["wc_id"])
    codes = [c for c in all_codes if c.get("id") != cid]
    main._save_discount_codes(codes)
    return {"deleted": True}


@router.post("/api/admin/discount-codes/{cid}/wc-sync")
async def api_wc_sync_code(cid: str, request: Request):
    """Manually push a code to WooCommerce (or re-sync if it previously failed)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    codes = main._load_discount_codes()
    c = next((x for x in codes if x.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    result = main._wc_sync_coupon(c)
    if "wc_id" in result:
        c["wc_id"] = result["wc_id"]
        c.pop("wc_sync_error", None)
        main._save_discount_codes(codes)
        return {"wc_id": result["wc_id"]}
    c["wc_sync_error"] = result.get("error", "")
    main._save_discount_codes(codes)
    return JSONResponse(status_code=502, content={"error": result.get("error", "WC sync failed")})


@router.post("/api/admin/discount-codes/{cid}/generate")
async def api_generate_child_code(cid: str, request: Request):
    """Generate a new unique one-off code using an existing code as a template."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    codes = main._load_discount_codes()
    template = next((c for c in codes if c.get("id") == cid), None)
    if not template:
        return JSONResponse(status_code=404, content={"error": "Template code not found"})

    prefix = body.get("prefix", template.get("code", "")).rstrip("-") + "-"
    new_code_str = main._generate_code(prefix=prefix, length=6)
    while any(c.get("code","").upper() == new_code_str for c in codes):
        new_code_str = main._generate_code(prefix=prefix, length=6)

    new_code = {k: v for k, v in template.items() if k not in ("id","code","usage_count","total_saved","uses")}
    new_code.update({
        "id":            str(uuid.uuid4())[:8],
        "code":          new_code_str,
        "created":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "template_id":   cid,
        "usage_count":   0,
        "total_saved":   0.0,
        "uses":          [],
        "expires":       body.get("expires") or template.get("expires"),
        "usage_limit":   body.get("usage_limit") or template.get("usage_limit"),
        "description":   body.get("description") or template.get("description",""),
    })
    wc_result = main._wc_sync_coupon(new_code)
    if "wc_id" in wc_result:
        new_code["wc_id"] = wc_result["wc_id"]
    else:
        new_code["wc_sync_error"] = wc_result.get("error", "")

    codes.append(new_code)
    main._save_discount_codes(codes)
    return {"code": {k: v for k, v in new_code.items() if k != "uses"}}
