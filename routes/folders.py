"""Routes: folders. Moved from main.py unchanged; shared helpers still live in main.py."""
from core.text import clean_location
from core.text import folder_key as _folder_key
from fastapi import Query
from fastapi import Request
from fastapi.responses import HTMLResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/folder-dates")
def get_folder_dates(location: str = Query("")):
    """Return distinct dates that have photos at a given location, newest first."""
    loc_lower = location.strip().lower()
    if not loc_lower:
        return {"dates": []}
    dates = sorted(
        {item["date"] for item in main.data
         if not item.get("draft") and not item.get("partner")
         and clean_location(item.get("location", "")).lower() == loc_lower},
        reverse=True,
    )
    return {"dates": dates}


@router.get("/api/folder/info")
def api_folder_info(date: str = Query(None), location: str = Query(None), family: str = Query(None)):
    if not date:
        return {"description": ""}
    # date-only key ("date||"):   used for date-level admin note
    # location-level key ("date|loc|"):  shown on sub-folder list view
    # folder-level key ("date|loc|fam"): shown inside a specific folder
    fk = _folder_key(date, (location or "").strip(), (family or "").strip())
    fm = main._load_folder_meta()
    desc = fm.get(fk, {}).get("description", "")
    # Fall back to category default_note when no folder-specific description
    if not desc and location:
        pricing = main._load_pricing()
        loc_name = (location or "").strip().lower()
        for cat in (pricing.get("activity_categories") or []):
            acts = [a.lower() for a in (cat.get("acts") or [])]
            if loc_name in acts and cat.get("default_note"):
                desc = cat["default_note"]
                break
    return {"description": desc}


@router.get("/api/folder/poses")
def api_folder_poses(request: Request, date: str = Query(None),
                     location: str = Query(None), family: str = Query(None)):
    if not date or not location or not family:
        return {"poses": [], "poses_live": False, "has_poses": False}
    fk    = _folder_key(date, location.strip(), family.strip())
    fm    = main._load_folder_meta()
    entry = fm.get(fk, {})
    poses = entry.get("poses", [])
    if not poses:
        return {"poses": [], "poses_live": False, "has_poses": False}
    poses_live = entry.get("poses_live", False)
    is_admin   = main._admin_authed(request)
    if not poses_live and not is_admin:
        return {"poses": [], "poses_live": False, "has_poses": False}
    return {
        "poses":        poses,
        "poses_live":   poses_live,
        "has_poses":    True,
        "preview_only": not poses_live and is_admin,
    }


@router.get("/clockin", response_class=HTMLResponse)
def clockin_page():
    return HTMLResponse(open("templates/clockin.html").read())
