"""Routes: trash. Moved from main.py unchanged; shared helpers still live in main.py."""
import threading
from datetime import datetime
from datetime import timezone
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/trash")
def get_trash_ep(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    trash = main._load_trash_meta()
    now   = datetime.now(timezone.utc)
    out   = []
    for t in trash:
        days_left = max(0, (datetime.fromisoformat(t["purge_at"]) - now).days)
        out.append({**t, "days_left": days_left,
                    "thumb_url": main._presigned_get(t["key"])})
    return {"items": out}


@router.post("/api/trash/restore")
async def trash_restore(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body      = await request.json()
    trash_key = body.get("key", "")
    trash     = main._load_trash_meta()
    item      = next((t for t in trash if t["key"] == trash_key), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    try:
        main.s3.copy_object(Bucket=main.R2_BUCKET,
                       CopySource={"Bucket": main.R2_BUCKET, "Key": trash_key},
                       Key=item["original_key"])
        main.s3.delete_object(Bucket=main.R2_BUCKET, Key=trash_key)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    if item.get("type") == "studio":
        entry = item.get("image_entry")
        if entry and entry["path"] not in {d["path"] for d in main.data}:
            main.data.append(entry)
            main._save_photo_list()
    else:
        pending = main._load_pending_meta()
        for m in pending:
            if m["key"] == item["original_key"]:
                m["status"] = "pending"
                break
        else:
            pending.append({
                "key": item["original_key"], "filename": item["filename"],
                "date": item["date"], "location": item["location"],
                "photographer_id": item.get("photographer_id"),
                "photographer_name": item.get("photographer_name", ""),
                "uploaded_at": item.get("trashed_at", ""),
                "status": "pending", "folder": item.get("folder"),
            })
        main._save_pending_meta(pending)
    main._save_trash_meta([t for t in trash if t["key"] != trash_key])
    return {"restored": True}


@router.post("/api/trash/empty")
async def trash_empty(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    trash = main._load_trash_meta()
    if not trash:
        return {"emptied": 0}
    count = len(trash)
    main._save_trash_meta([])   # clear meta immediately so UI updates fast
    keys = [t["key"] for t in trash]
    threading.Thread(target=main._batch_delete_keys, args=(keys,), daemon=True).start()
    return {"emptied": count}


@router.post("/api/trash/restore-batch")
async def trash_restore_batch(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    body     = await request.json()
    batch_id = body.get("batch_id", "")
    trash    = main._load_trash_meta()
    batch    = [t for t in trash if t.get("batch_id") == batch_id]
    if not batch:
        return JSONResponse(status_code=404, content={"error": "Batch not found"})
    pending = main._load_pending_meta()
    for t in batch:
        try:
            main.s3.copy_object(Bucket=main.R2_BUCKET,
                           CopySource={"Bucket": main.R2_BUCKET, "Key": t["key"]},
                           Key=t["original_key"])
            main.s3.delete_object(Bucket=main.R2_BUCKET, Key=t["key"])
        except Exception as e:
            print(f"Batch restore failed {t['key']}: {e}")
            continue
        for m in pending:
            if m["key"] == t["original_key"]:
                m["status"] = "pending"
                break
        else:
            pending.append({
                "key": t["original_key"], "filename": t["filename"],
                "date": t.get("date", ""), "location": t.get("location", ""),
                "folder": t.get("folder", ""), "batch_id": batch_id,
                "photographer_id": t.get("photographer_id", ""),
                "photographer_name": t.get("photographer_name", ""),
                "uploaded_at": t.get("trashed_at", ""),
                "status": "pending", "batch_total": len(batch),
            })
    main._save_pending_meta(pending)
    main._save_trash_meta([t for t in trash if t.get("batch_id") != batch_id])
    return {"restored": len(batch)}
