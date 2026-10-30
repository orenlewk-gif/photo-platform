"""Routes: admin_photos. Moved from main.py unchanged; shared helpers still live in main.py."""
import json
import os
import re
import threading
import uuid
import zipfile
from PIL import Image
from core.images import fix_orientation
from core.images import nowm_thumb_r2_key as _nowm_thumb_r2_key
from core.images import thumb_r2_key as _thumb_r2_key
from core.pricing import ZIP_LOCS_SET
from core.search import merged_colors
from core.text import clean_location
from core.text import folder_key as _folder_key
from core.text import natural_sort_key
from core.text import to_r2_key
from fastapi import Body
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from io import BytesIO

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/admin/color-labels")
async def admin_color_labels(request: Request, date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return per-photo color labels for a folder. Admin only."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    results = []
    for item in main.data:
        if item.get("draft"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        results.append({
            "file": item["path"].split("/")[-1],
            "jacket": merged_colors(item.get("jacket_colors")),
            "pants":  merged_colors(item.get("pants_colors")),
        })
    return {"count": len(results), "photos": results}


@router.get("/api/admin/folder-photos")
async def admin_folder_photos(request: Request, date: str = Query(""), location: str = Query(""), group: str = Query("")):
    """Return photo paths and thumbnail URLs for a folder. Admin only."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower = location.strip().lower()
    grp_lower = group.strip().lower()
    results = []
    for item in main.data:
        if item.get("draft"):
            continue
        if date and item.get("date") != date:
            continue
        if loc_lower and clean_location(item.get("location", "")).lower() != loc_lower:
            continue
        if grp_lower and (item.get("group", "") or item.get("last_name", "")).strip().lower() != grp_lower:
            continue
        results.append({
            "path":       item["path"],
            "file":       item["path"].split("/")[-1],
            "jacket":     merged_colors(item.get("jacket_colors")),     # camo shows (and saves) as green
            "pants":      merged_colors(item.get("pants_colors")),
            "sport":      item.get("sport"),
            "instructor": bool(item.get("ski_instructor")),
            "partner":    item.get("partner", ""),
        })
    results.sort(key=lambda x: natural_sort_key(x["file"]))
    return {"photos": results}


@router.post("/api/admin/set-photos-partner")
def admin_set_photos_partner(request: Request, body: dict = Body(...)):
    """Studio: mark photos as a partner's (Eagle Mount), or take the mark off (partner "").
    For photos missed at upload. Marked photos leave the public gallery and appear on the
    partner's page; unmarked ones go back to the gallery. Portraits are never partner photos."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    partner = str(body.get("partner") or "")
    if partner and partner not in main._partner_ids():
        return JSONResponse(status_code=400, content={"error": "Unknown partner"})
    wanted = {p for p in (body.get("paths") or []) if isinstance(p, str)}
    changed, skipped = [], 0
    for item in main.data:
        if item["path"] not in wanted:
            continue
        if partner and main._is_portrait_location(item.get("location", "")):
            skipped += 1
            continue
        if (item.get("partner") or "") == partner:
            continue
        if partner:
            item["partner"] = partner
        else:
            item.pop("partner", None)
        changed.append(item)
    if changed:
        main._photo_list_changed()          # the gallery and the partner page change right away
        main._save_photo_list_soon()        # the whole list is saved in the background
        if partner:
            threading.Thread(target=main._prepare_partner_photos, args=(changed,), daemon=True).start()
    return {"updated": len(changed), "skipped_portraits": skipped, "paths": [i["path"] for i in changed]}


@router.post("/api/admin/reclassify-photos")
async def admin_reclassify_photos(request: Request):
    """Clear and re-run the labeling model on a specific list of photo paths."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body  = await request.json()
    paths = body.get("paths", [])
    if not paths:
        return JSONResponse(status_code=400, content={"error": "No paths provided"})

    path_set = set(paths)
    targets  = [it for it in main.data if it["path"] in path_set]
    if not targets:
        return JSONResponse(status_code=404, content={"error": "No matching photos found"})

    for it in targets:
        it["sport"]         = None
        it["jacket_colors"] = None
        it["pants_colors"]  = None

    job_id = str(uuid.uuid4())[:8]
    main._embed_jobs[job_id] = {"total": len(targets), "done": 0, "finished": False}

    def _bg(jid=job_id):
        main._label_photos(targets, jid)
        main._save_photo_list()
        if jid in main._embed_jobs:
            main._embed_jobs[jid]["finished"] = True

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(targets), "job_id": job_id}


@router.post("/api/admin/set-photo-colors")
async def admin_set_photo_colors(request: Request):
    """Manually set jacket/pants/sport labels on a single photo."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        return JSONResponse(status_code=400, content={"error": "path required"})
    item = next((it for it in main.data if it["path"] == path), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Photo not found"})
    if "jacket_colors" in body: item["jacket_colors"] = merged_colors(body["jacket_colors"] or [])
    if "pants_colors"  in body: item["pants_colors"]  = merged_colors(body["pants_colors"]  or [])
    if "sport"         in body: item["sport"]          = body["sport"] or None
    if "ski_instructor"  in body: item["ski_instructor"]  = bool(body["ski_instructor"])
    item["manually_corrected"] = True
    main._save_photo_list_soon()
    return {"ok": True, "path": path}


@router.post("/api/admin/set-photos-colors-bulk")
async def admin_set_photos_colors_bulk(request: Request):
    """Set jacket/pants/sport labels on multiple photos in one R2 write."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    paths = body.get("paths") or []
    if not paths:
        return JSONResponse(status_code=400, content={"error": "paths required"})
    path_index = {it["path"]: it for it in main.data}
    updated = []
    for path in paths:
        item = path_index.get(path.strip())
        if not item:
            continue
        if "jacket_colors" in body: item["jacket_colors"] = merged_colors(body["jacket_colors"] or [])
        if "pants_colors"  in body: item["pants_colors"]  = merged_colors(body["pants_colors"]  or [])
        if "sport"         in body: item["sport"]          = body["sport"] or None
        if "ski_instructor" in body: item["ski_instructor"] = bool(body["ski_instructor"])
        item["manually_corrected"] = True
        updated.append(path)
    if updated:
        main._save_photo_list_soon()
    return {"ok": True, "updated": len(updated)}


@router.get("/api/admin/photo")
def admin_get_photo(request: Request, path: str, size: str = Query("thumb")):
    """Admin-only photo endpoint — no watermark. Caches to admin_thumbs/ in R2."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from fastapi.responses import StreamingResponse as SR
    SIZE_MAP = {"thumb": (450, 72), "medium": (1800, 90)}
    max_px, quality = SIZE_MAP.get(size, SIZE_MAP["thumb"])
    try:
        key = to_r2_key(path)
        if size == "thumb" and os.getenv("R2_ENDPOINT_URL"):
            data = main._cached_thumb(key, "admin") or main._thumb_on_demand(key, "admin")
            return SR(BytesIO(data), media_type="image/jpeg",
                      headers={"Cache-Control": "private, max-age=86400"})
        obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=key)
        img = Image.open(obj["Body"]).convert("RGB")
        img = fix_orientation(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        buf.seek(0)
        return SR(buf, media_type="image/jpeg",
                  headers={"Cache-Control": "private, max-age=86400"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/admin/download-folder")
def admin_download_folder(
    request: Request,
    date: str = Query(...),
    location: str = Query(...),
    family: str = Query(None),
    group: str = Query(None),
):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    all_data = main._r2_json_load("images.json", [])
    loc_lower = location.strip().lower()
    sub = (family or group or '').strip().lower()
    field = "last_name" if family else "group"
    matches = []
    for item in all_data:
        if item.get("date") != date:
            continue
        if (item.get("location") or "").strip().lower() != loc_lower:
            continue
        if sub and (item.get(field) or "").strip().lower() != sub:
            continue
        path = item.get("path", "")
        if path:
            matches.append(path)
    if not matches:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for path in matches:
            idx = path.find("images/")
            r2_key = path[idx:].replace("\\", "/") if idx >= 0 else path
            try:
                obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key=r2_key)
                zf.writestr(os.path.basename(r2_key), obj["Body"].read())
            except Exception:
                pass
    buf.seek(0)
    label = "_".join(filter(None, [date, location.replace(" ", "_"), family or group]))
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{label}.zip"'},
    )


@router.get("/api/admin/text-presets")
async def get_text_presets(request: Request):
    if not main._admin_authed(request):
        raise HTTPException(403)
    return {"presets": main._load_text_presets()}


@router.post("/api/admin/text-preset")
async def save_text_preset(request: Request):
    if not main._admin_authed(request):
        raise HTTPException(403)
    body = await request.json()
    preset_id   = body.get("id") or ""
    name        = (body.get("name") or "").strip()
    text        = (body.get("text") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    presets = main._load_text_presets()
    existing = next((p for p in presets if p.get("id") == preset_id), None)
    if existing:
        existing["name"] = name
        existing["text"] = text
    else:
        import uuid
        presets.append({"id": str(uuid.uuid4())[:8], "name": name, "text": text})
    main._save_text_presets(presets)
    return {"ok": True, "presets": presets}


@router.delete("/api/admin/text-preset/{preset_id}")
async def delete_text_preset(preset_id: str, request: Request):
    if not main._admin_authed(request):
        raise HTTPException(403)
    presets = [p for p in main._load_text_presets() if p.get("id") != preset_id]
    main._save_text_presets(presets)
    return {"ok": True}


@router.post("/api/admin/text-preset/bulk-apply")
async def bulk_apply_text_preset(request: Request):
    """Apply a preset's text as description to one or more folder keys."""
    if not main._admin_authed(request):
        raise HTTPException(403)
    body = await request.json()
    text        = (body.get("text") or "").strip()
    folder_keys = body.get("folder_keys") or []
    if not folder_keys:
        raise HTTPException(400, "folder_keys required")
    fm = main._load_folder_meta()
    for fk in folder_keys:
        if fk not in fm:
            fm[fk] = {}
        fm[fk]["description"] = text
    main._save_folder_meta(fm)
    return {"ok": True, "updated": len(folder_keys)}


@router.post("/api/admin/folder/set-item-list")
async def api_set_folder_item_list(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    item_list = body.get("item_list", "")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    folder_meta = main._load_folder_meta()
    if fk not in folder_meta:
        folder_meta[fk] = {}
    folder_meta[fk]["item_list"] = item_list
    main._save_folder_meta(folder_meta)
    return {"status": "ok"}


# Studio uploads: only photo files, and the date, location and family folder name can only
# name a folder (no slashes), so every upload lands in images/<date>/<location>[/<family>]/.
# Winter action photos ignore the folder (the location is chosen at upload); portrait families
# each get their own folder. File names are kept exactly as they are: poses are matched by them.
UPLOAD_EXTENSIONS = (".jpg", ".jpeg", ".png")
_UPLOAD_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _upload_names_problem(date: str, location: str, folder: str) -> str:
    if not _UPLOAD_DATE_RE.fullmatch(date or ""):
        return "The date isn't valid"
    for value, what in ((location, "Location"), (folder, "Folder name")):
        if "/" in value or "\\" in value or value.strip() in (".", "..") or any(ord(c) < 32 for c in value):
            return f"{what} can't contain slashes"
    return ""


def _upload_prefix(date: str, location: str, folder: str) -> str:
    """Where a studio upload's photos go (same slugs the studio has always used)."""
    loc_slug = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    return f"images/{date}/{loc_slug}/{folder_slug}/" if folder_slug else f"images/{date}/{loc_slug}/"


def _is_upload_photo(name: str) -> bool:
    return name.lower().endswith(UPLOAD_EXTENSIONS)


@router.post("/api/admin/upload/presign")
async def admin_upload_presign(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    files    = body.get("files", [])
    if not date or not location or not files:
        return JSONResponse(status_code=400, content={"error": "Missing date, location, or files"})
    is_portrait = main._is_portrait_location(location)
    folder      = body.get("folder", "").strip() if is_portrait else ""
    problem = _upload_names_problem(date, location, folder)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    refused = [os.path.basename(str(f.get("name") or "")) for f in files
               if not _is_upload_photo(os.path.basename(str(f.get("name") or "")))
               or not str(f.get("type") or "image/jpeg").lower().startswith("image/")
               or "svg" in str(f.get("type") or "").lower()]
    if refused:
        return JSONResponse(status_code=400, content={
            "error": "Only photos (JPG or PNG) can be uploaded: " + ", ".join(refused[:5])})
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    on_site = {item["path"] for item in main.data}       # dropped again after a failed upload: these are in
    urls = []
    for f in files:
        filename = os.path.basename(f["name"])
        if folder_slug:
            key = f"images/{date}/{loc_slug}/{folder_slug}/{filename}"
        else:
            key = f"images/{date}/{loc_slug}/{filename}"
        presigned = main.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": main.R2_BUCKET, "Key": key, "ContentType": f.get("type", "image/jpeg")},
            ExpiresIn=7200,
        )
        entry = {"key": key, "url": presigned, "filename": filename, "on_site": key in on_site}
        if main.s3_alt_host is not None:          # the same upload on storage's second address
            entry["url_alt"] = main.s3_alt_host.generate_presigned_url(
                "put_object", Params={"Bucket": main.R2_BUCKET, "Key": key, "ContentType": f.get("type", "image/jpeg")},
                ExpiresIn=7200)
        if entry["on_site"] and isinstance(f.get("size"), int):
            # The same name isn't always the same photo (two cameras numbering alike, two computers
            # uploading the same day): a different size means a different photo
            try:
                stored = main.s3.head_object(Bucket=main.R2_BUCKET, Key=key)["ContentLength"]
            except Exception:
                stored = None
            if stored is not None and stored != f["size"]:
                entry["on_site"], entry["name_taken"] = False, True
        urls.append(entry)
    return {"urls": urls}


@router.post("/api/admin/upload/index")
async def admin_upload_index(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    keys     = body.get("keys", [])
    poses    = body.get("poses", None)  # from UI pose assignment; None means use XMP detection
    times    = body.get("times", {})   # {filename: minutes_since_midnight} from client EXIF read
    if not date or not location or not keys:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    is_portrait = main._is_portrait_location(location)
    folder   = body.get("folder", "").strip() if is_portrait else ""
    problem = _upload_names_problem(date, location, folder)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    # Only the photos just uploaded to this folder can be added to the photo list
    prefix = _upload_prefix(date, location, folder)
    if any(not isinstance(k, str) or not k.startswith(prefix) or "/" in k[len(prefix):]
           or not _is_upload_photo(k) for k in keys):
        return JSONResponse(status_code=400, content={"error": "Those files don't belong to this upload"})
    # Winter action photos the photographer color-labeled for a partner (read from the files
    # by the upload page): {partner id: [file names]}. Portrait colors are poses, never partners.
    partner_of = {}
    if not is_portrait:
        known = main._partner_ids()
        for pid, names in (body.get("partner_files") or {}).items():
            if pid in known and isinstance(names, list):
                partner_of.update({str(n): pid for n in names})
    existing    = {item["path"] for item in main.data}
    added       = 0
    location = clean_location(location)
    for key in keys:
        if key not in existing:
            entry = {
                "path":      key,
                "date":      date,
                "location":  location,
                "last_name": folder if is_portrait else "",
                "group":     "" if is_portrait else folder,
            }
            if not is_portrait:
                entry["draft"] = True
            if os.path.basename(key) in partner_of:
                entry["partner"] = partner_of[os.path.basename(key)]
            main.data.append(entry)
            existing.add(key)
            added += 1
    # Thumbnails and color labels for these photos start right away (the next photos are still
    # uploading); the photo list is saved a moment later in the background, one save for several
    # batches, instead of each batch waiting on a save of the whole list. Push-live saves it too.
    thumbs_job = main._queue_new_photo_thumbs(keys)
    main._save_photo_list_soon()
    if is_portrait and folder and keys:
        if poses is not None:
            main._write_folder_poses_from_ui(date, location, folder, poses)
        else:
            await run_in_threadpool(main._update_folder_poses, date, location, folder, keys)
    if times:
        # Only persist times for locations that have time_search enabled
        _pricing = main._load_pricing()
        _loc_cfg = _pricing.get("activities", {}).get(location, {})
        if _loc_cfg.get("flags", {}).get("time_search"):
            cache_key = (date, location.strip().lower())
            merged = dict(main._folder_times_cache.get(cache_key) or {})
            merged.update(times)
            main._folder_times_cache[cache_key] = merged
            try:
                main.s3.put_object(Bucket=main.R2_BUCKET, Key=main._times_r2_key(date, location),
                              Body=json.dumps(merged).encode(), ContentType="application/json")
            except Exception:
                pass
    # Partner photos: capture times (their page shows them in the order taken) and the large
    # version for the page's enlarged view, made now in the background
    keyset = set(keys)
    marked = [i for i in main.data if i.get("partner") and i["path"] in keyset]
    if marked:
        threading.Thread(target=main._prepare_partner_photos, args=(marked,), daemon=True).start()
    return {"indexed": added, "date": date, "location": location, "folder": folder,
            "thumbs_job_id": thumbs_job, "partner_marked": sum(1 for k in keys if os.path.basename(k) in partner_of)}


@router.post("/api/admin/reindex-folder")
async def admin_reindex_folder(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "Missing date or location"})
    location    = clean_location(location)
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-") if folder else ""
    prefix = f"images/{date}/{loc_slug}/{folder_slug}/" if folder_slug else f"images/{date}/{loc_slug}/"
    is_portrait = location.lower() in main.PORTRAIT_LOCATIONS
    existing = {item["path"] for item in main.data}
    added = 0
    added_keys = []
    paginator = main.s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=main.R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            if key not in existing:
                main.data.append({
                    "path":      key,
                    "date":      date,
                    "location":  location,
                    "last_name": folder if is_portrait else "",
                    "group":     "" if is_portrait else folder,
                    "draft":     True,
                })
                existing.add(key)
                added_keys.append(key)
                added += 1
    if added:
        main._save_photo_list()
    if is_portrait and folder:
        all_keys = [
            obj["Key"]
            for page in main.s3.get_paginator("list_objects_v2").paginate(Bucket=main.R2_BUCKET, Prefix=prefix)
            for obj in page.get("Contents", [])
            if obj["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
        if all_keys:
            main._update_folder_poses(date, location, folder, all_keys)
    thumbs_job = main._queue_new_photo_thumbs(added_keys)
    return {"indexed": added, "prefix": prefix, "thumbs_job_id": thumbs_job}


@router.post("/api/admin/push-live")
async def admin_push_live(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()

    # ── Pricing gate ─────────────────────────────────────────────────────────
    loc_lower = location.lower()
    if loc_lower in ZIP_LOCS_SET:
        fm = main._load_folder_meta()
        fk = _folder_key(date, location, folder)
        group_size = fm.get(fk, {}).get("group_size")
        if not group_size:
            return JSONResponse(status_code=400, content={"error": "Set group size before pushing live"})
        zp = main._load_zip_pricing()
        if not any(str(t.get("people")) == str(group_size) for t in zp.get("tiers", [])):
            return JSONResponse(status_code=400, content={"error": f"No zip pricing for {group_size} people — add it in Pricing"})
    else:
        _pr = main._load_pricing()
        has_pricing = bool(_pr.get("default", {}).get("tiers")) or \
                      bool(_pr.get("activities", {}).get(location, {}).get("tiers"))
        if not has_pricing:
            return JSONResponse(status_code=400, content={"error": f"No pricing configured for {location} — set it up in Pricing"})

    pushed   = 0
    to_embed = []
    for item in main.data:
        if (item.get("draft")
                and item["date"] == date
                and item["location"].strip().lower() == location.lower()
                and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()):
            del item["draft"]
            to_embed.append(item)
            pushed += 1

    if pushed:
        await run_in_threadpool(main._save_photo_list)

    job_id = None
    if to_embed:
        job_id = str(uuid.uuid4())[:8]
        main._embed_jobs[job_id] = {"total": len(to_embed), "done": 0, "finished": False}
        def _embed_bg(jid=job_id):
            count = main.embed_items_fast(to_embed, job_id=jid)
            main._save_photo_list()
            if jid in main._embed_jobs:
                main._embed_jobs[jid]["finished"] = True
            print(f"Push-live classify done: {count}/{len(to_embed)}")
        threading.Thread(target=_embed_bg, daemon=True).start()

    return {"pushed": pushed, "job_id": job_id, "classify_total": len(to_embed)}


@router.post("/api/admin/thumbs/pos")
def admin_pos_thumbs(request: Request, body: dict = Body(...)):
    """Make missing POS thumbnails for photos in a date range (e.g. the winter photos),
    newest dates first. Safe to run again: photos that already have one are skipped."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    date_from = (body.get("date_from") or "").strip()
    date_to   = (body.get("date_to") or "").strip()
    if not date_to:
        return JSONResponse(status_code=400, content={"error": "Choose the last date to include"})
    running = main._embed_jobs.get(main._pos_thumbs_job["job_id"] or "")
    if running and not running.get("finished"):
        return {"job_id": main._pos_thumbs_job["job_id"], "already_running": True}
    have = main._r2_keys_under(_nowm_thumb_r2_key("images/"))
    photos = [it for it in main.data
              if not it.get("draft")
              and (not date_from or (it.get("date") or "") >= date_from)
              and (it.get("date") or "") <= date_to]
    work = []
    for item in sorted(photos, key=lambda it: it.get("date") or "", reverse=True):
        key = to_r2_key(item.get("path") or "")
        if key.startswith("images/") and _nowm_thumb_r2_key(key) not in have:
            work.append((key, ["pos"]))
    job_id = main._start_thumb_job(work)
    main._pos_thumbs_job["job_id"] = job_id
    return {"job_id": job_id, "queued": len(work), "in_range": len(photos)}


@router.get("/api/admin/embed-status")
def admin_embed_status(request: Request, job_id: str = Query(...)):
    """Return progress for a background labeling / thumbnail job (studio only)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    job = main._embed_jobs.get(job_id)
    if not job:
        return {"done": 0, "total": 0, "finished": True}
    return {"done": job["done"], "total": job["total"], "finished": job.get("finished", False),
            "failed": job.get("failed", 0)}


@router.post("/api/admin/classify-backfill")
async def admin_classify_backfill(request: Request):
    """Labeling backfill: label sport and jacket/pants colors (labeling model) for photos
    that have no jacket_colors label yet.
    Optionally filter by location (e.g. 'Whitefish Mountain') to process only
    specific resort folders instead of the entire library."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body             = await request.json()
    location_filter  = (body.get("location") or "").strip().lower()
    batch_limit      = int(body.get("limit") or 0)  # 0 = no limit

    _portrait_locs_lower = {loc.strip().lower() for loc in main.PORTRAIT_LOCATIONS}
    candidates = [
        it for it in main.data
        if not it.get("draft")
        and it.get("jacket_colors") is None
        and it.get("location", "").strip().lower() not in _portrait_locs_lower
        and (not location_filter or location_filter in it.get("location", "").lower())
    ]
    to_classify = candidates[:batch_limit] if batch_limit > 0 else candidates
    remaining   = max(0, len(candidates) - len(to_classify))

    if not to_classify:
        return {"queued": 0, "message": "All matching photos already classified", "remaining": 0}

    job_id = str(uuid.uuid4())[:8]
    main._embed_jobs[job_id] = {"total": len(to_classify), "done": 0, "finished": False}

    def _bg(jid=job_id):
        # Saves every 16 photos so partial results survive a restart
        done = main._label_photos(to_classify, jid, save_every=16)
        main._save_photo_list()
        if jid in main._embed_jobs:
            main._embed_jobs[jid]["finished"] = True
        print(f"Classify backfill done: {done}/{len(to_classify)}")

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(to_classify), "remaining": remaining, "job_id": job_id}


@router.post("/api/admin/classify-folder")
async def admin_classify_folder(request: Request):
    """Run the labeling model on a specific folder (date + location + folder name).
    Processes only photos in that folder that don't yet have color labels."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._load_onnx_model():
        return JSONResponse(status_code=503, content={"error": "The labeling model isn't available"})
    body     = await request.json()
    date     = (body.get("date") or "").strip()
    location = (body.get("location") or "").strip().lower()
    folder   = (body.get("folder") or "").strip().lower()

    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "date and location required"})

    to_classify = [
        it for it in main.data
        if not it.get("draft")
        and it.get("jacket_colors") is None
        and it["date"] == date
        and it.get("location", "").strip().lower() == location
        and (not folder or (it.get("last_name","") or it.get("group","")).strip().lower() == folder)
    ]
    if not to_classify:
        return {"queued": 0, "message": "All photos in this folder already classified"}

    job_id = str(uuid.uuid4())[:8]
    main._embed_jobs[job_id] = {"total": len(to_classify), "done": 0, "finished": False}

    def _bg(jid=job_id):
        done = main._label_photos(to_classify, jid)
        main._save_photo_list()
        if jid in main._embed_jobs:
            main._embed_jobs[jid]["finished"] = True
        print(f"Classify folder done: {done}/{len(to_classify)}")

    threading.Thread(target=_bg, daemon=True).start()
    return {"queued": len(to_classify), "job_id": job_id}


@router.post("/api/admin/discard-draft")
async def admin_discard_draft(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    before   = len(main.data)
    main.data = [item for item in main.data if not (
        item.get("draft")
        and item["date"] == date
        and item["location"].strip().lower() == location.lower()
        and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()
    )]
    removed = before - len(main.data)
    if removed:
        main._save_photo_list()
    return {"removed": removed}


@router.post("/api/admin/delete-folder")
async def admin_delete_folder(request: Request):
    """Move all photos in a folder to trash (7-day recovery window)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()

    def _matches(item):
        return (
            item["date"] == date
            and item["location"].strip().lower() == location.lower()
            and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()
        )

    to_trash = [item for item in main.data if _matches(item)]
    if not to_trash:
        return {"deleted": 0, "removed_index": 0}

    # Remove from index immediately so the response is instant
    count = len(to_trash)
    main.data = [item for item in main.data if not _matches(item)]
    main._save_photo_list()
    try:
        fm = main._load_folder_meta()
        fk = _folder_key(date, location, folder)
        if fk in fm:
            del fm[fk]
            main._save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    # Copy to trash in background so we don't block the HTTP response
    def _bg():
        new_meta, trashed = main._bulk_trash(to_trash)
        if trashed:
            main._add_to_trash_meta(new_meta)
        print(f"[delete-folder] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count, "removed_index": count}


@router.post("/api/admin/delete-location")
async def admin_delete_location(request: Request):
    """Move all photos at a date+location to trash (7-day recovery)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()

    def _matches(item):
        return (
            item["date"] == date
            and item["location"].strip().lower() == location.lower()
        )

    to_trash = [item for item in main.data if _matches(item)]
    if not to_trash:
        return {"deleted": 0}

    count = len(to_trash)
    main.data = [item for item in main.data if not _matches(item)]
    main._save_photo_list()
    try:
        fm = main._load_folder_meta()
        keys_to_del = [k for k in fm if k.startswith(f"{date}|{location}|") or k == f"{date}|{location}|"]
        for k in keys_to_del:
            del fm[k]
        if keys_to_del:
            main._save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    def _bg():
        new_meta, trashed = main._bulk_trash(to_trash)
        if trashed:
            main._add_to_trash_meta(new_meta)
        print(f"[delete-location] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count}


@router.post("/api/admin/delete-date")
async def admin_delete_date(request: Request):
    """Move all photos on a date to trash (7-day recovery)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    date = body.get("date", "")

    to_trash = [item for item in main.data if item.get("date", "") == date]
    if not to_trash:
        return {"deleted": 0}

    count = len(to_trash)
    main.data = [item for item in main.data if item.get("date", "") != date]
    main._save_photo_list()
    try:
        fm = main._load_folder_meta()
        keys_to_del = [k for k in fm if k.startswith(f"{date}|")]
        for k in keys_to_del:
            del fm[k]
        if keys_to_del:
            main._save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta cleanup error: {e}")

    def _bg():
        new_meta, trashed = main._bulk_trash(to_trash)
        if trashed:
            main._add_to_trash_meta(new_meta)
        print(f"[delete-date] bg trash done: {trashed}/{count}")
    threading.Thread(target=_bg, daemon=True).start()

    return {"deleted": count}


@router.post("/api/admin/rename-activity")
async def rename_activity(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    old_name = body.get("old_name", "").strip()
    new_name = body.get("new_name", "").strip()
    if not old_name or not new_name:
        return JSONResponse(status_code=400, content={"error": "Both old_name and new_name are required"})
    if old_name == new_name:
        return {"renamed": True, "photos_updated": 0}

    # Update location on every matching photo in memory
    updated = 0
    for item in main.data:
        if item.get("location", "").strip() == old_name:
            item["location"] = new_name
            updated += 1

    # Normalize all locations and deduplicate by path to prevent merge duplicates
    for item in main.data:
        raw = item.get("location", "")
        if raw:
            item["location"] = clean_location(raw)
    seen_paths: set = set()
    deduped = []
    for item in main.data:
        path = item.get("path", "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        deduped.append(item)
    main.data = deduped

    main._save_photo_list()

    # Rename / merge the pricing config key and update category acts list
    pricing = main._load_pricing()
    acts    = pricing.get("activities", {})
    if old_name in acts:
        old_cfg = acts.pop(old_name)
        if new_name not in acts:
            acts[new_name] = old_cfg
    elif new_name not in acts:
        acts[new_name] = {}
    pricing["activities"] = acts
    for cat in pricing.get("activity_categories", []):
        cat_acts = cat.get("acts", [])
        if old_name in cat_acts:
            idx = cat_acts.index(old_name)
            if new_name not in cat_acts:
                cat_acts[idx] = new_name
            else:
                cat_acts.pop(idx)
    main._save_pricing(pricing)

    return {"renamed": True, "photos_updated": updated}


@router.post("/api/admin/normalize-locations")
async def normalize_locations(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Step 1: normalize all location fields to their clean display form
    for item in main.data:
        raw = item.get("location", "")
        if raw:
            item["location"] = clean_location(raw)

    # Step 2: dedup by exact R2 path (same file indexed twice)
    seen_paths: set = set()
    after_path: list = []
    for item in main.data:
        path = item.get("path", "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        after_path.append(item)

    # Step 3: dedup by (date, location, family/group, filename)
    # Catches the same photo stored under two different directory names in R2
    seen_keys: set = set()
    deduped: list = []
    for item in after_path:
        filename = os.path.basename(item.get("path", ""))
        key = (
            item.get("date", ""),
            item.get("location", ""),
            item.get("last_name", "") or item.get("group", ""),
            filename,
        )
        if filename and key in seen_keys:
            continue
        if filename:
            seen_keys.add(key)
        deduped.append(item)

    removed = len(main.data) - len(deduped)
    main.data = deduped
    main._save_photo_list()

    return {"normalized": True, "total": len(main.data), "duplicates_removed": removed}


@router.get("/api/admin/debug-data")
async def admin_debug_data(request: Request):
    """Temporary diagnostic: shows what's in memory vs R2."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    dates_in_memory = sorted({item.get("date", "NO_DATE") for item in main.data}, reverse=True)
    try:
        obj = main.s3.get_object(Bucket=main.R2_BUCKET, Key="images.json")
        r2_data = json.loads(obj["Body"].read())
        dates_in_r2 = sorted({item.get("date", "NO_DATE") for item in r2_data}, reverse=True)
        r2_count = len(r2_data)
    except Exception as e:
        dates_in_r2 = [f"error: {e}"]
        r2_count = -1
    return {
        "memory_count": len(main.data),
        "memory_dates": dates_in_memory,
        "r2_count": r2_count,
        "r2_dates": dates_in_r2,
        "sample_paths": [item.get("path", "") for item in main.data[:5]],
    }


@router.get("/api/admin/folders")
def api_admin_folders(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    folder_meta = main._load_folder_meta()
    folders = {}
    for item in main.data:
        date     = item.get("date", "")
        location = item.get("location", "").strip()
        name     = (item.get("last_name") or item.get("group") or "").strip()
        if not date or not location:
            continue
        fk = _folder_key(date, location, name)
        if fk not in folders:
            loc_lower = location.lower()
            is_zip = loc_lower in ZIP_LOCS_SET
            is_portrait = loc_lower in main.PORTRAIT_LOCATIONS
            folders[fk] = {
                "folder_key":       fk,
                "date":             date,
                "location":         location,
                "name":             name,
                "photo_count":      0,
                "draft_count":      0,
                "classified_count": 0,
                "is_zip":           is_zip,
                "is_portrait":      is_portrait,
                "group_size":       folder_meta.get(fk, {}).get("group_size") if is_zip else None,
                "_preview":         None,
            }
        folders[fk]["photo_count"] += 1
        if item.get("draft"):
            folders[fk]["draft_count"] += 1
        if item.get("jacket_colors") is not None:
            folders[fk]["classified_count"] += 1
        if folders[fk]["_preview"] is None:
            folders[fk]["_preview"] = item.get("path")
    from urllib.parse import quote as _quote
    result = []
    for f in sorted(folders.values(), key=lambda x: (x["date"], x["location"], x["name"]), reverse=True):
        preview_path = f.pop("_preview", None)
        f["preview_url"] = f"/api/admin/photo?path={_quote(preview_path)}&size=thumb" if preview_path else ""
        f["reviewed"] = bool(folder_meta.get(f["folder_key"], {}).get("reviewed"))
        result.append(f)
    return {"folders": result}


@router.get("/api/admin/model-accuracy")
def api_model_accuracy(request: Request, date_from: str = Query(""), date_to: str = Query("")):
    """How each labeling model did on real uploads, in folders marked done (Settings > Color Labels)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return main._model_accuracy(date_from.strip(), date_to.strip())


@router.post("/api/admin/toggle-folder-reviewed")
async def api_toggle_folder_reviewed(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk = body.get("folder_key")
    if not fk:
        return JSONResponse(status_code=400, content={"error": "folder_key required"})
    fm = main._load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["reviewed"] = not bool(fm[fk].get("reviewed"))
    main._save_folder_meta(fm)
    return {"ok": True, "reviewed": fm[fk]["reviewed"]}


@router.get("/api/admin/r2-scan")
def api_admin_r2_scan(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    indexed_paths = {item["path"] for item in main.data}
    unindexed = {}
    try:
        paginator = main.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=main.R2_BUCKET, Prefix="images/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    continue
                if key in indexed_paths:
                    continue
                parts = key.split("/")
                if len(parts) < 4:
                    continue
                date     = parts[1]
                loc_slug = parts[2]
                folder_slug = parts[3] if len(parts) >= 5 else ""
                fk = f"{date}|{loc_slug}|{folder_slug}"
                if fk not in unindexed:
                    location    = main._SLUG_TO_LOC.get(loc_slug, loc_slug.replace("-", " ").title())
                    folder_name = folder_slug.replace("-", " ").title() if folder_slug else ""
                    unindexed[fk] = {
                        "folder_key":  fk,
                        "date":        date,
                        "location":    location,
                        "name":        folder_name,
                        "loc_slug":    loc_slug,
                        "folder_slug": folder_slug,
                        "photo_count": 0,
                    }
                unindexed[fk]["photo_count"] += 1
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"folders": list(unindexed.values())}


@router.get("/api/admin/folder/photos")
def api_admin_folder_photos(
    request: Request,
    date: str = Query(...),
    location: str = Query(...),
    folder: str = Query(""),
):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    loc_lower    = location.strip().lower()
    folder_lower = folder.strip().lower()
    photos = []
    for item in main.data:
        if item["date"] != date:
            continue
        if item["location"].strip().lower() != loc_lower:
            continue
        item_name = (item.get("last_name") or item.get("group") or "").strip().lower()
        if item_name != folder_lower:
            continue
        from urllib.parse import quote as _quote
        photos.append({
            "path":     item["path"],
            "filename": os.path.basename(item["path"]),
            "is_draft": bool(item.get("draft")),
            "partner":  item.get("partner", ""),
            "url":      f"/api/admin/photo?path={_quote(item['path'])}&size=medium",
        })
    photos.sort(key=lambda x: natural_sort_key(x["path"]))
    live_count  = sum(1 for p in photos if not p["is_draft"])
    draft_count = sum(1 for p in photos if p["is_draft"])
    fk = _folder_key(date, location.strip(), folder.strip())
    fm = main._load_folder_meta()
    return {
        "photos":      photos,
        "total":       len(photos),
        "live_count":  live_count,
        "draft_count": draft_count,
        "folder_key":  fk,
        "group_size":  fm.get(fk, {}).get("group_size"),
        "description": fm.get(fk, {}).get("description", ""),
    }


@router.post("/api/admin/folder/description")
async def admin_folder_set_description(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    fk   = body.get("folder_key", "").strip()
    desc = body.get("description", "").strip()
    if not fk:
        return JSONResponse(status_code=400, content={"error": "Missing folder_key"})
    fm = main._load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["description"] = desc
    main._save_folder_meta(fm)
    return {"status": "ok"}


@router.post("/api/admin/photo/trash")
async def admin_photo_trash(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body       = await request.json()
    paths      = body.get("paths", [])
    if not paths:
        return JSONResponse(status_code=400, content={"error": "No paths"})
    paths_set  = set(paths)
    to_trash   = [item for item in main.data if item["path"] in paths_set]

    # Storage work runs off the main loop (visitors aren't held up), several photos at a time
    def move_to_trash():
        new_meta, _ = main._bulk_trash(to_trash)            # thumbnails go in one batch there
        main._add_to_trash_meta(new_meta)
        return new_meta

    new_meta = await run_in_threadpool(move_to_trash)
    # Only photos that reached the trash leave the site (one that couldn't be copied stays)
    gone = {e["image_entry"]["path"] for e in new_meta}
    if gone:
        main.data = [item for item in main.data if item["path"] not in gone]
        await run_in_threadpool(main._save_photo_list)
    return {"trashed": len(gone), "failed": len(to_trash) - len(gone)}


@router.post("/api/admin/pull-draft")
async def admin_pull_draft(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    pulled   = 0
    for item in main.data:
        if (not item.get("draft")
                and item["date"] == date
                and item["location"].strip().lower() == location.lower()
                and (item.get("last_name","") or item.get("group","")).strip().lower() == folder.lower()):
            item["draft"] = True
            pulled += 1
    if pulled:
        main._save_photo_list()
    return {"pulled": pulled}


@router.post("/api/admin/folder/move")
async def admin_folder_move(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body         = await request.json()
    date         = body.get("date", "")
    location     = body.get("location", "").strip()
    folder       = body.get("folder", "").strip()
    new_date     = body.get("new_date", "").strip()
    new_location = body.get("new_location", "").strip()
    if not new_date or not new_location:
        return JSONResponse(status_code=400, content={"error": "Missing new_date or new_location"})
    loc_lower    = location.lower()
    folder_lower = folder.lower()
    to_move = [item for item in main.data
               if item["date"] == date
               and item["location"].strip().lower() == loc_lower
               and (item.get("last_name","") or item.get("group","")).strip().lower() == folder_lower]
    if not to_move:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    new_loc_slug = new_location.lower().replace(" ", "-")
    folder_slug  = folder.lower().replace(" ", "-") if folder else ""
    is_portrait  = new_location.lower() in main.PORTRAIT_LOCATIONS
    path_remap   = {}
    from concurrent.futures import ThreadPoolExecutor
    def move_one(item):
        old_key  = to_r2_key(item["path"])
        filename = os.path.basename(item["path"])
        new_key  = (f"images/{new_date}/{new_loc_slug}/{folder_slug}/{filename}"
                    if folder_slug else f"images/{new_date}/{new_loc_slug}/{filename}")
        try:
            main.s3.copy_object(Bucket=main.R2_BUCKET,
                           CopySource={"Bucket": main.R2_BUCKET, "Key": old_key}, Key=new_key)
            main.s3.delete_object(Bucket=main.R2_BUCKET, Key=old_key)
            try:
                main.s3.copy_object(Bucket=main.R2_BUCKET,
                               CopySource={"Bucket": main.R2_BUCKET, "Key": _thumb_r2_key(old_key)},
                               Key=_thumb_r2_key(new_key))
                main.s3.delete_object(Bucket=main.R2_BUCKET, Key=_thumb_r2_key(old_key))
            except Exception:
                pass
            return (item["path"], new_key)
        except Exception as e:
            print(f"Move failed {old_key}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(move_one, to_move))
    for r in results:
        if r:
            path_remap[r[0]] = r[1]
    for item in main.data:
        if item["path"] in path_remap:
            item["path"]     = path_remap[item["path"]]
            item["date"]     = new_date
            item["location"] = new_location
            if is_portrait:
                item["last_name"] = item.get("last_name") or folder
                item["group"]     = ""
            else:
                item["group"]     = item.get("group") or folder
                item["last_name"] = ""
    if path_remap:
        main._save_photo_list()
    try:
        fm     = main._load_folder_meta()
        old_fk = _folder_key(date, location, folder)
        new_fk = _folder_key(new_date, new_location, folder)
        if old_fk in fm:
            fm[new_fk] = fm.pop(old_fk)
            main._save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta move error: {e}")
    main._remap_unlimited_paths_soon(path_remap)   # Unlimited packages keep their photos
    main._queue_new_photo_thumbs(list(path_remap.values()))
    return {"moved": len(path_remap), "new_date": new_date, "new_location": new_location}


# ── Moving selected photos to another folder ────────────────────────────────

def _times_of(date: str, location: str) -> dict:
    """A time-search folder's capture times {file name: minutes}, from memory or storage."""
    cached = main._folder_times_cache.get((date, location.strip().lower()))
    if cached is not None:
        return dict(cached)
    try:
        return json.loads(main.s3.get_object(Bucket=main.R2_BUCKET, Key=main._times_r2_key(date, location))["Body"].read())
    except Exception:
        return {}


def _save_times(date: str, location: str, times: dict) -> None:
    main._folder_times_cache[(date, location.strip().lower())] = times
    try:
        main.s3.put_object(Bucket=main.R2_BUCKET, Key=main._times_r2_key(date, location),
                           Body=json.dumps(times).encode(), ContentType="application/json")
    except Exception as e:
        print(f"[move] times not saved for {date} {location}: {e}")


def _move_photos(paths: list, new_date: str, new_location: str, new_folder: str) -> dict:
    """Move photos (any folders) into one folder: originals and thumbnails to the new place, the
    photo records with them (labels, partner marks, model answers kept), capture times for the
    time filter, portrait poses (out of the old pose; a pose of their own in the new family) and
    Unlimited packages. A name already in the new folder is skipped, never overwritten."""
    is_portrait = main._is_portrait_location(new_location)
    by_path = {it["path"]: it for it in main.data}
    items = [by_path[p] for p in dict.fromkeys(paths) if p in by_path]
    prefix = _upload_prefix(new_date, new_location, new_folder)
    taken = {it["path"] for it in main.data}
    plan, skipped = [], []
    for it in items:
        new_key = prefix + os.path.basename(it["path"])
        if new_key == it["path"]:
            continue                                      # already there
        if new_key in taken:
            skipped.append(os.path.basename(it["path"]))
            continue
        taken.add(new_key)
        plan.append((it, new_key))

    def move_one(job):
        it, new_key = job
        old_key = to_r2_key(it["path"])
        try:
            main.s3.copy_object(Bucket=main.R2_BUCKET, CopySource={"Bucket": main.R2_BUCKET, "Key": old_key}, Key=new_key)
            main.s3.delete_object(Bucket=main.R2_BUCKET, Key=old_key)
        except Exception as e:
            print(f"[move] {old_key}: {e}")
            return None
        try:
            main.s3.copy_object(Bucket=main.R2_BUCKET, CopySource={"Bucket": main.R2_BUCKET, "Key": _thumb_r2_key(old_key)},
                                Key=_thumb_r2_key(new_key))
            main.s3.delete_object(Bucket=main.R2_BUCKET, Key=_thumb_r2_key(old_key))
        except Exception:
            pass                                          # remade below
        main._forget_thumb_links(old_key)
        return job

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        done = [j for j in ex.map(move_one, plan) if j]

    path_remap, old_folders, times_moves = {}, set(), {}
    for it, new_key in done:
        old = dict(it)
        path_remap[it["path"]] = new_key
        old_folders.add((old["date"], old["location"], (old.get("last_name") or old.get("group") or "").strip()))
        times_moves.setdefault((old["date"], old["location"]), []).append(os.path.basename(new_key))
        it["path"], it["date"], it["location"] = new_key, new_date, new_location
        if is_portrait:
            it["last_name"], it["group"] = new_folder, ""
        else:
            it["group"], it["last_name"] = new_folder, ""
    if not done:
        return {"moved": 0, "skipped": skipped, "failed": len(plan)}
    main._save_photo_list()

    # Capture times (the time filter), carried to the new date and location
    dest_times = _times_of(new_date, new_location)
    changed = False
    for (d, loc), names in times_moves.items():
        if (d, loc.strip().lower()) == (new_date, new_location.strip().lower()):
            continue
        src = _times_of(d, loc)
        for n in names:
            if n in src:
                dest_times[n] = src[n]; changed = True
    if changed:
        _save_times(new_date, new_location, dest_times)

    # Portrait poses: out of their old pose; in the new family, a pose of their own
    try:
        moved_names = {os.path.basename(k) for k in path_remap.values()}
        fm = main._load_folder_meta()
        for d, loc, folder in old_folders:
            entry = fm.get(_folder_key(d, loc, folder))
            if entry and entry.get("poses"):
                for pose in entry["poses"]:
                    pose["files"] = [f for f in pose.get("files", []) if f not in moved_names]
                entry["poses"] = [p for p in entry["poses"] if p.get("files")]
        if is_portrait:
            dest = fm.get(_folder_key(new_date, new_location, new_folder))
            if dest and dest.get("poses"):
                dest["poses"].append({"color": "", "label": f"Pose {len(dest['poses']) + 1}", "files": sorted(moved_names)})
        main._save_folder_meta(fm)
    except Exception as e:
        print(f"[move] poses not updated: {e}")

    main._remap_unlimited_paths_soon(path_remap)          # Unlimited packages keep their photos
    main._queue_new_photo_thumbs(list(path_remap.values()))
    return {"moved": len(done), "skipped": skipped, "failed": len(plan) - len(done)}


@router.post("/api/admin/photos/move")
async def admin_photos_move(request: Request):
    """Move the photos selected in the studio to another date, location and folder."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body = await request.json()
    paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]
    new_date = str(body.get("new_date") or "").strip()
    new_location = clean_location(str(body.get("new_location") or "").strip())
    new_folder = " ".join(str(body.get("new_folder") or "").split())
    if not paths:
        return JSONResponse(status_code=400, content={"error": "Select some photos first"})
    if not new_date or not new_location:
        return JSONResponse(status_code=400, content={"error": "Choose a date and location"})
    if main._is_portrait_location(new_location) and not new_folder:
        return JSONResponse(status_code=400, content={"error": "Enter the family name for a portrait location"})
    problem = _upload_names_problem(new_date, new_location, new_folder)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    return await run_in_threadpool(_move_photos, paths, new_date, new_location, new_folder)


@router.post("/api/admin/folder/rename")
async def admin_folder_rename(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body       = await request.json()
    date       = body.get("date", "")
    location   = body.get("location", "").strip()
    folder     = body.get("folder", "").strip()
    new_folder = body.get("new_folder", "").strip()
    if not new_folder:
        return JSONResponse(status_code=400, content={"error": "Missing new_folder"})
    loc_lower    = location.lower()
    folder_lower = folder.lower()
    loc_slug     = location.lower().replace(" ", "-")
    new_slug     = new_folder.lower().replace(" ", "-")
    is_portrait  = location.lower() in main.PORTRAIT_LOCATIONS
    to_rename = [item for item in main.data
                 if item["date"] == date
                 and item["location"].strip().lower() == loc_lower
                 and (item.get("last_name","") or item.get("group","")).strip().lower() == folder_lower]
    if not to_rename:
        return JSONResponse(status_code=404, content={"error": "No photos found"})
    path_remap = {}
    from concurrent.futures import ThreadPoolExecutor
    def rename_one(item):
        old_key  = to_r2_key(item["path"])
        filename = os.path.basename(item["path"])
        new_key  = f"images/{date}/{loc_slug}/{new_slug}/{filename}"
        try:
            main.s3.copy_object(Bucket=main.R2_BUCKET,
                           CopySource={"Bucket": main.R2_BUCKET, "Key": old_key}, Key=new_key)
            # Verify the copy actually landed before touching the original.
            # R2 can silently drop concurrent copy operations under load.
            main.s3.head_object(Bucket=main.R2_BUCKET, Key=new_key)
            main.s3.delete_object(Bucket=main.R2_BUCKET, Key=old_key)
            try:
                main.s3.copy_object(Bucket=main.R2_BUCKET,
                               CopySource={"Bucket": main.R2_BUCKET, "Key": _thumb_r2_key(old_key)},
                               Key=_thumb_r2_key(new_key))
                main.s3.delete_object(Bucket=main.R2_BUCKET, Key=_thumb_r2_key(old_key))
            except Exception:
                pass
            return (item["path"], new_key)
        except Exception as e:
            print(f"Rename failed {old_key}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(rename_one, to_rename))
    for r in results:
        if r:
            path_remap[r[0]] = r[1]
    for item in main.data:
        if item["path"] in path_remap:
            item["path"] = path_remap[item["path"]]
            if is_portrait:
                item["last_name"] = new_folder
            else:
                item["group"] = new_folder
    if path_remap:
        main._save_photo_list()
    try:
        fm     = main._load_folder_meta()
        old_fk = _folder_key(date, location, folder)
        new_fk = _folder_key(date, location, new_folder)
        if old_fk in fm:
            fm[new_fk] = fm.pop(old_fk)
            main._save_folder_meta(fm)
    except Exception as e:
        print(f"folder_meta rename error: {e}")
    main._remap_unlimited_paths_soon(path_remap)   # Unlimited packages keep their photos
    main._queue_new_photo_thumbs(list(path_remap.values()))
    return {"renamed": len(path_remap), "new_folder": new_folder}


@router.post("/api/admin/folder/poses-live")
async def api_toggle_poses_live(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    family   = body.get("family", "").strip()
    live     = bool(body.get("live", False))
    if not date or not location:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    fk = _folder_key(date, location, family)
    fm = main._load_folder_meta()
    if fk not in fm:
        fm[fk] = {}
    fm[fk]["poses_live"] = live
    main._save_folder_meta(fm)
    return {"ok": True, "poses_live": live}


@router.post("/api/admin/folder/rebuild-poses")
async def api_rebuild_poses(request: Request):
    """Re-read XMP labels for all photos in a portrait folder and rebuild pose groupings."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    date     = body.get("date", "")
    location = body.get("location", "").strip()
    folder   = body.get("folder", "").strip()
    if not date or not location or not folder:
        return JSONResponse(status_code=400, content={"error": "Missing fields"})
    loc_slug    = location.lower().replace(" ", "-")
    folder_slug = folder.lower().replace(" ", "-")
    prefix      = f"images/{date}/{loc_slug}/{folder_slug}/"
    all_keys = [
        obj["Key"]
        for page in main.s3.get_paginator("list_objects_v2").paginate(Bucket=main.R2_BUCKET, Prefix=prefix)
        for obj in page.get("Contents", [])
        if obj["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]
    if not all_keys:
        return {"ok": False, "error": "No images found"}
    main._update_folder_poses(date, location, folder, all_keys)
    fk    = _folder_key(date, location, folder)
    fm    = main._load_folder_meta()
    poses = fm.get(fk, {}).get("poses", [])
    return {"ok": True, "poses_found": len(poses), "photos_scanned": len(all_keys)}


@router.get("/api/admin/partners")
def admin_partners(request: Request):
    """Partner programs and the color label photographers use to mark their photos."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    # Only what the studio needs (never the page password), with how many photos each has
    counts = {}
    for item in main.data:
        if item.get("partner"):
            counts[item["partner"]] = counts.get(item["partner"], 0) + 1
    return {"partners": [{**{k: p.get(k) for k in ("id", "name", "label", "slug")}, "count": counts.get(p["id"], 0)}
                         for p in main._load_partners()]}


@router.get("/api/admin/partner-photos")
def admin_partner_photos(request: Request, partner: str = Query("")):
    """Studio: the partner's folder in the sidebar — every photo marked for them (drafts too),
    by day, newest day first, in the order taken."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if partner not in main._partner_ids():
        return JSONResponse(status_code=404, content={"error": "Unknown partner"})
    by_day = {}
    for item in main.data:
        if item.get("partner") == partner:
            by_day.setdefault(item["date"], []).append(item)
    days = [{"date": d, "photos": [{"path": i["path"], "filename": os.path.basename(i["path"]),
                                    "location": i.get("location", ""), "folder": i.get("group") or i.get("last_name") or "",
                                    "is_draft": bool(i.get("draft"))}
                                   for i in sorted(items, key=main._taken_order)]}
            for d, items in sorted(by_day.items(), reverse=True)]
    return {"days": days, "total": sum(len(d["photos"]) for d in days)}
