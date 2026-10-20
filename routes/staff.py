"""Routes: staff. Moved from main.py unchanged; shared helpers still live in main.py."""
import hmac
import time as _time
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from fastapi import Body
from fastapi import Query
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/admin/owner-status")
def owner_status(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"unlocked": main._owner_unlocked(request)}


@router.post("/api/admin/owner-unlock")
def owner_unlock(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if main._pin_locked_out(request):
        return JSONResponse(status_code=429, content={"error": "Too many wrong PINs — wait 10 minutes"})
    owner_pin = main._owner_pin()
    if not owner_pin:
        return JSONResponse(status_code=503, content={
            "error": f"No photographer named {main.OWNER_NAME} — add one with your PIN in Photographers first"})
    if not hmac.compare_digest(str(body.get("pin") or "").strip(), owner_pin):
        main._record_pin_failure(request)
        return JSONResponse(status_code=401, content={"error": "Incorrect PIN"})
    expires = int(_time.time()) + main.OWNER_UNLOCK_SECONDS
    resp = JSONResponse({"ok": True})
    resp.set_cookie(main.OWNER_COOKIE, main._owner_token(expires), httponly=True, samesite="strict",
                    max_age=main.OWNER_UNLOCK_SECONDS)
    return resp


@router.get("/api/clock/status")
def clock_status_ep(pin: str = Query(...)):
    p = main._auth_photographer(pin)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Invalid PIN"})
    records = main._load_clock_records()
    active = next((r for r in reversed(records)
                   if r["photographer_id"] == p["id"] and not r.get("clock_out")), None)
    return {"clocked_in": active is not None, "record": active,
            "photographer": {k: v for k, v in p.items() if k != "pin"}}


@router.put("/api/clock/record/{record_id}")
async def edit_clock_record(record_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    records = main._load_clock_records()
    for r in records:
        if r["id"] == record_id:
            if "clock_in" in body and body["clock_in"]:
                r["clock_in"] = body["clock_in"]
            if "clock_out" in body:
                r["clock_out"] = body["clock_out"] or None
            if "location" in body:
                r["location"] = body["location"]
            main._save_clock_records(records)
            return {"record": r}
    return JSONResponse(status_code=404, content={"error": "Record not found"})


@router.delete("/api/clock/record/{record_id}")
async def delete_clock_record(record_id: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    records = main._load_clock_records()
    new_records = [r for r in records if r["id"] != record_id]
    if len(new_records) == len(records):
        return JSONResponse(status_code=404, content={"error": "Record not found"})
    main._save_clock_records(new_records)
    return {"status": "deleted"}


@router.post("/api/clock/record")
async def create_clock_record(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    photographer_id = body.get("photographer_id", "")
    photographers = main._load_photographers()
    p = next((x for x in photographers if x["id"] == photographer_id), None)
    if not p:
        return JSONResponse(status_code=404, content={"error": "Photographer not found"})
    record = {
        "id": str(uuid.uuid4()),
        "photographer_id": photographer_id,
        "photographer_name": p["name"],
        "location": body.get("location", ""),
        "clock_in": body.get("clock_in", ""),
        "clock_out": body.get("clock_out") or None,
        "date": body.get("clock_in", "")[:10] if body.get("clock_in") else "",
    }
    records = main._load_clock_records()
    records.append(record)
    main._save_clock_records(records)
    return {"record": record}


@router.get("/api/clock/records")
def get_all_clock_records(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    records = main._load_clock_records()
    photographers = main._load_photographers()
    return {"records": records, "photographers": photographers}


@router.get("/api/photographers")
def list_photographers(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    return {"photographers": main._load_photographers()}


@router.post("/api/photographers")
async def add_photographer(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    name = body.get("name", "").strip()
    pin  = body.get("pin", "").strip()
    if not name or not pin:
        return JSONResponse(status_code=400, content={"error": "Name and PIN required"})
    photographers = main._load_photographers()
    if any(p["pin"] == pin for p in photographers):
        return JSONResponse(status_code=400, content={"error": "PIN already in use"})
    p = {"id": str(uuid.uuid4())[:8], "name": name, "pin": pin,
         "default_location": body.get("default_location", ""),
         "file_prefix": body.get("file_prefix", "").strip().lower(),
         "commission_type": body.get("commission_type", "hourly"),
         "commission_rate": float(body.get("commission_rate", 0) or 0),
         "created_at": datetime.now(timezone.utc).isoformat()}
    photographers.append(p)
    main._save_photographers(photographers)
    return {"photographer": p}


@router.put("/api/photographers/{pid}")
async def update_photographer(pid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    body = await request.json()
    photographers = main._load_photographers()
    for p in photographers:
        if p["id"] == pid:
            for field in ("name", "pin", "default_location", "file_prefix", "commission_type"):
                if field in body:
                    p[field] = body[field]
            if "commission_rate" in body:
                p["commission_rate"] = float(body["commission_rate"] or 0)
            main._save_photographers(photographers)
            return {"photographer": p}
    return JSONResponse(status_code=404, content={"error": "Not found"})


@router.delete("/api/photographers/{pid}")
async def delete_photographer(pid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    photographers = [p for p in main._load_photographers() if p["id"] != pid]
    main._save_photographers(photographers)
    return {"deleted": True}


@router.get("/api/photographers/commission")
def photographer_commission(request: Request,
                             date_from: str = Query(""),
                             date_to:   str = Query("")):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)
    if denied:
        return denied
    if not date_from:
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")
    photographers = main._load_photographers()
    records       = main._load_clock_records()
    pending_meta  = main._load_pending_meta()
    # Revenue by photographer prefix
    prefix_map = {}  # prefix (3-char lower) -> photographer id
    for p in photographers:
        pfx = p.get("file_prefix", "").strip().lower()
        if pfx:
            prefix_map[pfx] = p["id"]
    rev_by_pid: dict[str, float] = {p["id"]: 0.0 for p in photographers}
    refunds = main._load_order_refunds()
    try:
        wc_orders = main._fetch_wc_orders_all(after=date_from, before=date_to)
        pos_orders = main._fetch_pos_orders(after=date_from, before=date_to)
        for o in wc_orders:
            row = main._order_row(o)
            fnames = (row.get("filenames") or "").split(",")
            for f in fnames:
                f = f.strip()
                if len(f) >= 3:
                    pid_match = prefix_map.get(f[:3].lower())
                    if pid_match and pid_match in rev_by_pid:
                        rev_by_pid[pid_match] += main._commission_amount(row, refunds)
                        break
        for o in pos_orders:
            row = main._pos_order_row(o)
            fnames = (row.get("filenames") or "").split(",")
            for f in fnames:
                f = f.strip()
                if len(f) >= 3:
                    pid_match = prefix_map.get(f[:3].lower())
                    if pid_match and pid_match in rev_by_pid:
                        rev_by_pid[pid_match] += main._commission_amount(row, refunds)
                        break
    except Exception:
        pass
    result = []
    for p in photographers:
        pid    = p["id"]
        shifts = [r for r in records
                  if r["photographer_id"] == pid
                  and date_from <= r.get("date", "") <= date_to]
        hours  = 0.0
        for s in shifts:
            if s.get("clock_in") and s.get("clock_out"):
                ci = datetime.fromisoformat(s["clock_in"])
                co = datetime.fromisoformat(s["clock_out"])
                hours += (co - ci).total_seconds() / 3600
        uploads = [m for m in pending_meta
                   if m.get("photographer_id") == pid
                   and date_from <= m.get("date", "") <= date_to]
        result.append({
            "id": pid, "name": p["name"],
            "shifts": len(shifts), "hours": round(hours, 2),
            "photos_uploaded": len(uploads),
            "revenue": round(rev_by_pid.get(pid, 0.0), 2),
            "clock_records": shifts,
        })
    return {"commission": result, "date_from": date_from, "date_to": date_to}


@router.get("/admin/photographers", response_class=HTMLResponse)
def photographers_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/photographers")
    return HTMLResponse(open("templates/photographers.html").read())
