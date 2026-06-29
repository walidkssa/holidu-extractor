"""
Microservice d'extraction Holidu (portable, a heberger hors Vercel).

POST /extract  { "url": "..." }  ->  ExtractResult JSON

- Airbnb  : via pyairbnb (intercepte l'API interne, resistant aux changements CSS).
- Booking : via Playwright (best-effort: JSON-LD, og:title, og:image, prix visible).
- Ne crash jamais: renvoie toujours un JSON exploitable (partial=true si incomplet).

Lancer:
    uvicorn main:app --host 0.0.0.0 --port 8000
Exposer (tunnel gratuit):
    cloudflared tunnel --url http://localhost:8000
Puis renseigner EXTRACTOR_URL (et EXTRACTOR_SECRET) cote portfolio.

Avertissement: scraper Airbnb/Booking est contraire a leurs CGU. Holidu etant
partenaire, valider l'usage interne. L'archi en microservice permet de couper
ou remplacer la source en un point unique.
"""

import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="holidu-extractor")

SECRET = os.environ.get("EXTRACTOR_SECRET")
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 600  # 10 minutes


def cached(key: str) -> Optional[dict]:
    hit = _CACHE.get(key)
    if hit and time.time() - hit["t"] < _CACHE_TTL:
        return hit["v"]
    return None


def put_cache(key: str, value: dict) -> None:
    _CACHE[key] = {"t": time.time(), "v": value}


def parse_dates(url: str) -> Dict[str, Optional[str]]:
    """check_in/check_out (Airbnb) ou checkin/checkout (Booking)."""
    q = parse_qs(urlparse(url).query)

    def first(*names: str) -> Optional[str]:
        for n in names:
            if n in q and q[n]:
                return q[n][0]
        return None

    ci = first("check_in", "checkin", "checkIn")
    co = first("check_out", "checkout", "checkOut")
    nights = None
    if ci and co:
        try:
            d1 = datetime.fromisoformat(ci)
            d2 = datetime.fromisoformat(co)
            nights = max((d2 - d1).days, 0) or None
        except ValueError:
            nights = None
    return {"checkIn": ci, "checkOut": co, "nights": nights}


def empty_result(platform: str, url: str) -> dict:
    return {
        "ok": False,
        "partial": True,
        "platform": platform,
        "photos": [],
        **parse_dates(url),
    }


def extract_airbnb(url: str) -> dict:
    """pyairbnb (solo-maintainer): verifier la signature reelle de la version installee.
    On extrait le room_id depuis /rooms/{id}, puis on mappe defensivement."""
    out = empty_result("airbnb", url)
    m = re.search(r"/rooms/(\d+)", url)
    if not m:
        return out
    room_id = m.group(1)
    dates = parse_dates(url)
    try:
        import pyairbnb  # type: ignore

        details: Dict[str, Any] = {}
        # get_details: signature variable selon la version (room_url vs room_id).
        try:
            details = pyairbnb.get_details(room_url=url) or {}
        except Exception:
            try:
                details = pyairbnb.get_details(room_id=room_id) or {}
            except Exception:
                details = {}

        def g(*keys: str) -> Any:
            cur: Any = details
            for k in keys:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    return None
            return cur

        name = g("name") or g("title")
        if name:
            out["name"] = str(name)
        rating = g("rating") or g("review_score") or g("star_rating")
        if rating:
            out["rating"] = str(rating).replace(".", ",")
        reviews = g("review_count") or g("reviews_count") or g("number_of_reviews")
        if reviews:
            out["reviewsCount"] = str(reviews)
        location = g("location") or g("city") or g("address")
        if location:
            out["location"] = str(location)

        # Photos: structures variables, on ramasse toutes les URLs http trouvees.
        photos: List[str] = []
        raw_imgs = g("images") or g("photos") or []
        if isinstance(raw_imgs, list):
            for it in raw_imgs:
                if isinstance(it, str) and it.startswith("http"):
                    photos.append(it)
                elif isinstance(it, dict):
                    u = it.get("url") or it.get("picture") or it.get("baseUrl")
                    if isinstance(u, str) and u.startswith("http"):
                        photos.append(u)
        out["photos"] = photos[:6]

        # Prix avec dates: get_price si disponible.
        if dates["checkIn"] and dates["checkOut"]:
            try:
                price_data = pyairbnb.get_price(room_id, dates["checkIn"], dates["checkOut"])  # type: ignore
                total = None
                if isinstance(price_data, dict):
                    total = price_data.get("total") or price_data.get("price") or price_data.get("amount")
                    per_night = price_data.get("per_night") or price_data.get("nightly")
                    if total is None and per_night and dates["nights"]:
                        total = float(per_night) * dates["nights"]
                if total is not None:
                    out["price"] = int(round(float(total)))
            except Exception:
                pass

        if out.get("name") or out.get("price"):
            out["ok"] = True
            out["partial"] = not out.get("price")
    except Exception as exc:  # jamais de crash
        out["error"] = f"airbnb:{type(exc).__name__}"
    return out


def extract_booking(url: str) -> dict:
    """Booking via Playwright headful (best-effort). Classes aleatoires:
    on privilegie JSON-LD, og:title, og:image, data-testid."""
    out = empty_result("booking", url)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        import json as _json

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2500)

            title = page.eval_on_selector('meta[property="og:title"]', "el => el.content") if page.query_selector('meta[property="og:title"]') else None
            if title:
                out["name"] = title

            imgs = page.eval_on_selector_all('meta[property="og:image"]', "els => els.map(e => e.content)")
            gallery = page.eval_on_selector_all("img[src*='bstatic']", "els => els.map(e => e.src)")
            photos = [u for u in (imgs or []) + (gallery or []) if isinstance(u, str) and u.startswith("http")]
            out["photos"] = list(dict.fromkeys(photos))[:6]

            for raw in page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)"):
                try:
                    data = _json.loads(raw)
                except Exception:
                    continue
                items = data if isinstance(data, list) else [data]
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if it.get("name") and not out.get("name"):
                        out["name"] = it["name"]
                    agg = it.get("aggregateRating")
                    if isinstance(agg, dict):
                        if agg.get("ratingValue"):
                            out["rating"] = str(agg["ratingValue"]).replace(".", ",")
                        if agg.get("reviewCount"):
                            out["reviewsCount"] = str(agg["reviewCount"])
                    addr = it.get("address")
                    if isinstance(addr, dict) and addr.get("addressLocality"):
                        out["location"] = addr["addressLocality"]

            body = page.content()
            price_match = re.search(r"(?:€|EUR)\s?([\d\s.,]{2,})", body)
            if price_match:
                digits = re.sub(r"[^\d]", "", price_match.group(1))
                if digits:
                    out["price"] = int(digits)
            browser.close()

        if out.get("name") or out.get("price"):
            out["ok"] = True
        out["partial"] = True  # Booking: toujours best-effort
    except Exception as exc:
        out["error"] = f"booking:{type(exc).__name__}"
    return out


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/extract")
async def extract(request: Request, x_secret: Optional[str] = Header(default=None)) -> JSONResponse:
    if SECRET and x_secret != SECRET:
        return JSONResponse({"ok": False, "error": "forbidden", "photos": []}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body or {}).get("url", "")
    if not isinstance(url, str) or not url.startswith("http"):
        return JSONResponse({"ok": False, "partial": True, "platform": "other", "photos": []})

    cache_key = url
    hit = cached(cache_key)
    if hit:
        return JSONResponse(hit)

    host = urlparse(url).netloc.lower()
    if "airbnb." in host:
        result = extract_airbnb(url)
    elif "booking." in host:
        result = extract_booking(url)
    else:
        result = empty_result("other", url)

    put_cache(cache_key, result)
    return JSONResponse(result)
