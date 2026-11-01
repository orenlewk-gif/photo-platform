"""Routes: Settings > Security > Device links. The random addresses of the shop's own pages (the
Upload Station, store mode, the POS; see "Device links" in main.py), with a way to get each onto a
computer or iPad without typing it: copy it, scan its QR code, or have them all emailed to the owner."""
from io import BytesIO

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import main

router = APIRouter()

_NAMES = {"station": "Upload Station", "store": "Store screens", "pos": "POS"}


def _unauthorized():
    return JSONResponse(status_code=401, content={"error": "Unauthorized"})


@router.get("/api/admin/device-links")
def admin_device_links(request: Request):
    if not main._admin_authed(request):
        return _unauthorized()
    return {"links": [{"kind": k, "name": _NAMES[k], "link": main._device_link(k)} for k in main.DEVICE_LINKS]}


@router.post("/api/admin/device-links/{kind}/new")
def admin_new_device_link(kind: str, request: Request):
    """A new random address (if the old link got out). The old one stops working; approved devices
    stay approved and open the new link once."""
    if not main._admin_authed(request):
        return _unauthorized()
    if kind not in main.DEVICE_LINKS:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    main._new_device_path(kind)
    return {"kind": kind, "link": main._device_link(kind)}


@router.get("/api/admin/device-links/{kind}/qr")
def admin_device_link_qr(kind: str, request: Request):
    """The link as a QR code, to open it on an iPad or phone with the camera."""
    if not main._admin_authed(request):
        return _unauthorized()
    if kind not in main.DEVICE_LINKS:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    import segno
    buf = BytesIO()
    segno.make(main._device_link(kind), error="m").save(buf, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/api/admin/device-links/email")
def admin_email_device_links(request: Request):
    """Email all the links to the owner: open the email on the store's computer and click."""
    if not main._admin_authed(request):
        return _unauthorized()
    links = [(_NAMES[k], main._device_link(k)) for k in main.DEVICE_LINKS]
    if not main.RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "Email isn't set up on the server"})
    rows = "".join(f'<p style="margin:0 0 1rem"><strong>{main._html_escape(n)}</strong><br>'
                   f'<a href="{main._html_escape(u)}">{main._html_escape(u)}</a></p>' for n, u in links)
    body_html = ('<p>The links to the shop\'s own pages. Open this email on the computer or iPad and click '
                 'the one it needs; it asks for a code the first time.</p>' + rows +
                 '<p style="color:#b91c1c">Keep these to the shop: they\'re the way in to these pages.</p>')
    text = "\n".join(f"{n}: {u}" for n, u in links)
    try:
        r = main.http_requests.post("https://api.resend.com/emails",
                                    headers={"Authorization": f"Bearer {main.RESEND_API_KEY}", "Content-Type": "application/json"},
                                    json={"from": main.RESEND_FROM, "to": [main.OWNER_EMAIL], "subject": "Links for the shop's devices",
                                          "html": main._wrap_email_html(body_html), "text": text},
                                    timeout=10)
    except Exception:
        return JSONResponse(status_code=502, content={"error": "Couldn't send the email; try again"})
    if not r.ok:
        return JSONResponse(status_code=502, content={"error": "Couldn't send the email; try again"})
    return {"ok": True, "email": main._masked_email(main.OWNER_EMAIL)}
