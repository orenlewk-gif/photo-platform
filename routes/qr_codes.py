"""Routes: QR codes the studio makes for flyers and signs (Studio > Links > QR Codes).

Each code points at a short address on this site (/q/<name>), which sends the visitor on to
wherever the studio sets, so a printed code's destination can change without reprinting.
Scans are counted (by day, Mountain time). Kept in meta/qr_codes.json."""
import json
import re
import threading
import time as _time
import uuid
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

import main

router = APIRouter()

QR_KEY = "meta/qr_codes.json"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_lock = threading.Lock()                    # writes take turns
_cache = {"data": None, "ts": 0.0}          # the list, remembered briefly for scans
_CACHE_SECONDS = 30
_pending = Counter()                        # scans not saved yet: (code id, day) -> count
_pending_lock = threading.Lock()
_flush_scheduled = {"on": False}
FLUSH_SECONDS = 20                          # scans are saved in batches, not one write per scan
KEEP_DAYS = 400


def _load() -> list:
    """The codes. A storage problem other than 'no codes yet' raises, so a save can't wipe the list."""
    try:
        obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=QR_KEY)
    except main.s3.exceptions.NoSuchKey:
        return []
    return json.loads(obj["Body"].read()).get("codes", [])


def _codes() -> list:
    if _cache["data"] is None or _time.monotonic() - _cache["ts"] > _CACHE_SECONDS:
        _cache["data"], _cache["ts"] = _load(), _time.monotonic()
    return _cache["data"]


def _save(codes: list) -> None:
    main.s3.put_object(Bucket=main.R2_BUCKET, Key=QR_KEY, ContentType="application/json",
                       Body=json.dumps({"codes": codes}, indent=1).encode())
    _cache["data"], _cache["ts"] = codes, _time.monotonic()


def _today() -> str:
    return datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")


def flush_scans() -> None:
    """Add the waiting scan counts to the saved list."""
    with _pending_lock:
        batch = dict(_pending)
        _pending.clear()
        _flush_scheduled["on"] = False
    if not batch:
        return
    try:
        with _lock:
            codes = _load()
            by_id = {c["id"]: c for c in codes}
            for (cid, day), n in batch.items():
                c = by_id.get(cid)
                if not c:
                    continue                 # deleted meanwhile
                days = c.setdefault("days", {})
                days[day] = days.get(day, 0) + n
                c["scans"] = c.get("scans", 0) + n
                c["last_scan"] = max(c.get("last_scan") or "", datetime.now(timezone.utc).isoformat())
                for old in sorted(days)[:-KEEP_DAYS]:
                    del days[old]
            _save(codes)
    except Exception as e:                   # keep the counts for the next try
        print(f"[qr] scan counts not saved yet: {e}")
        with _pending_lock:
            _pending.update(batch)


def _count_scan(cid: str) -> None:
    with _pending_lock:
        _pending[(cid, _today())] += 1
        if _flush_scheduled["on"]:
            return
        _flush_scheduled["on"] = True
    t = threading.Timer(FLUSH_SECONDS, flush_scans)
    t.daemon = True
    t.start()


def _target_problem(target: str) -> str:
    """'' if the destination is a web address (https://...) or a page on this site (/...)."""
    if target.startswith("/") and not target.startswith("//"):
        return ""
    u = urlparse(target)
    if u.scheme in ("http", "https") and u.netloc:
        return ""
    return "Enter a web address starting with https:// (or a page on this site starting with /)"


def _row(c: dict) -> dict:
    days = dict(c.get("days") or {})
    waiting = 0
    with _pending_lock:                      # counts still waiting to be saved
        for (cid, day), n in _pending.items():
            if cid == c["id"]:
                days[day] = days.get(day, 0) + n
                waiting += n
    today = datetime.now(ZoneInfo("America/Denver")).date()
    recent = lambda n: sum(v for d, v in days.items()
                           if (today - datetime.strptime(d, "%Y-%m-%d").date()).days < n)
    return {"id": c["id"], "name": c.get("name", ""), "slug": c["slug"], "target": c.get("target", ""),
            "short_url": f"{main.SITE_URL}/q/{c['slug']}", "created": c.get("created", ""),
            "scans": c.get("scans", 0) + waiting, "last_7": recent(7), "last_30": recent(30),
            "last_scan": datetime.now(timezone.utc).isoformat() if waiting else c.get("last_scan", ""),
            "days": dict(sorted(days.items())[-30:])}


# ── The printed code's address ────────────────────────────────────────────────

@router.get("/q/{slug}")
def qr_scan(slug: str):
    """Someone scanned a code: count it and send them on. An unknown code goes to the gallery."""
    try:
        c = next((c for c in _codes() if c["slug"] == slug.lower()), None)
    except Exception:
        c = None
    if not c:
        return RedirectResponse("/", status_code=302)
    _count_scan(c["id"])
    return RedirectResponse(c["target"], status_code=302, headers={"Cache-Control": "no-store"})


# ── Studio ────────────────────────────────────────────────────────────────────

def _refused(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return None


@router.get("/api/admin/qr-codes")
def admin_qr_codes(request: Request):
    if (r := _refused(request)):
        return r
    return {"codes": [_row(c) for c in sorted(_load(), key=lambda c: c.get("created", ""), reverse=True)]}


def _slug_from(name: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:40]


@router.post("/api/admin/qr-codes")
def admin_qr_create(request: Request, body: dict = Body(...)):
    if (r := _refused(request)):
        return r
    name = str(body.get("name") or "").strip()[:80]
    target = str(body.get("target") or "").strip()
    slug = (str(body.get("slug") or "").strip().lower() or _slug_from(name))
    if not name:
        return JSONResponse(status_code=400, content={"error": "Give the code a name"})
    if not _SLUG_RE.match(slug):
        return JSONResponse(status_code=400, content={
            "error": "The short name can use lowercase letters, numbers and dashes (up to 40)"})
    if (problem := _target_problem(target)):
        return JSONResponse(status_code=400, content={"error": problem})
    with _lock:
        codes = _load()
        if any(c["slug"] == slug for c in codes):
            return JSONResponse(status_code=409, content={"error": f"/q/{slug} is already used — pick another short name"})
        code = {"id": uuid.uuid4().hex[:12], "name": name, "slug": slug, "target": target,
                "created": datetime.now(timezone.utc).isoformat(), "scans": 0, "days": {}}
        codes.append(code)
        _save(codes)
    return _row(code)


@router.put("/api/admin/qr-codes/{cid}")
def admin_qr_update(cid: str, request: Request, body: dict = Body(...)):
    """Change a code's name or destination. Its short address never changes (it's printed)."""
    if (r := _refused(request)):
        return r
    with _lock:
        codes = _load()
        c = next((c for c in codes if c["id"] == cid), None)
        if not c:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if "name" in body:
            name = str(body.get("name") or "").strip()[:80]
            if not name:
                return JSONResponse(status_code=400, content={"error": "Give the code a name"})
            c["name"] = name
        if "target" in body:
            target = str(body.get("target") or "").strip()
            if (problem := _target_problem(target)):
                return JSONResponse(status_code=400, content={"error": problem})
            c["target"] = target
        _save(codes)
    return _row(c)


@router.delete("/api/admin/qr-codes/{cid}")
def admin_qr_delete(cid: str, request: Request):
    """A deleted code's printed copies then open the gallery."""
    if (r := _refused(request)):
        return r
    with _lock:
        codes = _load()
        kept = [c for c in codes if c["id"] != cid]
        if len(kept) == len(codes):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        _save(kept)
    return {"ok": True}


@router.get("/api/admin/qr-codes/{cid}/image")
def admin_qr_image(cid: str, request: Request, format: str = Query("png")):
    """The code to print: PNG (about 1,200 px, for flyers and social posts) or SVG (sharp at any
    size, for a print shop or design program). Extra error correction so it still scans when
    printed small, slightly scuffed, or with a logo over the middle."""
    if (r := _refused(request)):
        return r
    c = next((c for c in _load() if c["id"] == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    import segno
    qr = segno.make(f"{main.SITE_URL}/q/{c['slug']}", error="q")
    buf = BytesIO()
    if format == "svg":
        qr.save(buf, kind="svg", scale=10, border=4, dark="#000000", light="#ffffff")
        media, ext = "image/svg+xml", "svg"
    else:
        scale = max(1, 1200 // (qr.symbol_size(border=4)[0]))
        qr.save(buf, kind="png", scale=scale, border=4, dark="#000000", light="#ffffff")
        media, ext = "image/png", "png"
    return Response(buf.getvalue(), media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="qr-{c["slug"]}.{ext}"',
                             "Cache-Control": "no-store"})
