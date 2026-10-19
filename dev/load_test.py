"""
Busy-day simulation against the LOCAL copy (dev/run_local.py) — never the live site.

Shoppers browse a winter folder the way the gallery does (days, locations, the folder,
a colour search, a page of thumbnails, a last-name lookup, a price quote), pausing
briefly between rounds; POS clerks sign in, look up customers, open a folder, load
thumbnails and ring up a comp sale. Each simulated person has their own address, so
the per-visitor limits behave as they would for real customers.

Usage:
    .venv/bin/python dev/load_test.py --shoppers 10 30 60 --pos 3 --seconds 40
"""
import argparse
import asyncio
import random
import statistics
import time
from collections import defaultdict

import httpx

DATE, LOCATION = "2026-06-07", "El Dorado Action"
SKIP_THUMBS = False
SAMPLE_THUMBS = [f"images/2026-06-07/el-dorado-action/AMS{6970 + i:05d}.JPG" for i in range(20)]


class Recorder:
    def __init__(self):
        self.times = defaultdict(list)
        self.errors = defaultdict(int)

    async def hit(self, name, coro):
        start = time.perf_counter()
        try:
            r = await coro
            ok = r.status_code < 400
        except Exception:
            ok = False
        self.times[name].append(time.perf_counter() - start)
        if not ok:
            self.errors[name] += 1

    async def hit_photo(self, name, coro):
        """A photo request, following a redirect to storage if the site sends one. Records the
        whole wait (photo arrives) and, separately, how long the site itself took to answer."""
        start = time.perf_counter()
        try:
            r = await coro
            ok = r.status_code < 400 and len(r.content) > 0
            first = r.history[0] if r.history else r
            self.times[name + " (site's part)"].append(first.elapsed.total_seconds())
        except Exception:
            ok = False
        self.times[name + " (photo arrives)"].append(time.perf_counter() - start)
        if not ok:
            self.errors[name + " (photo arrives)"] += 1


async def shopper(client, rec, n, stop):
    headers = {"X-Forwarded-For": f"203.0.{n // 250}.{n % 250 + 1}"}
    get = lambda url, **kw: client.get(url, headers=headers, **kw)
    while time.perf_counter() < stop:
        await rec.hit("gallery page", get("/"))
        await rec.hit("list days", get("/api/days"))
        await rec.hit("list locations", get(f"/api/locations?date={DATE}"))
        await rec.hit("open folder (with sizes)", get("/api/browse", params={"date": DATE, "location": LOCATION}))
        await rec.hit("folder prices", get("/api/pricing", params={"location": LOCATION, "date": DATE}))
        await rec.hit("colour search", get("/api/search", params={"date": DATE, "location": LOCATION, "jacket_color": "blue"}))
        if not SKIP_THUMBS:
            thumbs = random.sample(SAMPLE_THUMBS, 12)
            await asyncio.gather(*(rec.hit_photo("thumbnail", get("/api/photo", params={"path": p, "size": "thumb"},
                                                                     follow_redirects=True))
                                   for p in thumbs))
        await rec.hit("last-name lookup", get("/api/last-name-search", params={"q": "sm"}))
        await rec.hit("checkout quote", client.post("/api/checkout/quote", headers=headers, json={
            "digital_albums": [{"location": LOCATION, "date": DATE, "count": 2, "last_name": LOCATION}], "date": DATE}))
        await asyncio.sleep(random.uniform(0.5, 1.5))


async def pos_clerk(client, rec, n, stop):
    headers = {"X-Forwarded-For": f"198.51.100.{n + 1}", "X-POS-PIN": "1234"}
    while time.perf_counter() < stop:
        await rec.hit("POS sign-in", client.post("/api/photographer/auth", headers=headers, json={"pin": "1234"}))
        await rec.hit("POS customer search", client.get("/api/pos-customers", headers=headers, params={"q": "sm"}))
        await rec.hit("POS open folder", client.get("/api/browse", headers=headers,
                                                    params={"date": DATE, "location": LOCATION, "dims": 0}))
        if not SKIP_THUMBS:
            await asyncio.gather(*(rec.hit_photo("POS thumbnail", client.get(
                "/api/photo", headers=headers, params={"path": p, "size": "thumb", "nowm": 1}, follow_redirects=True))
                                   for p in random.sample(SAMPLE_THUMBS, 12)))
        await rec.hit("POS comp sale", client.post("/api/pos-checkout", headers=headers, json={
            "pin": "1234", "email": f"loadtest{n}@example.com", "name": "Load Test", "total": 0,
            "square_payment_id": "COMP", "items": [{"category": "base_area", "label": "Test", "price": 0, "qty": 1}]}))
        await asyncio.sleep(random.uniform(2, 4))


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0


async def warm_up(base):
    """Make sure every sample thumbnail exists before timing (as it would for a folder that went live)."""
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        await client.post("/api/photographer/auth", json={"pin": "1234"})
        for p in SAMPLE_THUMBS:
            for extra in ({}, {"nowm": 1}):
                await client.get("/api/photo", params={"path": p, "size": "thumb", **extra}, follow_redirects=True)
    await asyncio.sleep(5)   # let the background variants finish


async def run_level(base, shoppers, clerks, seconds):
    rec = Recorder()
    limits = httpx.Limits(max_connections=shoppers * 6 + clerks * 6 + 10)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        stop = time.perf_counter() + seconds
        await asyncio.gather(*([shopper(client, rec, i, stop) for i in range(shoppers)] +
                               [pos_clerk(client, rec, i, stop) for i in range(clerks)]))
    print(f"\n=== {shoppers} shoppers + {clerks} POS clerks for {seconds}s ===")
    print(f"{'step':28s} {'count':>6s} {'typical':>9s} {'slow 5%':>9s} {'slowest':>9s} {'errors':>7s}")
    for name, ts in rec.times.items():
        print(f"{name:28s} {len(ts):6d} {statistics.median(ts) * 1000:7.0f}ms {pct(ts, .95) * 1000:7.0f}ms "
              f"{max(ts) * 1000:7.0f}ms {rec.errors[name]:7d}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8777")
    ap.add_argument("--shoppers", type=int, nargs="+", default=[10, 30, 60])
    ap.add_argument("--pos", type=int, default=3)
    ap.add_argument("--seconds", type=int, default=40)
    ap.add_argument("--no-thumbs", action="store_true", help="skip thumbnails (to see what they cost)")
    a = ap.parse_args()
    global SKIP_THUMBS
    SKIP_THUMBS = a.no_thumbs
    assert "127.0.0.1" in a.base or "localhost" in a.base, "only run this against the local copy"
    if not SKIP_THUMBS:
        await warm_up(a.base)
    for n in a.shoppers:
        await run_level(a.base, n, a.pos, a.seconds)


if __name__ == "__main__":
    asyncio.run(main())
