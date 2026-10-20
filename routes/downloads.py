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

import main

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

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your Photos — Crystal Images</title>
<style>
  body{{margin:0;background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh;padding:2rem 1rem;box-sizing:border-box}}
  .wrap{{max-width:680px;margin:0 auto}}
  h1{{color:#F2C94C;margin-bottom:0.25rem}}
  .sub{{color:rgba(255,255,255,0.5);margin-bottom:2rem;font-size:0.9rem}}
  .card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:1.5rem;margin-bottom:1.5rem}}
  .license-title{{font-weight:700;color:#F2C94C;margin-bottom:0.75rem;font-size:1rem}}
  .license-item{{display:flex;gap:0.5rem;margin-bottom:0.4rem;font-size:0.88rem;color:rgba(255,255,255,0.75)}}
  .expire{{font-size:0.82rem;color:rgba(255,255,255,0.4);margin-top:1rem}}
</style></head><body><div class="wrap">
  <h1>Your Photos Are Ready</h1>
  <div class="sub">Order #{rec['order_id']} &nbsp;·&nbsp; {rec['location']} &nbsp;·&nbsp; {rec['date']}</div>

  <div class="card">
    <div class="license-title">📋 Digital Photo License</div>
    <div class="license-item"><span>✓</span><span>Personal use permitted — print, frame, and share freely.</span></div>
    <div class="license-item"><span>✓</span><span>Commercial use permitted <strong>when you tag @crystalimagesbigsky on Instagram.</strong></span></div>
    <div class="license-item"><span>✗</span><span>No resale or redistribution of the original image files.</span></div>
    <div class="license-item"><span>✗</span><span>Do not sell or transfer these files to third parties.</span></div>
    <div class="license-item"><span>📸</span><span>We love seeing your memories — tag us when you share!</span></div>
    <div style="margin-top:0.75rem;font-size:0.8rem;color:rgba(255,255,255,0.4)">
      By downloading you agree to these terms.
      Links expire <strong style="color:rgba(255,255,255,0.6)">{expire_str}</strong> —
      contact us anytime at info@bigskyphotos.com to resend.
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;flex-wrap:wrap;gap:0.75rem;">
      <div class="license-title" style="margin-bottom:0;">⬇ Download Your Photos ({len(rec['paths'])} files)</div>
      <a href="/download/{token}/all" download="crystal-images-order-{rec['order_id']}.zip"
         style="background:#F2C94C;color:#0d1f2d;padding:0.5rem 1.25rem;border-radius:6px;
                font-weight:700;font-size:0.9rem;text-decoration:none;white-space:nowrap;">
        ⬇ Download All
      </a>
    </div>
    {photo_rows}
  </div>
</div></body></html>"""
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
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Link Regenerated</title>
<style>body{{background:#0c2336;font-family:'Segoe UI',sans-serif;color:#fff;padding:2rem;}}
.card{{background:#0a1e2e;border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:2rem;max-width:560px;}}
h2{{color:#4ade80;margin-bottom:1rem}}p{{color:rgba(255,255,255,.6);font-size:.9rem;margin-bottom:.75rem}}
.url{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:8px;
  padding:.75rem 1rem;font-family:monospace;font-size:.85rem;word-break:break-all;color:#F5C518;margin-bottom:1.25rem}}
a.back{{color:#F5C518;font-size:.9rem}}</style></head><body>
<div class="card">
  <h2>✓ Download Link Regenerated</h2>
  <p>Customer: <strong>{rec.get('customer','')} ({rec.get('email','')})</strong></p>
  <p>New link (valid 30 days):</p>
  <div class="url">{new_url}</div>
  <p>Copy and send this to the customer.</p>
  <a class="back" href="/admin/orders">← Back to orders</a>
</div></body></html>"""
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
        f'<div class="pi"><img src="/package/{token}/thumb/{i}" alt=""><a href="/package/{token}/file/{i}" download class="dib">⬇ Download</a></div>'
        for i, _ in enumerate(paths)
    )

    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_esc} — Crystal Images</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1f2d;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh}}
header{{background:rgba(0,0,0,.35);border-bottom:1px solid rgba(255,255,255,.08);padding:.9rem 1.5rem;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.75rem;
  position:sticky;top:0;z-index:10;backdrop-filter:blur(8px)}}
header h1{{color:#F2C94C;font-size:1.1rem;font-weight:700}}
header .meta{{font-size:.75rem;color:rgba(255,255,255,.38);margin-top:.2rem}}
.dla{{background:#F2C94C;color:#0d1f2d;border:none;border-radius:7px;padding:.55rem 1.2rem;
  font-weight:700;font-size:.88rem;cursor:pointer;text-decoration:none;display:inline-block;white-space:nowrap}}
.dla:disabled{{opacity:.6;cursor:default}}
.dla.alt{{background:rgba(255,255,255,.1);color:#fff}}
.acts{{display:flex;gap:.5rem;flex-wrap:wrap}}
.wrap{{max-width:1400px;margin:0 auto;padding:1.25rem 1rem}}
.lic{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px;
  padding:.7rem 1rem;margin-bottom:1.1rem;font-size:.76rem;color:rgba(255,255,255,.42);line-height:1.8}}
.lic strong{{color:rgba(255,255,255,.68)}}
.grid{{position:relative;min-height:100px}}
.pi{{position:absolute;overflow:hidden;border-radius:6px;background:#111;cursor:pointer;transition:opacity .15s}}
.pi:hover{{opacity:.88}}
.pi img{{width:100%;height:auto;display:block}}
.pi:hover img{{opacity:.78}}
.dib{{position:absolute;bottom:7px;right:7px;background:#F2C94C;color:#0d1f2d;border-radius:5px;
  padding:.3rem .7rem;font-size:.75rem;font-weight:700;text-decoration:none;
  opacity:0;transition:opacity .15s;display:flex;align-items:center;gap:.3rem}}
.pi:hover .dib{{opacity:1}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:999;display:none;
  flex-direction:column;align-items:center;justify-content:center}}
#lb.open{{display:flex}}
#lb-img{{max-width:92vw;max-height:84vh;object-fit:contain;border-radius:4px;cursor:default}}
#lb-bar{{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;justify-content:flex-end;
  padding:.75rem 1rem;gap:.65rem;background:linear-gradient(rgba(0,0,0,.5),transparent)}}
#lb-dl{{background:#F2C94C;color:#0d1f2d;border:none;border-radius:6px;padding:.38rem .9rem;
  font-weight:700;font-size:.82rem;cursor:pointer;text-decoration:none}}
#lb-close{{background:rgba(255,255,255,.12);border:none;color:#fff;font-size:1.1rem;cursor:pointer;
  border-radius:6px;padding:.35rem .7rem;opacity:.8}}
#lb-close:hover{{opacity:1}}
</style></head>
<body>
<header>
  <div>
    <h1>{title_esc}</h1>
    <div class="meta">{len(paths)} photo{'s' if len(paths)!=1 else ''} &nbsp;·&nbsp; Link expires {expire_str}</div>
  </div>
  <div class="acts">
    <button id="save-photos" class="dla" style="display:none" onclick="savePhotosClick()">Save to Photos</button>
    <a href="/package/{token}/all" download class="dla" id="dl-all">⬇ Download All ({len(paths)} photos)</a>
  </div>
  <div id="save-hint" class="meta" style="display:none;width:100%"></div>
</header>
<div class="wrap">
  <div class="lic">
    All photos are for <strong>personal use</strong> — print, frame, and share freely.
    For commercial use, tag <strong>@crystalimagesbigsky</strong> on Instagram.
    Please do not resell or redistribute the original files.
    Questions? <a href="mailto:info@bigskyphotos.com" style="color:#F2C94C">info@bigskyphotos.com</a>
  </div>
  <div class="grid" id="masonry-grid">{photo_items}</div>
</div>
<div id="lb">
  <div id="lb-bar">
    <button id="lb-save" class="dla" style="display:none;padding:.38rem .9rem;font-size:.82rem" onclick="lbSaveClick()">Save to Photos</button>
    <a id="lb-dl" href="#" download>⬇ Download</a>
    <button id="lb-close" onclick="closeLb()">✕</button>
  </div>
  <img id="lb-img" src="" alt="">
</div>
<script>
// Masonry: place each item into the shortest column so portrait/landscape alternate naturally
function runMasonry() {{
  const grid = document.getElementById('masonry-grid');
  const items = [...grid.children];
  if (!items.length) return;
  const gap = 8;
  const W = grid.offsetWidth;
  const cols = W < 500 ? 2 : W < 780 ? 3 : W < 1100 ? 4 : 5;
  const colW = (W - gap * (cols - 1)) / cols;
  const heights = new Array(cols).fill(0);
  grid.style.position = 'relative';
  items.forEach(item => {{
    item.style.width = colW + 'px';
    const c = heights.indexOf(Math.min(...heights));
    item.style.left = (c * (colW + gap)) + 'px';
    item.style.top = heights[c] + 'px';
    heights[c] += item.offsetHeight + gap;
  }});
  grid.style.height = Math.max(...heights) + 'px';
}}
let _masonryTimer;
function scheduleMasonry() {{ clearTimeout(_masonryTimer); _masonryTimer = setTimeout(runMasonry, 60); }}
// Run once each image loads so layout fills in progressively, then again on resize
document.querySelectorAll('#masonry-grid img').forEach(img => {{
  if (img.complete) scheduleMasonry();
  else img.addEventListener('load', scheduleMasonry);
}});
window.addEventListener('resize', scheduleMasonry);

let lbIdx = 0;
document.querySelectorAll('.pi img').forEach((img,i)=>{{
  img.parentElement.addEventListener('click',e=>{{
    if(e.target.tagName==='A')return;
    lbIdx = i;
    document.getElementById('lb-img').src='/package/{token}/medium/'+i;
    document.getElementById('lb-dl').href='/package/{token}/file/'+i;
    document.getElementById('lb').classList.add('open');
    prepareLbSave(i);
  }});
}});
function closeLb(){{document.getElementById('lb').classList.remove('open')}}

// ── Save to Photos ──
// Phones put normal downloads in the Files app, where many people never find them.
// The share sheet's "Save Images" puts them straight into Photos. Sharing has to
// start from a tap, so photos are fetched first, then a second tap opens the sheet.
const IS_MOBILE = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const SAVE_OK = IS_MOBILE && (() => {{
  try {{
    const f = new File([new Blob(['x'], {{type: 'image/jpeg'}})], 't.jpg', {{type: 'image/jpeg'}});
    return !!(navigator.canShare && navigator.canShare({{files: [f]}}));
  }} catch(e) {{ return false; }}
}})();
const BATCH = 10;          // photos per share sheet: keeps memory use reasonable on phones
let _links = null, _next = 0, _ready = null, _busy = false;
const $id = id => document.getElementById(id);

async function getLinks() {{
  if (!_links) {{
    const r = await fetch('/package/{token}/links');
    if (!r.ok) throw new Error('This link has expired');
    _links = (await r.json()).files;
  }}
  return _links;
}}

async function fetchFile(link) {{
  const r = await fetch(link.url);
  if (!r.ok) throw new Error('Could not load ' + link.name);
  return new File([await r.blob()], link.name, {{type: 'image/jpeg'}});
}}

async function fetchAll(links, onEach) {{
  const out = new Array(links.length);
  let i = 0, done = 0;
  async function worker() {{
    while (i < links.length) {{
      const k = i++;
      out[k] = await fetchFile(links[k]);
      onEach(++done);
    }}
  }}
  await Promise.all([worker(), worker(), worker(), worker()]);
  return out;
}}

function setSave(label, disabled, hint) {{
  const b = $id('save-photos');
  b.textContent = label; b.disabled = !!disabled;
  $id('save-hint').style.display = hint ? '' : 'none';
  $id('save-hint').textContent = hint || '';
}}

async function savePhotosClick() {{
  if (_busy) return;
  if (_ready) {{                                    // second tap: open the share sheet
    const files = _ready;
    try {{
      await navigator.share({{files}});
      _ready = null; _next += files.length;
      const left = _links.length - _next;
      if (left > 0) setSave(`Save next ${{Math.min(BATCH, left)}} photo${{left === 1 ? '' : 's'}}`, false,
                            `${{_next}} of ${{_links.length}} saved`);
      else setSave('All photos saved', true, 'They are in your Photos app.');
    }} catch(e) {{
      if (e.name !== 'AbortError') setSave('Save to Photos', false, "Saving didn't work here. Use Download All instead.");
    }}
    return;
  }}
  _busy = true;                                     // first tap: fetch the next batch
  try {{
    const links = (await getLinks()).slice(_next, _next + BATCH);
    setSave(`Preparing 0 of ${{links.length}}...`, true);
    _ready = await fetchAll(links, n => setSave(`Preparing ${{n}} of ${{links.length}}...`, true));
    setSave(`Tap to save ${{_ready.length}} photo${{_ready.length === 1 ? '' : 's'}}`, false,
            'Then choose "Save Images" (or "Save Image").');
  }} catch(e) {{
    setSave('Save to Photos', false, e.message + '. Please try again.');
  }} finally {{ _busy = false; }}
}}

// Single photo from the enlarged view
let _lbFile = null, _lbFor = -1;
async function prepareLbSave(i) {{
  if (!SAVE_OK) return;
  const b = $id('lb-save');
  b.style.display = ''; b.disabled = true; b.textContent = 'Preparing...';
  _lbFile = null; _lbFor = i;
  try {{
    const f = await fetchFile((await getLinks())[i]);
    if (_lbFor !== i) return;                       // moved on to another photo
    _lbFile = f; b.disabled = false; b.textContent = 'Save to Photos';
  }} catch(e) {{ b.textContent = 'Unavailable'; }}
}}
async function lbSaveClick() {{
  if (!_lbFile) return;
  try {{ await navigator.share({{files: [_lbFile]}}); }} catch(e) {{}}
}}

if (SAVE_OK) {{
  $id('save-photos').style.display = '';
  $id('dl-all').classList.add('alt');
  $id('dl-all').textContent = 'Download as zip';
}}
document.getElementById('lb').addEventListener('click',e=>{{if(e.target.id==='lb'||e.target.id==='lb-img')closeLb()}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeLb()}});
</script>
</body></html>""")


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
