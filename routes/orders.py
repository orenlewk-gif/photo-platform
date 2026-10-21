"""Routes: orders. Moved from main.py unchanged; shared helpers still live in main.py."""
from core.text import clean_location
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from fastapi import Body
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from html import escape as _html_escape

from fastapi import APIRouter

import main
from core.pages import render_page

router = APIRouter()


@router.post("/api/admin/orders/refund")
def admin_record_refund(request: Request, body: dict = Body(...)):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    key = str(body.get("key") or "")
    if not main._REFUND_KEY_RE.match(key):
        return JSONResponse(status_code=400, content={"error": "Unknown order"})
    try:
        amount = round(float(body.get("amount")), 2)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Enter the amount refunded"})
    with main._order_refunds_lock:
        refunds = main._load_order_refunds(strict=True)
        refunds[key] = {"amount": amount, "full": bool(body.get("full")),
                        "note": str(body.get("note") or "").strip()[:300],
                        "date": datetime.now(timezone.utc).isoformat()}
        main._save_order_refunds(refunds)
    return {"ok": True}


@router.delete("/api/admin/orders/refund")
def admin_undo_refund(request: Request, key: str = Query("")):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    with main._order_refunds_lock:
        refunds = main._load_order_refunds(strict=True)
        if refunds.pop(key, None) is not None:
            main._save_order_refunds(refunds)
    return {"ok": True}


@router.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports_page(request: Request):
    if not main._admin_authed(request):
        return RedirectResponse("/admin?next=/admin/orders")
    return RedirectResponse("/admin/orders?tab=reports")


@router.get("/api/admin/reports")
def api_admin_reports(request: Request, days: int = 30,
                      date_from: str = "", date_to: str = "",
                      all_time: int = 0):
    if not main._admin_authed(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    from collections import defaultdict
    if all_time:
        orders = main._fetch_wc_orders_all()
    elif date_from and date_to:
        orders = main._fetch_wc_orders_all(after=f"{date_from}T00:00:00",
                                      before=f"{date_to}T23:59:59")
    else:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = main._fetch_wc_orders_all(after=after)

    rows = [main._order_row(o) for o in orders]

    # Summary
    total_rev  = sum(r["total"]      for r in rows)
    total_net  = sum(r["net"]        for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    n          = len(rows)
    avg_order  = total_rev / n if n else 0

    # Daily buckets
    daily: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        d = r["date"]
        if d:
            daily[d]["revenue"] += r["total"]
            daily[d]["net"]     += r["net"]
            daily[d]["fees"]    += r["stripe_fee"]
            daily[d]["orders"]  += 1
    daily_series = [{"date": k, "revenue": round(v["revenue"], 2),
                     "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                    for k, v in sorted(daily.items())]

    # Monthly buckets
    monthly: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "fees": 0.0, "orders": 0})
    for r in rows:
        if r["date"] and len(r["date"]) >= 7:
            m = r["date"][:7]
            monthly[m]["revenue"] += r["total"]
            monthly[m]["net"]     += r["net"]
            monthly[m]["fees"]    += r["stripe_fee"]
            monthly[m]["orders"]  += 1
    monthly_series = [{"month": k, "revenue": round(v["revenue"], 2),
                       "net": round(v["net"], 2), "fees": round(v["fees"], 2), "orders": v["orders"]}
                      for k, v in sorted(monthly.items())]

    # By activity
    by_act: dict = defaultdict(lambda: {"revenue": 0.0, "net": 0.0, "orders": 0})
    for r in rows:
        loc = clean_location(r["location"]) if r["location"] else "Unknown"
        by_act[loc]["revenue"] += r["total"]
        by_act[loc]["net"]     += r["net"]
        by_act[loc]["orders"]  += 1
    by_activity = [{"name": k, "revenue": round(v["revenue"], 2),
                    "net": round(v["net"], 2), "orders": v["orders"]}
                   for k, v in sorted(by_act.items(), key=lambda x: x[1]["revenue"], reverse=True)]

    # By day of week
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow: dict = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    for r in rows:
        try:
            dt  = datetime.strptime(r["date"], "%Y-%m-%d")
            day = dow_names[dt.weekday()]
            by_dow[day]["revenue"] += r["total"]
            by_dow[day]["orders"]  += 1
        except Exception:
            pass
    dow_series = [{"day": d, "revenue": round(by_dow[d]["revenue"], 2),
                   "orders": by_dow[d]["orders"]} for d in dow_names]

    return {
        "summary": {
            "revenue":   round(total_rev, 2),
            "net":       round(total_net, 2),
            "fees":      round(total_fees, 2),
            "orders":    n,
            "avg_order": round(avg_order, 2),
        },
        "daily":       daily_series,
        "monthly":     monthly_series,
        "by_activity": by_activity,
        "by_dow":      dow_series,
    }


@router.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, days: int = 30,
                 date_from: str = "", date_to: str = "",
                 tab: str = ""):
    if not main._admin_authed(request):
        return RedirectResponse("/admin")
    custom = bool(date_from and date_to)
    if custom:
        after  = f"{date_from}T00:00:00"
        before = f"{date_to}T23:59:59"
        orders = main._fetch_wc_orders(after=after, before=before)
    else:
        after  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = main._fetch_wc_orders(after=after)
    rows       = [main._order_row(o) for o in orders]
    # In-person (POS) sales in the same period, listed with online orders, newest first
    pos_rows   = [main._pos_order_row(o) for o in main._fill_pending_square_fees(
        main._fetch_pos_orders(after=after[:10], before=before[:10] if custom else None))]
    rows       = sorted(rows + pos_rows, key=lambda r: r.get("date") or "", reverse=True)
    main._apply_refunds(rows, main._load_order_refunds())
    total_rev  = sum(r.get("charged", r["total"]) for r in rows)
    total_refunds = sum(r.get("refunded", 0) for r in rows)
    total_fees = sum(r["stripe_fee"] for r in rows)
    total_net  = sum(r["net"]        for r in rows)
    total_tips = sum(r.get("tip", 0) for r in rows)

    # Location breakdown
    loc_stats = {}
    for r in rows:
        loc = r["location"] or "Unknown"
        if loc not in loc_stats:
            loc_stats[loc] = {"count": 0, "revenue": 0.0, "net": 0.0}
        loc_stats[loc]["count"]   += 1
        loc_stats[loc]["revenue"] += r.get("charged", r["total"])
        loc_stats[loc]["net"]     += r["net"]
    loc_stats = dict(sorted(loc_stats.items(), key=lambda x: x[1]["revenue"], reverse=True))
    now_utc    = datetime.now(timezone.utc)

    # Photographer prefix map
    _photogs    = main._load_photographers()
    _prefix_map = {p.get("file_prefix","").strip().lower(): p["name"]
                   for p in _photogs if p.get("file_prefix","").strip()}
    def _detect_photog(filenames_str):
        if not filenames_str:
            return "Oren Lewkowski"
        names, seen = [], set()
        for f in filenames_str.split(','):
            f = f.strip()
            if not f: continue
            name = _prefix_map.get(f[:3].lower())
            if name and name not in seen:
                names.append(name); seen.add(name)
        return " & ".join(names) if names else "Oren Lewkowski"

    _unl_records = {}
    if any(r.get("unlimited") for r in rows):
        main._close_due_unlimited()
        _unl_records = main._unlimited_records()

    import json as _json
    def td(v, cls=""): return f'<td class="{cls}">{v}</td>'
    trs = ""
    E = _html_escape
    for r in rows:
        # Names, emails, addresses and coupons come from customers (the online checkout
        # is public), so they're escaped and can't inject anything into this page
        first_file  = E((r['filenames'] or '').split(',')[0].strip() or '—')
        is_pos      = r.get("source") == "pos"
        photographer = (main._unlimited_photog_label(r, _photogs, _unl_records)
                        or _detect_photog(r['filenames']))   # commission follows the photo's JPEG code, not who sold it
        e_name, e_email = E(f"{r['first']} {r['last']}"), E(r['email'] or '')
        e_loc, e_what   = E(r['location'] or '—'), E(r['what'] or '')
        e_photog, e_files = E(photographer), E(r['filenames'] or '—')
        e_coupon, e_addr  = E(r['coupon_code'] or '—'), E(r['ship_addr'] or '—')
        charged     = r.get("charged", r["total"])
        tip_str     = f"${r['tip']:.2f}" if r.get("tip") else '—'
        total_str   = "Comp" if r.get("is_comp") else f"${charged:.2f}"
        if r.get("refund_full"):
            total_str = f'<s style="opacity:.5">${r["charged_original"]:.2f}</s> <span style="color:#f87171">Refunded</span>'
        elif r.get("refunded"):
            total_str = f'${charged:.2f} <span style="color:#f87171">(-${r["refunded"]:.2f} refunded)</span>'
        if is_pos:
            fee_str = "pending" if r.get("fee_pending") else (f"${r['stripe_fee']:.2f}" if r['stripe_fee'] else '—')
        else:
            fee_str = f"${r['stripe_fee']:.2f}"
        order_no    = E(str(r.get("order_no") or '—'))
        receipt_url = E(r.get("receipt_url") or '')
        order_cell  = (f'<a href="{receipt_url}" target="_blank" onclick="event.stopPropagation()" '
                       f'style="color:inherit">{order_no}</a>') if receipt_url else order_no
        detail_data = _json.dumps({
            "name":         e_name,
            "email":        e_email,
            "location":     e_loc,
            "photographer": e_photog,
            "what":         e_what,
            "files":        e_files,
            "total":        total_str,
            "tip":          tip_str,
            "discount":     f"-${r['discount']:.2f}" if r['discount'] else '—',
            "fee":          fee_str,
            "refund_key":   r["refund_key"],
            "refunded":     r.get("refunded", 0),
            "refund_full":  bool(r.get("refund_full")),
            "refund_date":  r.get("refund_date", ""),
            "refund_note":  r.get("refund_note", ""),
            "charged_num":  round(r["charged_original"], 2),
            "comp":         bool(r.get("is_comp")),
            "order_no":     ("Square " + order_no) if is_pos and r.get("order_no") else order_no,
            "receipt_url":  receipt_url,
            "net":          f"${r['net']:.2f}",
            "coupon":       e_coupon,
            "shipping":     f"${r['shipping']:.2f}" if r['shipping'] else '—',
            "address":      e_addr,
            "photo_paths":  r['photo_paths'] or '',    # raw: only used inside encoded URLs
            "date":         r['date'] or '',
            "last_name":    r['last'] or '',
            # the order list's own location filter needs the raw value too
            "location_raw": r['location'] or '',
        }).replace("&", "&amp;").replace('"', '&quot;').replace("<", "&lt;").replace(">", "&gt;")
        try:
            order_dt  = datetime.strptime(r['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
            is_recent = (now_utc - order_dt).days < main.DOWNLOAD_EXPIRE_DAYS
        except Exception:
            is_recent = False
        if is_pos:
            # In-person photo sales have a package download page; other POS sales have no link
            action_btn = (f'<a href="/package/{r["package_token"]}" target="_blank" class="copy-btn" '
                          f'onclick="event.stopPropagation()">Open Link</a>') if r.get("package_token") else '—'
        elif is_recent:
            action_btn = f'<button class="copy-btn" onclick="copyLink({r["order_id"]},event)">Copy Link</button>'
        else:
            action_btn = f'<a href="/admin/regen/{r["order_id"]}" class="regen-btn" onclick="event.stopPropagation()">↺ Regen Link</a>'
        trs += f"""<tr class="order-row" data-detail="{detail_data}" onclick="showDetail(this.dataset.detail)" style="cursor:pointer">
          {td(E(r['date'] or ''))}
          {td(order_cell, 'mono')}
          {td(e_name)}
          {td(e_loc)}
          {td(e_email)}
          {td(e_what)}
          {td(e_photog)}
          {td(first_file, 'mono')}
          {td(total_str)}
          {td(tip_str)}
          {td(f"-${r['discount']:.2f}" if r['discount'] else '—', 'fee')}
          {td(fee_str, 'fee')}
          {td(f"${r['net']:.2f}", 'net')}
          {td(e_coupon)}
          {td(f"${r['shipping']:.2f}" if r['shipping'] else '—')}
          {td(e_addr)}
          {td(action_btn)}
        </tr>"""

    period_opts = "".join(
        f'<option value="{d}" {"selected" if d==days else ""}>{label}</option>'
        for d, label in [(7,"Last 7 days"),(14,"Last 14 days"),(30,"Last 30 days"),(60,"Last 60 days"),(90,"Last 90 days")]
    )
    export_qs = f"date_from={date_from}&date_to={date_to}" if custom else f"days={days}"

    loc_bars = "".join(
        f'<div class="loc-bar-row">'
        f'<span class="loc-bar-label">{_html_escape(loc)}</span>'
        f'<div class="loc-bar-track"><div class="loc-bar-fill" style="width:{(s["revenue"]/total_rev*100) if total_rev else 0:.1f}%"></div></div>'
        f'<div class="loc-bar-stats">'
        f'<span class="loc-bar-pct">{(s["revenue"]/total_rev*100) if total_rev else 0:.1f}%</span>'
        f'<span class="loc-bar-rev">${s["revenue"]:.0f}</span>'
        f'<span class="loc-bar-cnt">{s["count"]} order{"s" if s["count"]!=1 else ""}</span>'
        f'</div></div>'
        for loc, s in loc_stats.items()
      ) if loc_stats else "<span style='color:rgba(255,255,255,.3);font-size:.82rem'>No orders in this period.</span>"
    html = render_page("admin_orders.html",
        tab=tab, order_count=len(rows),
        total_rev=f"{total_rev:.2f}", total_fees=f"{total_fees:.2f}", total_net=f"{total_net:.2f}",
        total_tips=f"{total_tips:.2f}", total_refunds=f"{total_refunds:.2f}",
        loc_bars=loc_bars, period_opts=period_opts,
        date_from=_html_escape(date_from), date_to=_html_escape(date_to), export_qs=export_qs,
        table_rows=trs if trs else '<tr><td colspan="17" class="empty">No completed orders in this period.</td></tr>')
    return HTMLResponse(html)


@router.get("/admin/export")
def admin_export(request: Request, days: int = 30,
                 date_from: str = "", date_to: str = ""):
    if not main._admin_authed(request):
        return RedirectResponse("/admin")
    import csv, io
    if date_from and date_to:
        orders = main._fetch_wc_orders(after=f"{date_from}T00:00:00", before=f"{date_to}T23:59:59")
    else:
        after  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        orders = main._fetch_wc_orders(after=after)
    rows   = [main._order_row(o) for o in orders]
    rows  += [main._pos_order_row(o) for o in main._fill_pending_square_fees(main._fetch_pos_orders(
        after=(date_from if date_from and date_to else (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")),
        before=(date_to if date_from and date_to else None)))]
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    main._apply_refunds(rows, main._load_order_refunds())
    buf    = io.StringIO()
    writer = csv.writer(buf)
    _exp_photogs    = main._load_photographers()
    _exp_prefix_map = {p.get("file_prefix","").strip().lower(): p["name"]
                       for p in _exp_photogs if p.get("file_prefix","").strip()}
    def _exp_photog(fn):
        if not fn: return "Oren Lewkowski"
        names, seen = [], set()
        for f in fn.split(','):
            f = f.strip()
            if not f: continue
            name = _exp_prefix_map.get(f[:3].lower())
            if name and name not in seen:
                names.append(name); seen.add(name)
        return " & ".join(names) if names else "Oren Lewkowski"
    writer.writerow(["Date","Order #","First Name","Last Name","Email","Photographer","What Ordered",
                     "Photo Files","Location","Photo Date","Total","Tip","Discount",
                     "Card Fee","Net","Refunded","Coupon Code","Shipping Cost","Shipping Address"])
    _exp_unl = {}
    if any(r.get("unlimited") for r in rows):
        main._close_due_unlimited()
        _exp_unl = main._unlimited_records()
    for r in rows:
        photog = main._unlimited_photog_label(r, _exp_photogs, _exp_unl) or _exp_photog(r["filenames"])
        if r.get("source") == "pos":
            fee_csv = "pending" if r.get("fee_pending") else (f"${r['stripe_fee']:.2f}" if r["stripe_fee"] else "")
            order_csv = ("Square " + r["order_no"]) if r.get("order_no") else ""
        else:
            fee_csv, order_csv = f"${r['stripe_fee']:.2f}", r.get("order_no", "")
        writer.writerow([r["date"], order_csv, r["first"], r["last"], r["email"], photog, r["what"],
                         r["filenames"], r["location"], r["photo_date"],
                         "Comp" if r.get("is_comp") else f"${r['charged_original']:.2f}",
                         f"${r['tip']:.2f}" if r.get("tip") else "",
                         f"-${r['discount']:.2f}" if r["discount"] else "",
                         fee_csv, f"${r['net']:.2f}",
                         f"${r['refunded']:.2f}" if r.get("refunded") else "",
                         r["coupon_code"],
                         f"${r['shipping']:.2f}" if r["shipping"] else "",
                         r["ship_addr"]])
    filename = f"crystal-images-orders-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})
