"""Routes: checkout. Moved from main.py unchanged; shared helpers still live in main.py."""
import base64
import hashlib
import hmac
import json
from core.checkout import CheckoutError
from fastapi import Body
from fastapi import Request
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.post("/api/checkout")
async def create_checkout(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    # Pricing lookups and the WooCommerce order request are slow; run them off the
    # main loop so other visitors aren't held up while this checkout is created
    partner_prints = main._partner_print_discounts(request, body)     # Eagle Mount prints, from the cookie
    return await run_in_threadpool(main._create_checkout, body, partner_prints)


@router.post("/api/checkout/quote")
def checkout_quote(request: Request, body: dict = Body(...)):
    """The checkout page's totals: exactly what /api/checkout will charge."""
    try:
        return main._price_order(body, main._partner_print_discounts(request, body))["summary"]
    except CheckoutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/webhook")
async def wc_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-WC-Webhook-Signature", "")

    # Only WooCommerce, which signs each message with the shared secret, may tell us an
    # order was paid: an unsigned "paid" message would get photos sent for free. Without
    # the secret nothing can be checked, so refuse (loudly) rather than trust anything.
    if not main.WC_WEBHOOK_SECRET:
        print("ORDER WEBHOOK REFUSED: WC_WEBHOOK_SECRET is not set on the server — "
              "paid online orders are NOT getting their download emails until it is restored")
        return Response(status_code=503)
    expected = base64.b64encode(
        hmac.new(main.WC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(expected, sig):
        return Response(status_code=401)

    try:
        order = json.loads(body)
    except Exception:
        return Response(status_code=400)
    return await run_in_threadpool(main._handle_paid_wc_order, order)


@router.get("/checkout", response_class=HTMLResponse)
def checkout_page():
    return HTMLResponse(open("templates/checkout.html").read())
