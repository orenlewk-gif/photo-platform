"""Routes: customers. Moved from main.py unchanged; shared helpers still live in main.py."""
import requests as http_requests
from datetime import datetime
from datetime import timezone
from fastapi import Body
from fastapi import Query
from fastapi import Request
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import main

router = APIRouter()


@router.get("/api/admin/customers/filtered")
def api_customers_filtered(request: Request):
    """Return customer emails + basic info matching date range and/or product type filters."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    date_from  = request.query_params.get("date_from", "")   # YYYY-MM-DD
    date_to    = request.query_params.get("date_to", "")     # YYYY-MM-DD
    types_raw  = request.query_params.get("types", "")       # comma-separated
    types      = {t.strip().lower() for t in types_raw.split(",") if t.strip()} if types_raw else set()

    want_pos     = not types or "in-person" in types
    want_digital = not types or "digital"  in types
    want_prints  = not types or "prints"   in types
    want_frames  = not types or "frames"   in types
    want_wc      = want_digital or want_prints or want_frames

    # Load customer directory for name/tag enrichment
    cust_by_email = {(c.get("email") or "").lower(): c for c in main._load_customers()}

    results: dict = {}  # email -> {email, name, sources: set, order_count, total_spend}

    def _upsert(email_raw: str, name: str, source: str, amount: float):
        key = email_raw.lower()
        if key not in results:
            cust = cust_by_email.get(key, {})
            results[key] = {
                "id": cust.get("id"),
                "email": email_raw,
                "name": name or f"{cust.get('first','')} {cust.get('last','')}".strip() or email_raw,
                "tags": cust.get("tags", []),
                "sources": set(),
                "order_count": 0,
                "total_spend": 0.0,
            }
        results[key]["sources"].add(source)
        results[key]["order_count"] += 1
        results[key]["total_spend"] += amount

    # ── POS orders ────────────────────────────────────────────────────────────
    if want_pos:
        for order in main._fetch_pos_orders():
            odate = (order.get("date") or "")[:10]
            if date_from and odate < date_from:
                continue
            if date_to and odate > date_to:
                continue
            email = (order.get("email") or "").strip()
            if not email:
                continue
            first = order.get("first", "")
            last  = order.get("last", "")
            name  = f"{first} {last}".strip()
            total = float(order.get("net") or order.get("total") or 0)
            _upsert(email, name, "pos", total)

    # ── WooCommerce orders ─────────────────────────────────────────────────────
    if want_wc and main.WC_KEY and main.WC_SECRET:
        try:
            params: dict = {"per_page": 100, "page": 1, "status": "completed"}
            if date_from:
                params["after"] = f"{date_from}T00:00:00"
            if date_to:
                params["before"] = f"{date_to}T23:59:59"
            while True:
                resp = http_requests.get(
                    f"{main.WC_BASE}/orders", params=params,
                    auth=(main.WC_KEY, main.WC_SECRET), timeout=20,
                )
                if not resp.ok:
                    break
                orders = resp.json()
                if not orders:
                    break
                for o in orders:
                    email = (o.get("billing") or {}).get("email", "").strip()
                    if not email:
                        continue
                    name = f"{(o.get('billing') or {}).get('first_name','')} {(o.get('billing') or {}).get('last_name','')}".strip()
                    total = float(o.get("total") or 0)
                    line_items = o.get("line_items", [])
                    def _cat(item_name: str) -> str:
                        n = item_name.lower()
                        if "frame" in n:
                            return "frames"
                        if "print" in n or "4x6" in n or "5x7" in n or "8x10" in n or "11x14" in n:
                            return "prints"
                        return "digital"
                    for li in line_items:
                        cat = _cat(li.get("name", ""))
                        if cat == "digital"  and not want_digital:
                            continue
                        if cat == "prints"   and not want_prints:
                            continue
                        if cat == "frames"   and not want_frames:
                            continue
                        _upsert(email, name, cat, total / max(len(line_items), 1))
                        break  # count order once per type match
                if len(orders) < 100:
                    break
                params["page"] += 1
        except Exception as e:
            print(f"WC filtered query error: {e}")

    out = []
    for r in results.values():
        out.append({
            "id":          r["id"],
            "email":       r["email"],
            "name":        r["name"],
            "tags":        r["tags"],
            "sources":     list(r["sources"]),
            "order_count": r["order_count"],
            "total_spend": round(r["total_spend"], 2),
        })
    out.sort(key=lambda x: x["name"].lower())
    return {"customers": out, "count": len(out)}


@router.get("/api/admin/customers")
def api_admin_customers(request: Request, q: str = Query(""),
                        offset: int = Query(0), limit: int = Query(100)):
    """Paged, searchable customer list for the studio CRM."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    customers = main._load_customers()
    if q.strip():
        customers = [c for c in customers if main._customer_admin_matches(c, q)]
    customers = sorted(customers, key=lambda c: ((c.get("last") or "").lower(),
                                                 (c.get("first") or "").lower()))
    offset = max(0, offset)
    limit  = max(1, min(limit, 500))
    return {"customers": customers[offset:offset + limit], "total": len(customers),
            "offset": offset, "limit": limit}


@router.get("/api/admin/customers/{cid}")
def api_admin_customer_get(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    c = next((c for c in main._load_customers() if c.get("id") == cid), None)
    if not c:
        return JSONResponse(status_code=404, content={"error": "Customer not found"})
    return {"customer": c}


@router.post("/api/admin/customers")
async def api_admin_customer_create(request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    fields = main._clean_customer_fields(await request.json())
    if not (fields.get("first") or fields.get("last") or fields.get("email")):
        return JSONResponse(status_code=400, content={"error": "Name or email required"})
    with main._customers_lock:
        customers = main._load_customers()
        dup = main._customer_by_email(customers, fields.get("email", ""))
        if dup:
            return JSONResponse(status_code=409, content={"error": "A customer with that email already exists",
                                                          "id": dup.get("id")})
        c = {"id": main._new_customer_id(), **fields, "source": "studio",
             "created": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        customers.append(c)
        main._save_customers(customers)
    return {"customer": c}


@router.put("/api/admin/customers/{cid}")
async def api_admin_customer_update(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    fields = main._clean_customer_fields(await request.json())
    with main._customers_lock:
        customers = main._load_customers()
        c = next((c for c in customers if c.get("id") == cid), None)
        if not c:
            return JSONResponse(status_code=404, content={"error": "Customer not found"})
        dup = main._customer_by_email(customers, fields.get("email", ""))
        if dup and dup is not c:
            return JSONResponse(status_code=409, content={"error": "Another customer already has that email"})
        c.update(fields)
        main._save_customers(customers)
    return {"customer": c}


@router.delete("/api/admin/customers/{cid}")
def api_admin_customer_delete(cid: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    with main._customers_lock:
        customers = main._load_customers()
        remaining = [c for c in customers if c.get("id") != cid]
        if len(remaining) == len(customers):
            return JSONResponse(status_code=404, content={"error": "Customer not found"})
        main._save_customers(remaining)
    return {"deleted": True}


@router.get("/api/admin/customer/{email}/history")
def api_customer_history(email: str, request: Request):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    email = email.lower().strip()
    wc_orders = []
    if main.WC_KEY and main.WC_SECRET:
        try:
            # Search by email covers both guest and registered WC customers
            ord_resp = http_requests.get(
                f"{main.WC_BASE}/orders",
                params={"search": email, "per_page": 100, "orderby": "date", "order": "desc", "status": "any"},
                auth=(main.WC_KEY, main.WC_SECRET),
                timeout=15,
            )
            if ord_resp.ok:
                wc_orders = [
                    o for o in ord_resp.json()
                    if (o.get("billing", {}).get("email") or "").lower() == email
                ]
        except Exception as e:
            print(f"WC order fetch error for {email}: {e}")

    orders_out = []
    for o in wc_orders:
        order_id = o.get("id")
        date_str = (o.get("date_created") or "")[:10]
        total = float(o.get("total") or 0)
        items = [li.get("name", "") for li in o.get("line_items", [])][:4]
        gallery_url = None
        gallery_expired = False
        try:
            token, rec, exp = main._get_best_token_for_order(str(order_id))
            if token and exp:
                gallery_url = f"/download/{token}"
                now = datetime.utcnow()
                exp_naive = exp.replace(tzinfo=None) if hasattr(exp, "tzinfo") and exp.tzinfo else exp
                gallery_expired = exp_naive < now
        except Exception:
            pass
        orders_out.append({
            "id": order_id,
            "date": date_str,
            "total": total,
            "status": o.get("status", ""),
            "items": items,
            "gallery_url": gallery_url,
            "gallery_expired": gallery_expired,
            "source": "woocommerce",
        })

    # POS orders filtered by email
    pos_orders_out = []
    for o in main._fetch_pos_orders():
        if (o.get("email") or "").lower() == email:
            pos_orders_out.append({
                "id": o.get("id"),
                "date": (o.get("date") or o.get("created", ""))[:10],
                "total": float(o.get("total") or 0),
                "location": o.get("location", ""),
                "items": [it.get("label", "") for it in (o.get("items") or [])],
                "source": "pos",
            })

    # Free photos given in the store to a partner family (Eagle Mount): shown, not counted as spending
    known = main._customer_by_email(main._load_customers(), email) or {}
    gifts_out = [{"date": g.get("date", ""), "partner": g.get("partner", ""), "photos": int(g.get("photos") or 0),
                  "gallery_url": f"/package/{g['token']}" if g.get("token") else None, "source": "gift"}
                 for g in (known.get("partner_gifts") or []) if isinstance(g, dict)]

    all_totals = [o["total"] for o in orders_out + pos_orders_out]
    all_dates  = sorted([o["date"] for o in orders_out + pos_orders_out if o.get("date")], reverse=True)
    stats = {
        "total_spend": round(sum(all_totals), 2),
        "order_count": len(all_totals),
        "last_purchase": all_dates[0] if all_dates else None,
    }
    return {"stats": stats, "orders": orders_out, "pos_orders": pos_orders_out, "gifts": gifts_out}


@router.post("/api/admin/customer/{email}/followup/preview")
def api_customer_followup_preview(email: str, request: Request, body: dict = Body(...)):
    """The exact email a follow-up would send — nothing is sent."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    built = main._build_followup_email(email, body)
    if isinstance(built, JSONResponse):
        return built
    _, subject, html, _ = built
    return {"to": email, "subject": subject, "html": html}


@router.post("/api/admin/customer/{email}/followup")
def api_customer_followup(email: str, request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main.RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "Email not configured — RESEND_API_KEY is missing"})
    optout = main._opted_out(email)
    if optout:
        return JSONResponse(status_code=409, content={
            "error": f"This customer unsubscribed from marketing emails on {optout.get('date', '')[:10]}"})
    built = main._build_followup_email(email, body)
    if isinstance(built, JSONResponse):
        return built
    ftype, subject, html, text_inner = built

    try:
        r = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {main.RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": main.RESEND_FROM, "to": [email], "subject": subject, "html": html, "text": text_inner,
                  "headers": main._list_unsubscribe_headers(email)},
            timeout=10,
        )
        if not r.ok:
            return JSONResponse(status_code=502, content={"error": f"Send failed: {r.text}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Record the follow-up on the customer record
    with main._customers_lock:
        customers = main._load_customers()
        customer  = main._customer_by_email(customers, email)
        if customer:
            customer.setdefault("followups", []).append({
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "type": ftype,
                "subject": subject,
            })
            main._save_customers(customers)

    return {"ok": True}


@router.post("/api/admin/customers/bulk-followup")
def api_bulk_followup(request: Request, body: dict = Body(...)):
    """Send a follow-up email to multiple customers using a saved template."""
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    if not main.RESEND_API_KEY:
        return JSONResponse(status_code=503, content={"error": "RESEND_API_KEY not configured"})

    emails      = body.get("emails", [])
    template_id = (body.get("template_id") or "").strip()
    extra_vars  = body.get("variables", {})   # e.g. {review_url, discount_code}

    if not emails:
        return JSONResponse(status_code=400, content={"error": "emails is required"})
    if not template_id:
        return JSONResponse(status_code=400, content={"error": "template_id is required"})

    templates = main._load_email_templates()
    if template_id not in templates:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    tmpl = templates[template_id]
    review_url = (extra_vars.get("review_url") or tmpl.get("review_url") or "").strip()
    if "{{review_button}}" in (tmpl.get("body") or "") and not review_url:
        return JSONResponse(status_code=400, content={
            "error": f"Add your Google review link to the '{tmpl.get('name', template_id)}' template in Settings > Email Templates"})

    customers = main._load_customers()
    code_obj_bulk = None
    if extra_vars.get("discount_code"):
        _code = str(extra_vars["discount_code"]).strip().upper()
        code_obj_bulk = next((c for c in main._load_discount_codes() if c.get("code", "").upper() == _code), None)

    results = []
    for email in emails:
        if main._opted_out(email):
            results.append({"email": email, "ok": False, "error": "unsubscribed", "subject": ""})
            continue
        cust = main._customer_by_email(customers, email)
        first_name = (cust.get("first") or "").strip() if cust else ""

        plain_vars = {"first_name": first_name or "there", **{k: str(v) for k, v in extra_vars.items()}}

        # Build html_blocks for known action tokens
        html_blocks: dict = {}
        if review_url:
            html_blocks["review_button"] = main._review_button_html(review_url)
            plain_vars["review_button"] = f"Leave us a review: {review_url}"
        if "discount_code" in extra_vars:
            code   = str(extra_vars["discount_code"]).upper()
            detail = main._discount_detail(code_obj_bulk) if code_obj_bulk else ""
            html_blocks["code_box"] = main._code_box_html(code, detail)
            plain_vars.setdefault("code", code)
            plain_vars.setdefault("discount_amount", detail)
            plain_vars["code_box"] = (f"Your discount code: {code}" + (f" ({detail})" if detail else "")
                                      + f"\nShop now: {main.SITE_URL}")

        subject = tmpl["subject"]
        for k, v in plain_vars.items():
            subject = subject.replace(f"{{{{{k}}}}}", str(v))
        body_html, body_text = main._render_email_body(tmpl["body"], plain_vars, html_blocks)
        html = main._wrap_email_html(body_html, marketing_to=email)
        body_text += main._marketing_footer_text(email)

        ok = False
        err = ""
        try:
            r = http_requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {main.RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": main.RESEND_FROM, "to": [email], "subject": subject, "html": html, "text": body_text,
                      "headers": main._list_unsubscribe_headers(email)},
                timeout=10,
            )
            ok = r.ok
            if not ok:
                err = r.text[:200]
        except Exception as exc:
            err = str(exc)

        results.append({"email": email, "ok": ok, "error": err, "subject": subject})

    # Record follow-ups on customer records (one save, after all sends)
    if any(r["ok"] for r in results):
        try:
            with main._customers_lock:
                customers = main._load_customers()
                for r in results:
                    cust = main._customer_by_email(customers, r["email"]) if r["ok"] else None
                    if cust:
                        cust.setdefault("followups", []).append({
                            "date": datetime.utcnow().strftime("%Y-%m-%d"),
                            "type": "bulk",
                            "subject": r["subject"],
                        })
                main._save_customers(customers)
        except Exception as e:
            print(f"bulk-followup customer save error: {e}")
    for r in results:
        r.pop("subject", None)

    sent = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": sent, "total": len(results), "results": results}
