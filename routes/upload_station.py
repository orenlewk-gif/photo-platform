"""Routes: the Upload Station (/u/<random>), the store desktops' page for uploading photos.

The studio's upload form (static/upload.js) and nothing else of the studio, plus "Today's uploads":
everything uploaded today (from any station or the studio), where it can be moved or put in the
trash, so a batch sent to the wrong place never has to be uploaded again.

Only approved computers get in (see "Upload stations" in main.py). The page's address is random
and any other /u/ address is a plain "not found", so it can't be stumbled on. Stations may move,
trash and restore only photos in today's upload log."""
import hmac
import os
import secrets
import time as _time
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Body, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import main
from core.pages import render_page, with_upload_form
from core.text import natural_sort_key

router = APIRouter()

_NO_INDEX = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


def _not_found():
    # The same answer as any address that doesn't exist
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _station_page(path: str, name: str) -> str:
    return with_upload_form(render_page("upload_station.html", path=quote(path, safe=""),
                                        station_name=main._html_escape(name)))


@router.get("/u/{path}", response_class=HTMLResponse)
def station_page(path: str, request: Request, background: BackgroundTasks):
    if not main._is_device_path("station", path):
        return _not_found()
    station = main._upload_station(request)
    if station:
        background.add_task(main._touch_upload_station, station[0])
        return HTMLResponse(_station_page(path, station[1].get("name", "")), headers=_NO_INDEX)
    if main._admin_authed(request):                              # the owner looking at it
        return HTMLResponse(_station_page(path, "Studio"), headers=_NO_INDEX)
    return HTMLResponse(open("templates/upload_station_locked.html").read(), status_code=403, headers=_NO_INDEX)


@router.get("/u/{path}/manifest.json")
def station_manifest(path: str):
    """Lets Chrome install the station as an app (its own window and Dock icon), like the POS."""
    if not main._is_device_path("station", path):
        return _not_found()
    return JSONResponse({
        "name": "Crystal Images Upload Station",
        "short_name": "CI Upload",
        "start_url": f"/u/{path}",
        "scope": f"/u/{path}",
        "display": "standalone",
        "background_color": "#0c2336",
        "theme_color": "#0c2336",
        "icons": [
            {"src": "/static/upload-icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/upload-icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    }, headers=_NO_INDEX)


# ── Approving a computer: a code emailed to the owner, typed on the computer once ──────────────

@router.post("/api/station/request")
def station_request(request: Request, body: dict = Body(...)):
    """A computer that isn't approved asks for access: the owner is emailed a code for it.
    Only from the station's own address (anyone else can't even ask)."""
    if not main._is_device_path("station", str(body.get("path") or "")):
        return _not_found()
    if main._upload_station(request):
        return {"approved": True}
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        return JSONResponse(status_code=400, content={"error": "Give this computer a name first"})
    if len(name) > main.STORE_NAME_MAX:
        return JSONResponse(status_code=400, content={"error": f"Keep the name to {main.STORE_NAME_MAX} letters or fewer"})
    info = main._request_info(request)
    if not main._station_request_limiter.is_allowed(info["ip"]):
        return JSONResponse(status_code=429, content={"error": "Too many requests — try again in an hour"})
    now = _time.time()
    for k in [k for k, v in main._station_challenges.items() if v["expires"] < now]:
        main._station_challenges.pop(k, None)
    challenge = secrets.token_urlsafe(16)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    main._station_challenges[challenge] = {"code": main._token_hash(code), "tries": 0, "name": name, "info": info,
                                           "expires": now + main.LOGIN_CODE_MINUTES * 60}
    main._send_upload_station_email(info, name, code)
    return {"challenge": challenge, "email": main._masked_email(main.OWNER_EMAIL),
            "minutes": main.LOGIN_CODE_MINUTES}


@router.post("/api/station/resend")
def station_resend(body: dict = Body(...)):
    """The code screen's "Send a new code" (same request, a fresh code)."""
    challenge = str(body.get("challenge") or "")
    code, why = main._renew_code(main._station_challenges, challenge)
    if code is None:
        return JSONResponse(status_code=429 if challenge in main._station_challenges else 401,
                            content={"error": why, "restart": challenge not in main._station_challenges})
    pending = main._station_challenges[challenge]
    main._send_upload_station_email(pending["info"], pending["name"], code)
    return {"ok": True, "email": main._masked_email(main.OWNER_EMAIL), "minutes": main.LOGIN_CODE_MINUTES}


@router.post("/api/station/verify")
def station_verify(body: dict = Body(...)):
    """The emailed code, typed on the computer: approves it and remembers it."""
    challenge = str(body.get("challenge") or "")
    code = str(body.get("code") or "").strip()
    pending = main._station_challenges.get(challenge)
    if not pending or pending["expires"] < _time.time():
        main._station_challenges.pop(challenge, None)
        return JSONResponse(status_code=401, content={"error": "That code has expired — ask for a new one",
                                                      "restart": True})
    pending["tries"] += 1
    if not hmac.compare_digest(main._token_hash(code), pending["code"]):
        if pending["tries"] >= main.LOGIN_CODE_TRIES:
            main._station_challenges.pop(challenge, None)
            return JSONResponse(status_code=401, content={"error": "Too many wrong codes — ask for a new one",
                                                          "restart": True})
        return JSONResponse(status_code=401, content={"error": "Incorrect code"})
    main._station_challenges.pop(challenge, None)
    token = main._approve_upload_station(pending["name"], pending["info"])
    resp = JSONResponse({"ok": True, "name": pending["name"]})
    resp.set_cookie(main.UPLOAD_STATION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=main.UPLOAD_STATION_DAYS * 86400)
    return resp


# ── Today's uploads: see them, move them, put them in the trash, bring them back ────────────────

def _forbidden():
    return JSONResponse(status_code=403, content={"error": "This computer isn't an approved upload station"})


def _thumb(path: str) -> str:
    return f"/api/station/photo?path={quote(path, safe='')}"


@router.get("/api/station/today")
def station_today(request: Request):
    """Everything uploaded today, by folder (where the photos are now), and today's photos in the trash."""
    if not main._upload_allowed(request):
        return _forbidden()
    day = main._mountain_today()
    with main._upload_log_lock:
        batches = [dict(b) for b in main._upload_log(day)]
    by_path = {it["path"]: it for it in main.data}
    groups, seen = {}, set()
    for b in batches:
        for p in b.get("paths", []):
            it = by_path.get(p)
            if not it or p in seen:
                continue
            seen.add(p)
            folder = (it.get("last_name") or it.get("group") or "").strip()
            g = groups.setdefault((it["date"], it["location"], folder),
                                  {"date": it["date"], "location": it["location"], "folder": folder,
                                   "by": [], "last": "", "photos": []})
            g["photos"].append({"path": p, "name": os.path.basename(p), "thumb": _thumb(p)})
            if b.get("by") and b["by"] not in g["by"]:
                g["by"].append(b["by"])
            g["last"] = max(g["last"], b.get("at", ""))
    out = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
    for g in out:
        g["photos"].sort(key=lambda x: natural_sort_key(x["name"]))
        g["count"] = len(g["photos"])
    logged = {p for b in batches for p in b.get("paths", [])}
    trash = []
    for t in main._load_trash_meta():
        entry = t.get("image_entry") or {}
        if t.get("type") == "studio" and entry.get("path") in logged and entry["path"] not in by_path:
            small = main._THUMB_VARIANTS["admin"][0](t["original_key"]) if t.get("in_place") else t["key"]
            trash.append({"path": entry["path"], "name": t.get("filename", ""), "date": t.get("date", ""),
                          "location": t.get("location", ""), "folder": t.get("folder", ""),
                          "trashed_at": t.get("trashed_at", ""), "thumb": main._presigned_get(small)})
    trash.sort(key=lambda x: x["trashed_at"], reverse=True)
    return {"day": day, "groups": out, "trash": trash}


def _todays(paths) -> list | None:
    """The paths asked for, if every one is a photo uploaded today; otherwise None."""
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return None
    today = main._todays_upload_paths()
    return paths if all(p in today for p in paths) else None


@router.get("/api/station/photo")
def station_photo(request: Request, path: str = Query(...), size: str = Query("thumb")):
    """A photo uploaded today, without a watermark (small, or the larger view)."""
    if not main._upload_allowed(request):
        return _forbidden()
    if not _todays([path]):
        return JSONResponse(status_code=404, content={"error": "Not one of today's uploads"})
    from routes.admin_photos import _studio_photo
    return _studio_photo(path, "medium" if size == "medium" else "thumb")


@router.post("/api/station/move")
async def station_move(request: Request):
    """Move some of today's photos to another date, location or family (as the studio's Move does)."""
    if not main._upload_allowed(request):
        return _forbidden()
    from routes.admin_photos import _move_photos, _move_request
    move = _move_request(await request.json())
    if isinstance(move, JSONResponse):
        return move
    if not _todays(move[0]):
        return JSONResponse(status_code=400, content={"error": "Only photos uploaded today can be moved here"})
    remap = {}
    result = await run_in_threadpool(_move_photos, *move, remap)
    main._remap_upload_log(remap)
    return result


@router.post("/api/station/trash")
async def station_trash(request: Request, body: dict = Body(...)):
    """Put some of today's photos in the trash (kept 7 days; they can come back until then)."""
    if not main._upload_allowed(request):
        return _forbidden()
    paths = _todays(body.get("paths"))
    if not paths:
        return JSONResponse(status_code=400, content={"error": "Only photos uploaded today can be trashed here"})
    from routes.admin_photos import _trash_photos
    return await _trash_photos(paths)


@router.post("/api/station/restore")
async def station_restore(request: Request, body: dict = Body(...)):
    """Bring one of today's photos back from the trash, to where it was."""
    if not main._upload_allowed(request):
        return _forbidden()
    paths = _todays([body.get("path")])
    if not paths:
        return JSONResponse(status_code=400, content={"error": "Only photos uploaded today can be brought back here"})
    item = next((t for t in main._load_trash_meta()
                 if t.get("type") == "studio" and (t.get("image_entry") or {}).get("path") == paths[0]), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "That photo isn't in the trash"})
    from routes.trash import _restore
    return await run_in_threadpool(_restore, item["key"])


# ── Studio: Settings > Security > Upload stations ───────────────────────────

@router.get("/api/admin/upload-stations")
def admin_upload_stations(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)       # Admin area: the owner's PIN
    if denied:
        return denied
    now = main._now_iso()
    stations = [{"id": k[:16], "name": v.get("name", ""), "describe": v.get("describe", ""),
                 "approved": v.get("approved", ""), "last_used": v.get("last_used", ""), "ip": v.get("ip", "")}
                for k, v in main._load_admin_auth()["upload_stations"].items() if v.get("expires", "") > now]
    return {"stations": sorted(stations, key=lambda x: x["approved"], reverse=True)}


def _station_key(d: dict, station_id: str) -> str | None:
    keys = [k for k in d["upload_stations"] if k[:16] == station_id]
    return keys[0] if len(keys) == 1 else None


@router.put("/api/admin/upload-stations/{station_id}")
def admin_rename_upload_station(station_id: str, request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)       # Admin area: the owner's PIN
    if denied:
        return denied
    name = " ".join(str(body.get("name") or "").split())
    if not name or len(name) > main.STORE_NAME_MAX:
        return JSONResponse(status_code=400, content={"error": f"Give it a name of up to {main.STORE_NAME_MAX} letters"})
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        key = _station_key(d, station_id)
        if not key:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        d["upload_stations"][key]["name"] = name
        main._save_admin_auth(d)
    return {"ok": True}


@router.delete("/api/admin/upload-stations/{station_id}")
def admin_remove_upload_station(station_id: str, request: Request):
    """Forget an upload station: it's locked out until it's approved with a new code."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    denied = main._owner_required(request)       # Admin area: the owner's PIN
    if denied:
        return denied
    d = main._load_admin_auth()
    with main._admin_auth_lock:
        key = _station_key(d, station_id)
        if not key:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        d["upload_stations"].pop(key)
        main._save_admin_auth(d)
    return {"ok": True}
