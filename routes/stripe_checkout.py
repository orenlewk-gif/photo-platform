"""Routes: online checkout with Stripe (see "Online orders with Stripe" in main.py).

/pay/<order>?k=<key>     the order's payment page: Stripe's embedded form (card, Apple Pay, Google Pay)
/order/complete          Stripe sends the customer here after paying; checks the payment, then the
/order/<order>?k=<key>   thank-you page: the download right there, prints' next step, review link
/stripe/webhook          Stripe tells us an order was paid or refunded (signed by Stripe)
Settings > Online payments: the WooCommerce / Stripe switch and whether Stripe's keys are set,
plus a $1 test order to try the whole flow before switching."""
import json
from html import escape

from fastapi import APIRouter, Body, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import main
from core import stripe_api
from core.pages import render_page

router = APIRouter()

_NO_INDEX = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


def _money(v) -> str:
    return f"{float(v or 0):.2f}"


def _items_html(order: dict) -> str:
    rows = "".join(f'<div class="line"><span>{escape(li.get("name", "Item"))}'
                   f'{" &times; " + str(li.get("quantity")) if int(li.get("quantity") or 1) > 1 else ""}</span>'
                   f'<span>${_money(li.get("total"))}</span></div>' for li in order.get("line_items") or [])
    rows += "".join(f'<div class="line sub"><span>{escape(f.get("name", ""))}</span><span>${_money(f.get("total"))}</span></div>'
                    for f in order.get("fee_lines") or [])
    meta = {m.get("key"): m.get("value") for m in order.get("meta_data") or []}
    if meta.get("_coupon_code") and float(meta.get("_coupon_discount") or 0):
        rows += (f'<div class="line sub"><span>Code {escape(str(meta["_coupon_code"]).upper())}</span>'
                 f'<span>-${_money(meta["_coupon_discount"])}</span></div>')
    return rows


def _message_page(order: dict, text: str) -> HTMLResponse:
    return HTMLResponse(render_page("pay.html", order_id=escape(order["id"]), items=_items_html(order),
                                    total=_money(order.get("total")), payment=f'<div class="msg">{text}</div>',
                                    stripe_script=""), headers=_NO_INDEX)


@router.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: str, k: str = Query("")):
    order = main._online_order_for(order_id, k)
    if not order:
        return HTMLResponse(main._pkg_error("This payment link isn't valid. Check the link in your email, or contact us.", 404),
                            status_code=404, headers=_NO_INDEX)
    if order["status"] in ("completed", "refunded"):
        return RedirectResponse(f"/order/{order['id']}?k={order['order_key']}", status_code=303)
    if order["status"] == "cancelled":
        return _message_page(order, 'This order was cancelled. Questions? Email <a href="mailto:info@bigskyphotos.com">info@bigskyphotos.com</a>.')
    if order["status"] == "check":
        return _message_page(order, 'We\'re checking this payment. Please email <a href="mailto:info@bigskyphotos.com">info@bigskyphotos.com</a> and we\'ll sort it out.')
    if not main._stripe_ready():
        return _message_page(order, 'Payments are briefly unavailable. Please try again in a few minutes.')
    script = ('<script src="https://js.stripe.com/v3/"></script>\n<script>\n'
              f'const PAY = {json.dumps({"id": order["id"], "k": k, "pk": main.STRIPE_PUBLISHABLE_KEY})};\n'
              + """(async function () {
  const box = document.getElementById('stripe-checkout');
  function fail(msg) {
    box.outerHTML = '<div class="msg">' + msg + ' <a href="">Try again</a></div>';
  }
  if (typeof Stripe !== 'function') { fail('The payment form couldn\\'t load. Check the connection.'); return; }
  try {
    const stripe = Stripe(PAY.pk);
    const checkout = await stripe.initEmbeddedCheckout({
      fetchClientSecret: async () => {
        const r = await fetch(`/api/pay/${encodeURIComponent(PAY.id)}/session`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({k: PAY.k})});
        const d = await r.json().catch(() => ({}));
        if (d.paid) { location.href = d.paid; return new Promise(() => {}); }
        if (!r.ok || !d.client_secret) throw new Error(d.error || 'The payment form couldn\\'t start.');
        return d.client_secret;
      },
    });
    box.innerHTML = '';
    checkout.mount('#stripe-checkout');
  } catch (e) { fail(e.message || 'The payment form couldn\\'t start.'); }
})();
</script>""")
    return HTMLResponse(render_page("pay.html", order_id=escape(order["id"]), items=_items_html(order),
                                    total=_money(order.get("total")),
                                    payment='<div id="stripe-checkout"><div class="loading">Loading the payment form&hellip;</div></div>',
                                    stripe_script=script), headers=_NO_INDEX)


@router.post("/api/pay/{order_id}/session")
async def pay_session(order_id: str, body: dict = Body(...)):
    """A fresh Stripe payment form for the order (each visit to the pay page gets one)."""
    order = main._online_order_for(order_id, str(body.get("k") or ""))
    if not order:
        return JSONResponse(status_code=404, content={"error": "This payment link isn't valid"})
    if order["status"] in ("completed", "refunded"):
        return {"paid": f"/order/{order['id']}?k={order['order_key']}"}
    if order["status"] != "pending":
        return JSONResponse(status_code=409, content={"error": "This order can't be paid here; please contact us"})
    try:
        session = await run_in_threadpool(main._stripe_session_for, order)
    except stripe_api.StripeError as e:
        print(f"[stripe] session for {order_id} refused: {e}")
        return JSONResponse(status_code=502, content={"error": "The payment form couldn't start. Please try again."})
    return {"client_secret": session.get("client_secret", "")}


def _complete_content(order: dict) -> str:
    first = escape(((order.get("billing") or {}).get("first_name") or "").strip())
    email = escape((order.get("billing") or {}).get("email") or "")
    parts = [f'<h1>Thank you{", " + first if first else ""}!</h1>',
             f'<p class="sub">Order {escape(order["id"])} &middot; ${_money(order.get("total"))} paid</p>']
    if order.get("status") == "refunded":
        parts.append('<p class="note">This order was refunded.</p>')
    elif order.get("download_token"):
        parts.append(f'<a class="btn" href="/download/{escape(order["download_token"])}">Download your photos</a>')
        parts.append(f'<p class="note">We also emailed this link to {email}. It works for {main.DOWNLOAD_EXPIRE_DAYS} days.</p>')
    else:
        parts.append(f'<p class="note">We emailed your receipt to {email}.</p>')
    if main._order_has_shipping(order):
        parts.append('<div class="block">Your prints are being made. We\'ll email you the tracking number when they ship.</div>')
    review = main._review_url()
    if review:
        parts.append(f'<div class="block">Love your photos? A quick Google review means a lot to us.<br>'
                     f'<a class="review" href="{escape(review)}" target="_blank" rel="noopener">Leave a review</a></div>')
    return "".join(parts)


@router.get("/order/complete", response_class=HTMLResponse)
async def order_complete(session_id: str = Query("")):
    """Stripe sends the customer here after paying. The payment is checked with Stripe (so the
    download is here even before Stripe's webhook arrives), then on to the order's own page."""
    if not session_id.startswith("cs_"):
        return HTMLResponse(main._pkg_error("This page isn't valid.", 404), status_code=404, headers=_NO_INDEX)
    try:
        session = await run_in_threadpool(stripe_api.call, "GET", f"checkout/sessions/{session_id}", main.STRIPE_SECRET_KEY)
    except stripe_api.StripeError as e:
        print(f"[stripe] thank-you page couldn't read {session_id}: {e}")
        return HTMLResponse(render_page("order_complete.html", refresh='<meta http-equiv="refresh" content="4">',
                                        content='<h1>Almost done</h1><p class="sub">Checking your payment&hellip;</p>'),
                            headers=_NO_INDEX)
    order_id = (session.get("metadata") or {}).get("order_id") or session.get("client_reference_id") or ""
    order = main._load_online_order(order_id)
    if not order:
        return HTMLResponse(main._pkg_error("We couldn't find this order. Please contact us.", 404), status_code=404, headers=_NO_INDEX)
    if session.get("payment_status") != "paid":
        return HTMLResponse(render_page("order_complete.html", refresh='<meta http-equiv="refresh" content="4">',
                                        content='<h1>Almost done</h1><p class="sub">Your payment is being confirmed&hellip;</p>'),
                            headers=_NO_INDEX)
    order = await run_in_threadpool(main._complete_online_order, order_id, session)
    return RedirectResponse(f"/order/{order['id']}?k={order['order_key']}", status_code=303)


@router.get("/order/{order_id}", response_class=HTMLResponse)
def order_page(order_id: str, k: str = Query("")):
    order = main._online_order_for(order_id, k)
    if not order:
        return HTMLResponse(main._pkg_error("This order link isn't valid.", 404), status_code=404, headers=_NO_INDEX)
    if order["status"] == "pending":
        return RedirectResponse(f"/pay/{order['id']}?k={order['order_key']}", status_code=303)
    if order["status"] == "check":
        return HTMLResponse(render_page("order_complete.html", refresh="",
                                        content='<h1>Thank you!</h1><p class="sub">We\'re checking this payment and will email you shortly.</p>'),
                            headers=_NO_INDEX)
    return HTMLResponse(render_page("order_complete.html", refresh="", content=_complete_content(order)), headers=_NO_INDEX)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe tells us about payments: paid (deliver the order) and refunded. Only messages
    signed with the webhook's secret are believed; anything else could fake a payment."""
    payload = await request.body()
    if not main.STRIPE_WEBHOOK_SECRET:
        print("STRIPE WEBHOOK REFUSED: STRIPE_WEBHOOK_SECRET is not set on the server")
        return Response(status_code=503)
    if not stripe_api.verify_webhook(payload, request.headers.get("Stripe-Signature", ""), main.STRIPE_WEBHOOK_SECRET):
        return Response(status_code=400)
    try:
        event = json.loads(payload)
    except ValueError:
        return Response(status_code=400)
    if bool(event.get("livemode")) != (stripe_api.mode_of(main.STRIPE_SECRET_KEY) == "live"):
        return Response(status_code=200)             # a test message on the live site (or the reverse): ignore
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and obj.get("payment_status") == "paid":
        order_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id") or ""
        await run_in_threadpool(main._complete_online_order, order_id, obj)
    elif kind == "charge.refunded":
        await run_in_threadpool(main._record_stripe_refund, obj)
    return Response(status_code=200)


# ── Studio: Settings > Online payments ───────────────────────────────────────

@router.get("/api/admin/checkout-settings")
def admin_checkout_settings(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return {"provider": main._checkout_provider(),
            "chosen": main._checkout_settings_doc.load().get("provider", "woo"),
            "stripe": {"secret_key": bool(main.STRIPE_SECRET_KEY), "publishable_key": bool(main.STRIPE_PUBLISHABLE_KEY),
                       "webhook_secret": bool(main.STRIPE_WEBHOOK_SECRET),
                       "mode": stripe_api.mode_of(main.STRIPE_SECRET_KEY),
                       "keys_match": stripe_api.mode_of(main.STRIPE_SECRET_KEY) == stripe_api.mode_of(main.STRIPE_PUBLISHABLE_KEY)},
            "webhook_url": f"{main.SITE_URL}/stripe/webhook"}


@router.put("/api/admin/checkout-settings")
def admin_set_checkout_provider(request: Request, body: dict = Body(...)):
    """The switch: online checkout with WooCommerce or with Stripe (flip back any time)."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    provider = str(body.get("provider") or "")
    if provider not in ("woo", "stripe"):
        return JSONResponse(status_code=400, content={"error": "Choose WooCommerce or Stripe"})
    if provider == "stripe" and not (main._stripe_ready() and main.STRIPE_WEBHOOK_SECRET):
        return JSONResponse(status_code=400, content={"error": "Stripe's keys and webhook secret need to be on Railway first"})
    d = dict(main._checkout_settings_doc.load())
    d.update({"provider": provider, "changed": main._now_iso()})
    main._checkout_settings_doc.save(d)
    return {"ok": True, "provider": main._checkout_provider()}


@router.post("/api/admin/stripe/test-order")
def admin_stripe_test_order(request: Request):
    """A $1.00 test order (one photo) paid through Stripe, whatever the switch says, to try the
    whole flow: payment form, thank-you page, email and download."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main._stripe_ready():
        return JSONResponse(status_code=400, content={"error": "Stripe's keys aren't on Railway yet"})
    photo = next((i for i in reversed(main.data) if not i.get("draft") and not i.get("partner")), None)
    meta = [{"key": "_photo_paths", "value": photo["path"] if photo else ""},
            {"key": "_photo_location", "value": (photo or {}).get("location", "")},
            {"key": "_photo_date", "value": (photo or {}).get("date", "")},
            {"key": "_photo_files", "value": (photo or {}).get("path", "").rsplit("/", 1)[-1]},
            {"key": "_test_order", "value": "1"}]
    parts = {"priced": {"line_items": [{"product_id": main.WC_DIGITAL_PRODUCT_ID, "quantity": 1,
                                        "name": "Test order (one photo)", "subtotal": "1.00", "total": "1.00"}],
                        "fee_lines": [], "coupon_lines": [], "meta": meta, "summary": {}},
             "total": 1.0, "normal_total": 1.0}
    email = main.OWNER_EMAIL
    made = main._create_stripe_order({"email": email, "billing": {"first_name": "Test", "last_name": "Order", "email": email}},
                                     parts, source="test")
    if isinstance(made, JSONResponse):
        return made
    return {"checkout_url": made["checkout_url"], "order_id": made["order_id"]}
