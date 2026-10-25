"""Partner programs (Eagle Mount): a password-protected page where participants download their
photos free, by day. Photos are marked for a partner at upload (their color label). Every
download is logged for the partner's in-kind donation report."""
import csv
import io
import json
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html import escape
from io import BytesIO

from fastapi import APIRouter, Body, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from PIL import Image

import main
from core.images import fix_orientation
from core.pages import render_page, site_footer, site_header
from core.text import natural_sort_key, to_r2_key

router = APIRouter()


def _closed():
    return HTMLResponse(main._pkg_error("This page isn't available.", 404), status_code=404)


def _page_head(p: dict) -> dict:
    return {"partner": escape(p["name"]), "slug": escape(p["slug"]), "welcome": escape(p.get("welcome") or ""),
            "site_header": site_header(), "site_footer": site_footer()}


# The page itself lives at the short address photos.bigskyphotos.com/<slug> (e.g. /eaglemount).
# These catch-all routes are on their own router, included after every other one in main.py, so
# they never take an address from another page; anything that isn't a partner gets the normal 404.
page_router = APIRouter()


def _partner_or_404(slug: str):
    p = main._partner_by_slug(slug)
    if not p:
        raise HTTPException(status_code=404)
    return p


@page_router.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
def partner_page(slug: str, request: Request):
    p = _partner_or_404(slug)
    if not p.get("password"):
        return _closed()
    if not main._partner_signed_in(request, p):
        return HTMLResponse(render_page("partner_login.html", **_page_head(p), error=""))
    return HTMLResponse(render_page("partner.html", **_page_head(p)))


@page_router.post("/{slug}", response_class=HTMLResponse, include_in_schema=False)
def partner_login(slug: str, request: Request, password: str = Form("")):
    p = _partner_or_404(slug)
    if not p.get("password"):
        return _closed()
    if main._pin_locked_out(request):
        return HTMLResponse(render_page("partner_login.html", **_page_head(p),
                                        error="Too many tries. Please wait 10 minutes."), status_code=429)
    if not main.hmac.compare_digest(password.strip().encode(), p["password"].encode()):
        main._record_pin_failure(request)
        return HTMLResponse(render_page("partner_login.html", **_page_head(p),
                                        error="That password isn't right."), status_code=401)
    token, max_age = main._partner_token(p)
    r = RedirectResponse(f"/{p['slug']}", status_code=303)
    r.set_cookie(main._partner_cookie_name(p), token, max_age=max_age, httponly=True, samesite="lax")
    return r


def _signed_in_partner(slug: str, request: Request):
    p = main._partner_by_slug(slug)
    if not p or not main._partner_signed_in(request, p):
        return None
    return p


@router.get("/api/partner/{slug}/photos")
def partner_photos(slug: str, request: Request):
    """The partner's photos by day, newest day first, photos in shooting order."""
    p = _signed_in_partner(slug, request)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Please enter the password"})
    by_day = main._partner_photos(p)
    main._fill_taken_times([i for items in by_day.values() for i in items])   # usually done at upload
    days = []
    for date, items in sorted(by_day.items(), reverse=True):
        photos = sorted(items, key=main._taken_order)            # in the order they were taken
        days.append({"date": date, "photos": [{"path": i["path"], "filename": os.path.basename(i["path"])}
                                              for i in photos]})
    return {"days": days, "family_code": p.get("family_code") or "", "family_code_message": main._family_code_message(p),
            "print_pct": float(p.get("print_discount_pct") or 0)}


@router.get("/partner/{slug}/thumb")
def partner_thumb(slug: str, request: Request, path: str = Query("")):
    p = _signed_in_partner(slug, request)
    if not p or not main._partner_photo(p, path):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(path)
    link = main._stored_thumb_link(key, "pos")             # the unwatermarked thumbnail
    if link:
        return main._thumb_redirect(link)
    data = main._thumb_on_demand(key, "pos")
    return StreamingResponse(BytesIO(data), media_type=main._THUMB_MEDIA_TYPES[main._THUMB_VARIANTS["pos"][5]],
                             headers={"Cache-Control": "private, max-age=86400"})


@router.get("/partner/{slug}/view")
def partner_view(slug: str, request: Request, path: str = Query("")):
    """The enlarged view: 1800 px, no watermark. Served from its stored copy (made when the
    photo was marked or uploaded); the first view of an older photo makes it."""
    p = _signed_in_partner(slug, request)
    if not p or not main._partner_photo(p, path):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(path)
    link = main._stored_thumb_link(key, "large")
    if link:
        return main._thumb_redirect(link)
    data = main._make_thumbs(key, ["large"])["large"]
    return StreamingResponse(BytesIO(data), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/partner/{slug}/file")
def partner_file(slug: str, request: Request, path: str = Query("")):
    """One original file."""
    p = _signed_in_partner(slug, request)
    item = main._partner_photo(p, path) if p else None
    if not item:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    main._log_partner_downloads(p, [item], "single")
    key = to_r2_key(path)
    link = main.s3.generate_presigned_url("get_object", ExpiresIn=3600, Params={
        "Bucket": main.R2_BUCKET, "Key": key,
        "ResponseContentDisposition": f'attachment; filename="{os.path.basename(key)}"'})
    return RedirectResponse(link)


@router.post("/partner/{slug}/zip")
def partner_zip(slug: str, request: Request, paths: str = Form("[]"), date: str = Form("")):
    """Original files as one zip: a whole day (date) or the photos picked (paths, a JSON list)."""
    p = _signed_in_partner(slug, request)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Please enter the password"})
    if date:
        items = main._partner_photos(p).get(date, [])
        how, name = "day", f"{p['name']} {date}"
    else:
        try:
            wanted = json.loads(paths)
        except ValueError:
            wanted = []
        items = [i for i in (main._partner_photo(p, x) for x in dict.fromkeys(wanted) if isinstance(x, str)) if i]
        how, name = "selection", f"{p['name']} photos"
    main._fill_taken_times(items)                               # usually done at upload
    items = sorted(items, key=main._taken_order)
    if not items:
        return JSONResponse(status_code=404, content={"error": "No photos"})
    main._log_partner_downloads(p, items, how)
    filename = "".join(c for c in name if c.isalnum() or c in " -_").strip().replace(" ", "_") + ".zip"

    def fetch(item):
        try:
            return item, main.s3.get_object(Bucket=main.R2_BUCKET, Key=to_r2_key(item["path"]))["Body"].read()
        except Exception as e:
            print(f"[partner zip] skipped {item['path']}: {e}")
            return item, None

    def iter_zip():
        # Same streaming as the download page's Download All: a few photos fetched ahead,
        # each sent as soon as it's added, so nothing piles up in memory
        out = main._ZipStream()
        zf = zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ahead = [pool.submit(fetch, i) for i in items[:4]]
            nxt = len(ahead)
            while ahead:
                item, data = ahead.pop(0).result()
                if nxt < len(items):
                    ahead.append(pool.submit(fetch, items[nxt])); nxt += 1
                if data is not None:
                    zf.writestr(os.path.basename(item["path"]), data)
                    yield out.take()
        zf.close()
        rest = out.take()
        if rest:
            yield rest

    return StreamingResponse(iter_zip(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/partner/{slug}/links")
def partner_links(slug: str, request: Request, body: dict = Body(...)):
    """Short-lived direct links to originals for "Save to Photos" on phones and tablets
    (fetched straight from storage). Logged like any other download."""
    p = _signed_in_partner(slug, request)
    if not p:
        return JSONResponse(status_code=401, content={"error": "Please enter the password"})
    wanted = body.get("paths") if isinstance(body.get("paths"), list) else []
    items = [i for i in (main._partner_photo(p, x) for x in dict.fromkeys(wanted) if isinstance(x, str)) if i]
    if not items:
        return JSONResponse(status_code=404, content={"error": "No photos"})
    main._log_partner_downloads(p, items, "save")
    return {"files": [{"name": os.path.basename(i["path"]),
                       "url": main.s3.generate_presigned_url("get_object", ExpiresIn=3600, Params={
                           "Bucket": main.R2_BUCKET, "Key": to_r2_key(i["path"])})} for i in items]}


# ── Settings (Admin > Settings > Partners) ────────────────────────────────────

@router.get("/api/admin/partner-settings")
def partner_settings(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"partners": main._load_partners(), "default_message": main.FAMILY_CODE_MESSAGE_DEFAULT,
            "site_url": main.SITE_URL}


@router.post("/api/admin/partner-settings")
def save_partner_settings(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    try:
        cleaned = main._clean_partners(body.get("partners"))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    before = {p["id"]: p for p in main._load_partners()}
    main._save_partners(cleaned)
    notes = [n for n in (main._sync_family_code(p, before.get(p["id"], {}).get("family_code", "")) for p in cleaned) if n]
    return {"partners": cleaned, "notes": notes}


def _report_args(request: Request, partner_id: str, date_from: str, date_to: str):
    if not main._admin_authed(request):
        return None, JSONResponse(status_code=401, content={"error": "Unauthorized"})
    p = next((x for x in main._load_partners() if x["id"] == partner_id), None)
    if not p:
        return None, JSONResponse(status_code=404, content={"error": "Unknown partner"})
    for d in (date_from, date_to):
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return None, JSONResponse(status_code=400, content={"error": "Dates must look like 2026-12-05"})
    return p, None


@router.get("/api/admin/partner-report")
def partner_report(request: Request, partner: str = Query(""), date_from: str = Query(""), date_to: str = Query("")):
    """The in-kind donation report for the Settings page (totals and by day)."""
    p, err = _report_args(request, partner, date_from, date_to)
    if err:
        return err
    rep = main._partner_report(p, date_from, date_to)
    rep.pop("rows")
    return rep


@router.get("/admin/partner-report.csv")
def partner_report_csv(request: Request, partner: str = Query(""), date_from: str = Query(""), date_to: str = Query("")):
    """The same report as a spreadsheet: one row per photo, then the totals."""
    p, err = _report_args(request, partner, date_from, date_to)
    if err:
        return err
    rep = main._partner_report(p, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{rep['partner']} — photos donated (Crystal Images)"])
    w.writerow([f"Downloaded {date_from or 'from the start'} to {date_to or 'today'}"])
    w.writerow([])
    w.writerow(["First downloaded", "Photo taken", "File", "Times downloaded", "Value"])
    for r in rep["rows"]:
        w.writerow([r["first_download"], r["photo_date"], r["file"], r["downloads"], f"${rep['value_per_photo']:.2f}"])
    w.writerow([])
    w.writerow(["Total photos", rep["photos"]])
    w.writerow(["Value per photo", f"${rep['value_per_photo']:.2f}"])
    w.writerow(["Total donated value", f"${rep['total_value']:.2f}"])
    name = re.sub(r"[^A-Za-z0-9]+", "-", rep["partner"]).strip("-").lower() or "partner"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-donated-photos.csv"'})
