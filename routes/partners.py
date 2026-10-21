"""Partner programs (Eagle Mount): a password-protected page where participants download their
photos free, by day. Photos are marked for a partner at upload (their color label). Every
download is logged for the partner's in-kind donation report."""
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html import escape
from io import BytesIO

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from PIL import Image

import main
from core.images import fix_orientation
from core.pages import render_page
from core.text import natural_sort_key, to_r2_key

router = APIRouter()


def _closed():
    return HTMLResponse(main._pkg_error("This page isn't available.", 404), status_code=404)


def _page_head(p: dict) -> dict:
    return {"partner": escape(p["name"]), "slug": escape(p["slug"]), "welcome": escape(p.get("welcome") or "")}


@router.get("/eaglemount", include_in_schema=False)
def eaglemount_short_link():
    return RedirectResponse("/partner/eaglemount")


@router.get("/partner/{slug}", response_class=HTMLResponse)
def partner_page(slug: str, request: Request):
    p = main._partner_by_slug(slug)
    if not p or not p.get("password"):
        return _closed()
    if not main._partner_signed_in(request, p):
        return HTMLResponse(render_page("partner_login.html", **_page_head(p), error=""))
    return HTMLResponse(render_page("partner.html", **_page_head(p)))


@router.post("/partner/{slug}/login", response_class=HTMLResponse)
def partner_login(slug: str, request: Request, password: str = Form("")):
    p = main._partner_by_slug(slug)
    if not p or not p.get("password"):
        return _closed()
    if main._pin_locked_out(request):
        return HTMLResponse(render_page("partner_login.html", **_page_head(p),
                                        error="Too many tries. Please wait 10 minutes."), status_code=429)
    if not main.hmac.compare_digest(password.strip().encode(), p["password"].encode()):
        main._record_pin_failure(request)
        return HTMLResponse(render_page("partner_login.html", **_page_head(p),
                                        error="That password isn't right."), status_code=401)
    token, max_age = main._partner_token(p)
    r = RedirectResponse(f"/partner/{p['slug']}", status_code=303)
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
    days = []
    for date, items in sorted(main._partner_photos(p).items(), reverse=True):
        photos = sorted(items, key=lambda i: natural_sort_key(i["path"]))
        days.append({"date": date, "photos": [{"path": i["path"], "filename": os.path.basename(i["path"])}
                                              for i in photos]})
    return {"days": days, "family_code": p.get("family_code") or "", "family_code_pct": p.get("family_code_pct")}


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
    """The enlarged view: 1800 px, no watermark."""
    p = _signed_in_partner(slug, request)
    if not p or not main._partner_photo(p, path):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=to_r2_key(path))
    img = fix_orientation(Image.open(obj["Body"]).convert("RGB"))
    img.thumbnail((1800, 1800), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


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
    items = sorted(items, key=lambda i: natural_sort_key(i["path"]))
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
