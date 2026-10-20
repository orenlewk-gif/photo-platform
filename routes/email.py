"""Routes: email. Moved from main.py unchanged; shared helpers still live in main.py."""
from fastapi import Body
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/admin/email-templates")
def api_list_email_templates(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"templates": list(main._load_email_templates().values())}


@router.put("/api/admin/email-templates/{tid}")
def api_update_email_template(tid: str, request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    templates = main._load_email_templates()
    if tid not in templates:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    templates[tid]["subject"] = body.get("subject", templates[tid]["subject"])
    templates[tid]["body"]    = body.get("body",    templates[tid]["body"])
    if "review_url" in body:
        review_url = (body.get("review_url") or "").strip()
        if review_url and not review_url.startswith("https://"):
            return JSONResponse(status_code=400, content={"error": "Review link must start with https://"})
        templates[tid]["review_url"] = review_url
    if not templates[tid].get("system") and "name" in body:
        templates[tid]["name"] = body["name"]
    main._save_email_templates(templates)
    return {"ok": True, "template": templates[tid]}


@router.post("/api/admin/email-templates")
def api_create_email_template(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    import uuid as _uuid
    tid = "custom_" + _uuid.uuid4().hex[:8]
    templates = main._load_email_templates()
    templates[tid] = {
        "id": tid, "name": name, "system": False,
        "subject": body.get("subject", ""),
        "body":    body.get("body",    ""),
        "tokens":  ["first_name"],
    }
    main._save_email_templates(templates)
    return {"ok": True, "template": templates[tid]}


@router.delete("/api/admin/email-templates/{tid}")
def api_delete_email_template(tid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    templates = main._load_email_templates()
    if tid not in templates:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if templates[tid].get("system"):
        return JSONResponse(status_code=400, content={"error": "Cannot delete system templates"})
    del templates[tid]
    main._save_email_templates(templates)
    return {"ok": True}
