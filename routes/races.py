"""Routes: races (Settings > Races). A race is set up before race day: its name, date, and the
start list for each run, which the upload form's race mode sorts the photos by (see "Races" in
main.py and core/race.py). Race photos stay hidden while the race isn't live."""
import re
import uuid

from fastapi import APIRouter, Body, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import main
from core.race import parse_start_list, pdf_text, title_of
from core.text import clean_location

router = APIRouter()

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _unauthorized():
    return JSONResponse(status_code=401, content={"error": "Unauthorized"})


def _photo_count(race: dict) -> int:
    names = {race["name"].lower(), clean_location(race["name"]).lower()}
    return sum(1 for i in main.data if i.get("location", "").strip().lower() in names)


def _public(race: dict, counts: bool = False) -> dict:
    out = {"id": race["id"], "name": race["name"], "date": race["date"], "live": bool(race.get("live")),
           "runs": [{"n": r["n"], "title": r.get("title", ""), "file": r.get("file", ""), "updated": r.get("updated", ""),
                     "racers": r.get("racers", [])} for r in race.get("runs", [])]}
    if counts:
        out["photos"] = _photo_count(race)
    return out


@router.get("/api/races")
def list_races(request: Request):
    """The races and their start lists, for the upload form (studio and upload stations)."""
    if not main._upload_allowed(request):
        return _unauthorized()
    races = sorted(main._load_races(), key=lambda r: r["date"], reverse=True)
    return {"races": [_public(r) for r in races]}


# ── Studio: Settings > Races ────────────────────────────────────────────────

@router.get("/api/admin/races")
def admin_races(request: Request):
    if not main._admin_authed(request):
        return _unauthorized()
    races = sorted(main._load_races(), key=lambda r: r["date"], reverse=True)
    return {"races": [_public(r, counts=True) for r in races]}


def _name_problem(name: str, date: str, races: list, except_id: str = "") -> str:
    if not name:
        return "Give the race a name"
    if len(name) > main.RACE_NAME_MAX:
        return f"Keep the name to {main.RACE_NAME_MAX} letters or fewer"
    if "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        return "The name can't contain slashes"
    if not _DATE_RE.fullmatch(date or ""):
        return "Choose the race date"
    low = name.lower()
    if any(r["name"].lower() == low and r["id"] != except_id for r in races):
        return "There's already a race with that name"
    pricing = main._load_pricing()
    places = ({a.lower() for a in pricing.get("activities", {})} | {l.lower() for l in main.PORTRAIT_LOCATIONS}
              | {a.lower() for c in pricing.get("activity_categories", []) for a in (c.get("acts") or []) if a})
    if low in places:
        return "That's the name of a location; give the race its own name"
    return ""


@router.post("/api/admin/races")
def admin_create_race(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return _unauthorized()
    name = " ".join(str(body.get("name") or "").split())
    date = str(body.get("date") or "").strip()
    with main._races_lock:
        races = main._load_races()
        problem = _name_problem(name, date, races)
        if problem:
            return JSONResponse(status_code=400, content={"error": problem})
        race = {"id": uuid.uuid4().hex[:10], "name": name, "date": date, "live": False,
                "created": main._now_iso(), "runs": [{"n": 1, "racers": []}]}
        main._save_races(races + [race])
    return _public(race, counts=True)


def _find(races: list, race_id: str) -> dict | None:
    return next((r for r in races if r["id"] == race_id), None)


@router.put("/api/admin/races/{race_id}")
def admin_update_race(race_id: str, request: Request, body: dict = Body(...)):
    """Rename a race or change its date (only before it has photos: they're filed under both)."""
    if not main._admin_authed(request):
        return _unauthorized()
    with main._races_lock:
        races = main._load_races()
        race = _find(races, race_id)
        if not race:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        name = " ".join(str(body.get("name", race["name"]) or "").split())
        date = str(body.get("date", race["date"]) or "").strip()
        if (name != race["name"] or date != race["date"]) and _photo_count(race):
            return JSONResponse(status_code=400, content={
                "error": "This race already has photos, which are filed under its name and date, so they can't change"})
        problem = _name_problem(name, date, races, except_id=race_id)
        if problem:
            return JSONResponse(status_code=400, content={"error": problem})
        race["name"], race["date"] = name, date
        main._save_races(races)
    return _public(race, counts=True)


@router.delete("/api/admin/races/{race_id}")
def admin_delete_race(race_id: str, request: Request):
    if not main._admin_authed(request):
        return _unauthorized()
    with main._races_lock:
        races = main._load_races()
        race = _find(races, race_id)
        if not race:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if _photo_count(race):
            return JSONResponse(status_code=400, content={
                "error": "This race has photos. Move them or put them in the trash first."})
        main._save_races([r for r in races if r["id"] != race_id])
    return {"ok": True}


@router.post("/api/admin/races/{race_id}/runs")
def admin_add_run(race_id: str, request: Request):
    if not main._admin_authed(request):
        return _unauthorized()
    with main._races_lock:
        races = main._load_races()
        race = _find(races, race_id)
        if not race:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if len(race["runs"]) >= main.RACE_MAX_RUNS:
            return JSONResponse(status_code=400, content={"error": f"Up to {main.RACE_MAX_RUNS} runs"})
        race["runs"].append({"n": max(r["n"] for r in race["runs"]) + 1 if race["runs"] else 1, "racers": []})
        main._save_races(races)
    return _public(race, counts=True)


@router.delete("/api/admin/races/{race_id}/runs/{n}")
def admin_remove_run(race_id: str, n: int, request: Request):
    if not main._admin_authed(request):
        return _unauthorized()
    with main._races_lock:
        races = main._load_races()
        race = _find(races, race_id)
        if not race or not any(r["n"] == n for r in race["runs"]):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        if len(race["runs"]) == 1:
            return JSONResponse(status_code=400, content={"error": "A race has at least one run"})
        race["runs"] = [r for r in race["runs"] if r["n"] != n]
        main._save_races(races)
    return _public(race, counts=True)


@router.post("/api/admin/races/{race_id}/runs/{n}/start-list")
async def admin_run_start_list(race_id: str, n: int, request: Request):
    """A run's start list: the timing PDF, or pasted / text-file racers (one a line). Replaces
    the run's list (start lists change, e.g. after the first run)."""
    if not main._admin_authed(request):
        return _unauthorized()
    data = await request.body()
    if len(data) > 8 * 1024 * 1024:
        return JSONResponse(status_code=413, content={"error": "That file is too big for a start list"})
    is_pdf = data[:5] == b"%PDF-"
    if is_pdf:
        try:
            text = await run_in_threadpool(pdf_text, data)
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Couldn't read that PDF"})
    else:
        text = data.decode("utf-8", errors="replace")
    racers = parse_start_list(text)
    if not racers:
        return JSONResponse(status_code=400, content={
            "error": "No racers found. Use the timing company's start list PDF, or paste one racer a line (bib and name)."})
    file_name = " ".join(str(request.headers.get("x-file-name") or "").split())[:120]
    with main._races_lock:
        races = main._load_races()
        race = _find(races, race_id)
        run = next((r for r in race["runs"] if r["n"] == n), None) if race else None
        if not run:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        run.update({"racers": racers, "title": title_of(text) if is_pdf else "",
                    "file": file_name or ("Pasted list" if not is_pdf else "Start list.pdf"), "updated": main._now_iso()})
        main._save_races(races)
    return _public(race, counts=True)
