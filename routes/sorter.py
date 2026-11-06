"""Close-call sorter: one photo at a time, the two colors (or sports) the labeling model was torn
between, and Oren picks. Each answer becomes the photo's label right away and is kept with the
photo (item["sorted"]), so it also counts as a checked label for training.

A close call is a photo the model labeled at upload with its guesses kept (model_labels.scores):
the label it gave, and another color it scored at or above SORT_LINE. Photos in folders already
marked done, photos fixed by hand, and parts already sorted aren't asked again. The most torn
photos come first (the other color's score, highest first)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.search import merged_colors
from core.text import folder_key

import main

router = APIRouter()

PARTS = ("jacket", "pants", "sport")
# The other guess must score at least this to be asked about. The search's own line is lower
# (core/search.CLOSE_LINE, 15%: shows more photos); here 20% keeps the pile to the real calls
# (about 270 per 1,000 uploads, measured on v5a 2026-11-04).
SORT_LINE = {"jacket": 0.20, "pants": 0.20, "sport": 0.20}
COLORS = ["red", "orange", "yellow", "green", "blue", "purple", "pink", "white", "black", "gray", "brown"]
SPORTS = ("ski", "snowboard")


def _same(c: str) -> str:
    """A color as the model's guesses name it (teal is kept with blue there, camo with green, tan with brown)."""
    c = (merged_colors([c]) or [c])[0]
    return "blue" if c == "teal" else c


def close_call(item: dict, part: str) -> dict | None:
    """The call to make on this photo's jacket, pants or sport: {"a": its label, "b": the other
    guess, "a_score", "b_score"}, or None when the model wasn't torn (or kept no guesses)."""
    scores = (item.get("model_labels") or {}).get("scores") or {}
    line = SORT_LINE[part]
    if part == "sport":
        a = item.get("sport")
        split = scores.get("sport") or {}
        if a not in SPORTS or not split:
            return None
        b = next(sp for sp in SPORTS if sp != a)
        if split.get(b, 0) < line:
            return None
        return {"a": a, "b": b, "a_score": split.get(a), "b_score": split[b]}
    labels = [c for c in (item.get(f"{part}_colors") or []) if c not in ("none", "unclear")]
    guesses = scores.get(part) or []
    if not labels or not guesses:
        return None
    have = {_same(c) for c in labels}
    a = labels[0]
    own = dict((c, s) for c, s in guesses)
    for c, s in guesses:
        if s >= line and c in COLORS and c not in have:
            return {"a": a, "b": c, "a_score": own.get(_same(a)), "b_score": s}
    return None


def _folder_of(item: dict) -> str:
    return folder_key(item.get("date", ""), item.get("location", "").strip(),
                      (item.get("last_name") or item.get("group") or "").strip())


def _askable(item: dict, reviewed: set) -> bool:
    """A photo the sorter can ask about at all (its folder isn't checked, nobody fixed it by hand)."""
    if item.get("draft") or item.get("manually_corrected") or not item.get("model_labels"):
        return False
    if main._is_portrait_location(item.get("location", "")):
        return False
    return _folder_of(item) not in reviewed


def queue(part: str) -> list:
    """The close calls still to make for one part, most torn first."""
    reviewed = {k for k, v in main._load_folder_meta().items() if isinstance(v, dict) and v.get("reviewed")}
    out = []
    for item in main.data:
        if part in (item.get("sorted") or {}) or not _askable(item, reviewed):
            continue
        call = close_call(item, part)
        if call:
            out.append((item, call))
    out.sort(key=lambda ic: (-ic[1]["b_score"], ic[0].get("date", ""), ic[0]["path"]))
    return out


def _photo(item: dict, call: dict, part: str) -> dict:
    return {"path": item["path"], "file": item["path"].split("/")[-1], "date": item.get("date", ""),
            "location": item.get("location", ""), "group": item.get("last_name") or item.get("group") or "",
            "part": part, **call,
            "labels": {"jacket": merged_colors(item.get("jacket_colors")) or [],
                       "pants": merged_colors(item.get("pants_colors")) or [], "sport": item.get("sport")}}


@router.get("/admin/sorter", response_class=HTMLResponse)
def admin_sorter_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/sorter")
    return HTMLResponse(open("templates/admin_sorter.html").read())


@router.get("/api/admin/close-calls")
def admin_close_calls(request: Request, part: str = Query("jacket"), limit: int = Query(30)):
    """The next close calls for one part, and how many are left for each part."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if part not in PARTS:
        return JSONResponse(status_code=400, content={"error": "part must be jacket, pants or sport"})
    queues = {p: queue(p) for p in PARTS}
    limit = max(1, min(limit, 100))
    return {"counts": {p: len(q) for p, q in queues.items()}, "line": SORT_LINE[part],
            "photos": [_photo(it, call, part) for it, call in queues[part][:limit]]}


@router.post("/api/admin/close-calls/answer")
def admin_close_call_answer(request: Request, body: dict = Body(...)):
    """Save one answer: the photo's jacket or pants colors (a list), or its sport. With "undo": true,
    the photo goes back to what it was before its last answer for that part."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    path, part = str(body.get("path") or ""), str(body.get("part") or "")
    if part not in PARTS:
        return JSONResponse(status_code=400, content={"error": "part must be jacket, pants or sport"})
    item = next((it for it in main.data if it["path"] == path), None)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Photo not found"})
    field = "sport" if part == "sport" else f"{part}_colors"
    sorted_ = item.get("sorted") or {}

    if body.get("undo"):
        done = sorted_.get(part)
        if not done:
            return JSONResponse(status_code=409, content={"error": "Nothing to undo on this photo"})
        item[field] = done["was"]
        sorted_.pop(part)
        if sorted_:
            item["sorted"] = sorted_
        else:
            item.pop("sorted", None)
    else:
        answer = body.get("answer")
        if part == "sport":
            if answer not in SPORTS:
                return JSONResponse(status_code=400, content={"error": "sport must be ski or snowboard"})
            new = answer
        else:
            if not isinstance(answer, list):
                return JSONResponse(status_code=400, content={"error": "answer must be a list of colors"})
            new = merged_colors([c for c in answer if isinstance(c, str)])
            if not new or len(new) > 3 or any(c not in COLORS for c in new):
                return JSONResponse(status_code=400, content={"error": "pick one to three of the listed colors"})
        was = sorted_[part]["was"] if part in sorted_ else (
            item.get(field) if part == "sport" else list(item.get(field) or []))
        item[field] = new
        sorted_[part] = {"answer": new, "was": was, "how": str(body.get("how") or "")[:12],
                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        item["sorted"] = sorted_
    main._photo_list_changed()          # the gallery's color filters use the new label right away
    main._save_photo_list_soon()
    return {"ok": True, "path": path, "part": part, "now": item.get(field)}
