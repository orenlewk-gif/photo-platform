"""Routes: downloads. Moved from main.py unchanged; shared helpers still live in main.py."""
import os
import re
import uuid
import zipfile
from PIL import Image
from concurrent.futures import ThreadPoolExecutor
from core.images import fix_orientation
from core.text import to_r2_key
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from io import BytesIO

from fastapi import APIRouter

from html import escape as _html_escape

import main
from core.pages import render_page

router = APIRouter()


@router.get("/download/{token}")
def download_page(token: str):
    rec = main._load_token(token)
    if not rec:
        return HTMLResponse("<h2>Invalid or expired download link.</h2>", status_code=404)

    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;color:#c0392b'>This download link has expired.</h2>"
            "<p style='font-family:sans-serif'>Please contact us at bigskyphotos.com to receive a new link.</p>",
            status_code=410
        )

    expire_str = expires_dt.strftime("%B %d, %Y")
    photo_rows = ""
    for i, path in enumerate(rec["paths"]):
        fname = os.path.basename(path)
        photo_rows += f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    padding:0.65rem 0;border-bottom:1px solid rgba(255,255,255,0.08);">
          <span style="font-size:0.9rem;color:rgba(255,255,255,0.8)">{fname}</span>
          <a href="/download/{token}/{i}" download="{fname}"
             style="background:#F2C94C;color:#0d1f2d;padding:0.4rem 1rem;border-radius:6px;
                    font-weight:700;font-size:0.85rem;text-decoration:none;">
            Download
          </a>
        </div>"""

    html = render_page("download_order.html", order_id=rec["order_id"], location=_html_escape(str(rec["location"])),
                       date=_html_escape(str(rec["date"])), expires=expire_str, file_count=len(rec["paths"]),
                       token=token, photo_rows=photo_rows)
    return HTMLResponse(html)


@router.get("/download/{token}/all")
def download_all(token: str):
    import zipfile
    rec = main._load_token(token)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Invalid link"})
    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return JSONResponse(status_code=410, content={"error": "Link expired"})

    paths = rec["paths"]
    filename = f"crystal-images-order-{rec['order_id']}.zip"

    def iter_zip():
        buf = BytesIO()
        last_pos = 0
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True)
        for path in paths:
            key = to_r2_key(path)
            try:
                obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=key)
                data = obj["Body"].read()
                zf.writestr(os.path.basename(path), data)
                # Yield only the new bytes written since last flush
                cur_pos = buf.tell()
                buf.seek(last_pos)
                chunk = buf.read(cur_pos - last_pos)
                last_pos = cur_pos
                if chunk:
                    yield chunk
            except Exception as e:
                print(f"  zip: skipped {key}: {e}")
        # Close writes the central directory — yield those final bytes
        zf.close()
        buf.seek(last_pos)
        remainder = buf.read()
        if remainder:
            yield remainder

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/download/{token}/{idx}")
def download_file(token: str, idx: int):
    rec = main._load_token(token)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "Invalid link"})

    expires_dt = datetime.fromisoformat(rec["expires"])
    if datetime.now(timezone.utc) > expires_dt:
        return JSONResponse(status_code=410, content={"error": "Link expired"})

    if idx < 0 or idx >= len(rec["paths"]):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    key = to_r2_key(rec["paths"][idx])
    presigned = main.s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": main.R2_BUCKET, "Key": key,
                "ResponseContentDisposition": f"attachment; filename={os.path.basename(key)}"},
        ExpiresIn=3600
    )
    return RedirectResponse(presigned)


@router.get("/api/admin/order-link/{order_id}")
def api_admin_order_link(request: Request, order_id: int):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    token, rec, expires_dt = main._get_best_token_for_order(order_id)
    if not rec:
        return JSONResponse(status_code=404, content={"error": "No download record found"})
    expired = datetime.now(timezone.utc) > expires_dt
    return {"url": f"{main.SITE_URL}/download/{token}", "expired": expired,
            "expires": expires_dt.isoformat()}


@router.get("/admin/regen/{order_id}")
def admin_regen(request: Request, order_id: int):
    if not main._admin_authed(request):
        return RedirectResponse("/admin")
    token, rec = main._get_token_by_order_id(order_id)
    if not rec:
        return HTMLResponse("<h2 style='font-family:sans-serif;color:#e53e3e;padding:2rem'>Order not found in download records.</h2>")
    new_token  = str(uuid.uuid4()).replace("-", "")
    new_expires = (datetime.now(timezone.utc) + timedelta(days=main.DOWNLOAD_EXPIRE_DAYS)).isoformat()
    rec["expires"] = new_expires
    main._store_token(new_token, rec)
    new_url = f"{main.SITE_URL}/download/{new_token}"
    html = render_page("link_regenerated.html", customer=_html_escape(str(rec.get('customer', ''))),
                       email=_html_escape(str(rec.get('email', ''))), new_url=new_url)
    return HTMLResponse(html)


@router.post("/api/admin/packages/upload")
async def api_create_package_upload(request: Request):
    """Create a download package by uploading photos directly (not from the site gallery)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from fastapi import UploadFile, File, Form
    form = await request.form()
    title      = (form.get("title") or "").strip()
    emails_raw = (form.get("emails") or "").strip()
    expire_days = int(form.get("expire_days") or 30)
    files = form.getlist("photos")

    emails = [e.strip() for e in emails_raw.replace(",", "\n").split("\n") if e.strip()]
    if not title or not emails or not files:
        return JSONResponse(status_code=400, content={"error": "title, emails, and at least one photo required"})

    token = str(uuid.uuid4()).replace("-", "")
    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(days=expire_days)).isoformat()
    expire_str = (now + timedelta(days=expire_days)).strftime("%B %d, %Y")

    paths = []
    for f in files:
        if not hasattr(f, "filename") or not f.filename:
            continue
        safe_name = f.filename.replace("/", "_").replace("\\", "_")
        r2_key = f"packages/uploads/{token}/{safe_name}"
        data = await f.read()
        main.s3.put_object(Bucket=main.R2_BUCKET, Key=r2_key, Body=data,
                      ContentType=f.content_type or "image/jpeg")
        paths.append(r2_key)

    if not paths:
        return JSONResponse(status_code=400, content={"error": "No valid photos received"})

    pkg = {
        "token": token, "title": title, "emails": emails,
        "paths": paths, "created": now.isoformat(),
        "expires": expires, "expire_days": expire_days,
    }
    main._store_pkg(token, pkg)

    download_url = f"{main.SITE_URL}/package/{token}"
    main._send_package_email(emails, title, download_url, len(paths), expire_str)
    return {"token": token, "url": download_url}


@router.post("/api/admin/packages")
async def api_create_package(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    title = body.get("title", "").strip()
    emails = [e.strip() for e in body.get("emails", []) if e.strip()]
    paths = body.get("paths", [])
    expire_days = int(body.get("expire_days", 30))

    if not title or not emails or not paths:
        return JSONResponse(status_code=400, content={"error": "title, emails, and paths required"})

    token = str(uuid.uuid4()).replace("-", "")
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=expire_days)).isoformat()
    expire_str = (now + timedelta(days=expire_days)).strftime("%B %d, %Y")

    pkg = {
        "token": token,
        "title": title,
        "emails": emails,
        "paths": paths,
        "created": now.isoformat(),
        "expires": expires,
        "expire_days": expire_days,
    }
    main._store_pkg(token, pkg)

    download_url = f"{main.SITE_URL}/package/{token}"
    main._send_package_email(emails, title, download_url, len(paths), expire_str)

    return {"token": token, "url": download_url}


@router.get("/api/admin/packages")
def api_list_packages(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pkgs = main._list_pkgs()
    now = datetime.now(timezone.utc)
    result = []
    for p in pkgs:
        try:
            exp = datetime.fromisoformat(p["expires"])
            expired = now > exp
        except Exception:
            expired = False
        result.append({
            "token":   p["token"],
            "title":   p["title"],
            "emails":  p.get("emails", []),
            "count":   len(p.get("paths", [])),
            "created": p.get("created", "")[:10],
            "expires": p.get("expires", "")[:10],
            "expired": expired,
        })
    return {"packages": result}


@router.delete("/api/admin/packages/{token}")
def api_delete_package(token: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    main._delete_pkg_r2(token)
    return {"status": "ok"}


@router.post("/api/admin/packages/{token}/extend")
async def api_extend_package(token: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    days = int(body.get("days", 30))
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    pkg["expires"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    pkg["expire_days"] = days
    main._store_pkg(token, pkg)
    return {"expires": pkg["expires"]}


@router.post("/api/admin/packages/{token}/resend-email")
async def api_resend_pkg_email(token: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    download_url = f"{main.SITE_URL}/package/{token}"
    try:
        exp_str = datetime.fromisoformat(pkg["expires"]).strftime("%B %d, %Y")
    except Exception:
        exp_str = "soon"
    main._send_package_email(pkg.get("emails", []), pkg["title"], download_url, len(pkg.get("paths", [])), exp_str)
    return {"status": "ok"}


@router.get("/package/{token}")
def package_page(token: str):
    pkg = main._load_pkg(token)
    if not pkg:
        return HTMLResponse(main._pkg_error("Invalid or expired download link.", 404), status_code=404)
    try:
        expires_dt = datetime.fromisoformat(pkg["expires"])
    except Exception:
        return HTMLResponse(main._pkg_error("Invalid link.", 404), status_code=404)
    if datetime.now(timezone.utc) > expires_dt:
        return HTMLResponse(main._pkg_error(
            "This download link has expired.",
            410,
            sub="Please contact us at <a href='mailto:info@bigskyphotos.com' style='color:#F2C94C'>info@bigskyphotos.com</a> to receive a new link."
        ), status_code=410)

    paths = pkg.get("paths", [])
    expire_str = expires_dt.strftime("%B %d, %Y")
    title_esc = pkg["title"].replace("<", "&lt;").replace(">", "&gt;")

    photo_items = "\n".join(
        f'<div class="pi"><img src="/package/{token}/thumb/{i}" alt="Photo {i + 1} of {len(paths)}"><a href="/package/{token}/file/{i}" download class="dib" aria-label="Download photo {i + 1}">⬇ Download</a></div>'
        for i, _ in enumerate(paths)
    )

    notice = ""
    if pkg.get("unlimited"):
        # Unlimited package: photos are added during the trip, so say so (and when it's still empty)
        expire_str = main._mountain_date_str(pkg["expires"])
        notice = f'<div class="note">{_html_escape(main._unlimited_note(pkg["unlimited"].get("last_day", ""), expire_str))}</div>'
        if not paths:
            notice += '<div class="empty">No photos yet. They will show up here after your first day on the mountain.</div>'

    return HTMLResponse(render_page("package.html", title=title_esc, photo_count=len(paths),
                                    plural="s" if len(paths) != 1 else "", expires=expire_str,
                                    token=token, photo_items=photo_items, notice=notice,
                                    acts_hidden="" if paths else ' style="display:none"'))


@router.get("/package/{token}/thumb/{idx}")
def package_thumb(token: str, idx: int):
    from fastapi.responses import StreamingResponse as SR
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    try:
        # Package photos may be uploaded files rather than gallery photos: studio size only
        data = main._cached_thumb(key, "admin") or main._make_thumbs(key, ["admin"])["admin"]
        return SR(BytesIO(data), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/package/{token}/medium/{idx}")
def package_medium(token: str, idx: int):
    from fastapi.responses import StreamingResponse as SR
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    try:
        obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=key)
        img = Image.open(obj["Body"]).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((1800, 1800), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        buf.seek(0)
        return SR(buf, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/package/{token}/file/{idx}")
def package_file(token: str, idx: int):
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    if idx < 0 or idx >= len(paths):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    key = to_r2_key(paths[idx])
    presigned = main.s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": main.R2_BUCKET, "Key": key,
                "ResponseContentDisposition": f"attachment; filename={os.path.basename(key)}"},
        ExpiresIn=3600,
    )
    return RedirectResponse(presigned)


@router.get("/package/{token}/links")
def package_links(token: str):
    """Short-lived direct links to the originals, so the page can fetch them for
    "Save to Photos" straight from storage (fast, and not through this server)."""
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    files = []
    for path in pkg.get("paths", []):
        key = to_r2_key(path)
        files.append({"name": os.path.basename(key),
                      "url": main.s3.generate_presigned_url("get_object", Params={"Bucket": main.R2_BUCKET, "Key": key},
                                                       ExpiresIn=3600)})
    return {"files": files}


@router.get("/package/{token}/all")
def package_all(token: str):
    pkg = main._load_pkg(token)
    if not pkg:
        return JSONResponse(status_code=404, content={"error": "Invalid"})
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(pkg["expires"]):
            return JSONResponse(status_code=410, content={"error": "Expired"})
    except Exception:
        pass
    paths = pkg.get("paths", [])
    safe_title = re.sub(r'[^\w\-_. ]', '', pkg.get("title", "photos"))[:60].strip().replace(" ", "_")
    filename = f"{safe_title}.zip"

    def _fetch(path):
        try:
            return path, main.s3.get_object(Bucket=main.R2_BUCKET, Key=to_r2_key(path))["Body"].read()
        except Exception as e:
            print(f"[zip] skipped {path}: {e}")
            return path, None

    def iter_zip():
        # Fetch a few photos ahead in parallel while earlier ones are being sent,
        # and send each one as soon as it's added (nothing piles up in memory)
        out = main._ZipStream()
        zf = zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ahead = [pool.submit(_fetch, p) for p in paths[:4]]
            nxt = len(ahead)
            while ahead:
                path, data = ahead.pop(0).result()
                if nxt < len(paths):
                    ahead.append(pool.submit(_fetch, paths[nxt])); nxt += 1
                if data is not None:
                    zf.writestr(os.path.basename(path), data)
                    yield out.take()
        zf.close()
        rem = out.take()
        if rem: yield rem

    from fastapi.responses import StreamingResponse
    return StreamingResponse(iter_zip(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/admin/links", response_class=HTMLResponse)
def admin_links_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/links")
    new_token = request.query_params.get("new", "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", new_token):
        new_token = ""            # only a real package id is ever written into the page
    new_url   = f"{main.SITE_URL}/package/{new_token}" if new_token else ""
    html = (open("templates/admin_links.html").read()
            .replace("__NEW_TOKEN__", new_token)
            .replace("__NEW_URL__", new_url))
    return HTMLResponse(html)
