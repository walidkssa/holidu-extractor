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


def _json_dumps_safe(obj: Any) -> str:
    import json as _json
    try:
        return _json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        return str(obj)


def _money(s: Any) -> Optional[int]:
    """Parse une chaine prix Airbnb ('1 208,00 €', '€1,208', '620') en entier."""
    import re

    if s is None:
        return None
    digits = re.sub(r"[^\d.,]", "", str(s))
    digits = re.sub(r"[.,]\d{2}$", "", digits)   # centimes
    digits = digits.replace(".", "").replace(",", "")
    return int(digits) if digits.isdigit() and 10 < int(digits) < 1000000 else None


def price_from_main(price_raw: Any) -> Optional[int]:
    """Prix du sejour, robuste mais fiable. Ordre de priorite:
    1) la ligne "Total" du detail (le vrai total), 2) le prix principal affiche,
    3) le plus grand montant plausible dans 'main' (jamais dans 'raw')."""
    if not isinstance(price_raw, dict):
        return None
    main = price_raw.get("main")
    if not isinstance(main, dict):
        return None

    # 1) Ligne "Total" dans le detail
    details = main.get("details")
    if isinstance(details, dict):
        for k, v in details.items():
            if isinstance(k, str) and "total" in k.lower():
                m = _money(v)
                if m:
                    return m

    # 2) Prix principal affiche (primaryLine)
    for key in ("price", "discountedPrice", "originalPrice"):
        v = _money(main.get(key))
        if v:
            return v

    # 3) Repli: plus grand montant plausible dans 'main' (le total est le plus eleve)
    nums: List[int] = []

    def _walk(o: Any) -> None:
        if isinstance(o, dict):
            for vv in o.values():
                _walk(vv)
        elif isinstance(o, list):
            for vv in o:
                _walk(vv)
        else:
            m = _money(o)
            if m and 30 < m < 100000:
                nums.append(m)

    _walk(main)
    return max(nums) if nums else None


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

        # Nom (cle variable selon version)
        sub = details.get("sub_description")
        sub_title = sub.get("title") if isinstance(sub, dict) else (sub if isinstance(sub, str) else None)
        name = details.get("name") or details.get("title") or sub_title or details.get("listing_title")
        if name:
            out["name"] = str(name)

        # Note: get_details renvoie un sous-dict {guest_satisfaction, review_count, ...}
        rating_obj = details.get("rating")
        if isinstance(rating_obj, dict):
            gs = rating_obj.get("guest_satisfaction") or rating_obj.get("value")
            if gs not in (None, 0, "0"):
                out["rating"] = str(gs).replace(".", ",")
            rc = rating_obj.get("review_count") or rating_obj.get("reviews_count")
            if rc:
                out["reviewsCount"] = str(rc)
        elif rating_obj:
            out["rating"] = str(rating_obj).replace(".", ",")

        # Localisation
        loc = details.get("location") or details.get("city") or details.get("address")
        if isinstance(loc, dict):
            loc = loc.get("city") or loc.get("title") or loc.get("address")
        if loc:
            out["location"] = str(loc)

        # Photos: liste d'URLs ou d'objets {url}
        photos: List[str] = []
        raw_imgs = details.get("images") or details.get("photos") or []
        if isinstance(raw_imgs, list):
            for it in raw_imgs:
                if isinstance(it, str) and it.startswith("http"):
                    photos.append(it)
                elif isinstance(it, dict):
                    u = it.get("url") or it.get("picture") or it.get("baseUrl")
                    if isinstance(u, str) and u.startswith("http"):
                        photos.append(u)
        out["photos"] = photos[:6]

        # Capacite (nb de voyageurs) et chambres
        cap = details.get("person_capacity") or details.get("capacity")
        if cap:
            out["capacity"] = str(cap)
        # Chambres: best-effort dans sub_description/highlights (texte "X chambre(s)/bedroom(s)")
        import re as _re
        blob = _json_dumps_safe(details.get("sub_description")) + " " + _json_dumps_safe(details.get("highlights"))
        mb = _re.search(r"(\d+)\s*(chambre|bedroom)", blob, _re.I)
        if mb:
            out["bedrooms"] = f"{mb.group(1)} chambres"

        # Type de logement
        rt = details.get("room_type") or details.get("home_tier")
        if rt:
            out["roomType"] = str(rt)

        # Equipements: ramasse les titres d'amenities (structure variable -> defensif)
        amenities: List[str] = []
        seen_am = set()
        def _collect_amenities(node: Any) -> None:
            if len(amenities) >= 12:
                return
            if isinstance(node, dict):
                t = node.get("title") or node.get("name")
                avail = node.get("available")
                if isinstance(t, str) and 2 < len(t) < 40 and avail is not False and t.lower() not in seen_am:
                    seen_am.add(t.lower())
                    amenities.append(t)
                for v in node.values():
                    _collect_amenities(v)
            elif isinstance(node, list):
                for v in node:
                    _collect_amenities(v)
        _collect_amenities(details.get("amenities"))
        if amenities:
            out["amenities"] = amenities[:12]

        # Prix: get_price exige api_key + cookies (sinon cookies.update(None) plante).
        # Flux: get_api_key + get_metadata_from_url (-> impression_id + cookies) -> get_price.
        price_raw: Any = None
        if dates["checkIn"] and dates["checkOut"]:
            from datetime import date as _date
            ci = _date.fromisoformat(dates["checkIn"])
            co = _date.fromisoformat(dates["checkOut"])
            try:
                api_key = pyairbnb.get_api_key("")
                _meta, price_input, cookies = pyairbnb.get_metadata_from_url(url, "en", "")
                impression_id = price_input.get("impression_id") if isinstance(price_input, dict) else None
                price_raw = pyairbnb.get_price(
                    room_id=room_id,
                    check_in=ci,
                    check_out=co,
                    adults=2,
                    currency="EUR",
                    language="fr",
                    impresion_id=impression_id,
                    api_key=api_key,
                    cookies=cookies,
                )
            except Exception as e:
                out["_price_err"] = f"{type(e).__name__}: {e}"[:200]
            # Prix EXACT affiche par Airbnb (primaryLine), pas une heuristique:
            # on ne met un prix que s'il est sans ambiguite, sinon rien (jamais de faux prix).
            total = price_from_main(price_raw)
            if total:
                out["price"] = total

        if out.get("name") or out.get("price") or out.get("photos"):
            out["ok"] = True
            out["partial"] = not out.get("price")
    except Exception as exc:  # jamais de crash
        out["error"] = f"airbnb:{type(exc).__name__}:{exc}"[:200]
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
