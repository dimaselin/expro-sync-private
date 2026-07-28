"""
ExPro API scraper — replaces Playwright-based scraper.py
Uses ExPro REST API: /api/auth, /api/investment/, /api/realestate/

Output schema is backward-compatible with wp_sync.py and mieszkania_sync.py.

New fields added (bonus):
  expro_uuid, postal_code, latitude, longitude, price_to, price_to_raw, rooms_from, rooms_to

Fields unavailable via API (handled gracefully):
  developer_url     → "" (wp_sync.py only writes if non-empty, so WP value preserved)
  documents         → [] (wp_sync.py only writes if non-empty)
  (zasady_wspolpracy IS collected — separate HTML pass, see fill_zasady_concurrent)
  extra.*           → parsed from description text where present; otherwise ""
                      (wp_sync.py has `if value:` guard, so existing WP values preserved)
  per-unit Phase 2  → bedrooms/bathrooms/has_balcony etc. → absent or default
                      mieszkania_sync.py has fallbacks for all of these

Migration note (realestate_id):
  Old scraper: numeric string e.g. "12345"
  New scraper: UUID e.g. "fc2879cf-6c93-4dbe-a952-b1733188e34e"
  mieszkania_sync.py has been updated with a fallback query (parent+name) to
  find existing posts and migrate expro_unit_id to UUID transparently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
try:
    from config import EXPRO, DATA_FILE
except ImportError:
    EXPRO = {
        "base_url": "https://expro.expander.pl",
        "username": "",
        "password": "",
    }
    DATA_FILE = "data/expro_data.json"

BASE_URL = EXPRO["base_url"]
USERNAME = EXPRO["username"]
PASSWORD = EXPRO["password"]

# Filter: only include these voivodeships. None = all.
VOIVODESHIP_FILTER: Optional[set[str]] = {"dolnośląskie"}
# Concurrent requests for unit detail fetching
MAX_WORKERS = 8
# Small delay between investment batches (politeness)
DELAY = 0.05

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _s(v: object) -> str:
    """Stringify an API value without turning a missing one into the word "None".

    ExPro sends JSON null for fields it has no value for, and `str(None)` is the
    four-character string "None" — which is exactly what reached the database:
    531 property posts carry fave_property_rooms="None" and 515 carry
    lokal_pietro="None", rendered verbatim on the unit card. A value ExPro
    doesn't have has to read as absent, not as a room count.
    """
    return "" if v is None else str(v).strip()


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")

def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Auth — API token + session cookie for photo downloads
# ---------------------------------------------------------------------------

def get_token() -> str:
    """POST /api/auth → JWT token (valid 24 h)."""
    log(f"Authenticating as {USERNAME} …")
    resp = requests.post(
        f"{BASE_URL}/api/auth",
        data={"login": USERNAME, "password": PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError(f"No token in auth response: {resp.text[:200]}")
    log("Auth OK.")
    return token

def make_headers(token: str) -> dict:
    # ExPro requires double-colon: "Bearer: <token>"
    return {"Authorization": f"Bearer: {token}"}

def create_web_session() -> requests.Session:
    """Login via web form to get session cookie (needed for /files/photos/ downloads).

    The login form requires a hidden "nocsrf" anti-CSRF token tied to the
    GET-rendered form — posting username/password alone (the previous
    implementation) always failed silently, so this session was NEVER
    actually authenticated. Confirmed live 2026-07-23: without the token,
    /user/login redirects right back to itself; with it, it reaches
    /dashboard and the response contains a logout link.
    """
    session = requests.Session()
    try:
        login_page = session.get(f"{BASE_URL}/user/login", timeout=15)
        m = re.search(r'name=["\']nocsrf["\']\s+value=["\']([^"\']*)["\']', login_page.text, re.I)
        nocsrf = m.group(1) if m else ""
        resp = session.post(
            f"{BASE_URL}/user/login",
            data={"username": USERNAME, "password": PASSWORD, "nocsrf": nocsrf, "Login": "ZALOGUJ"},
            allow_redirects=True,
            timeout=15,
        )
        if "login" in resp.url.lower() and "logout" not in resp.text.lower():
            log("Web session login may have failed (photos may be unavailable).")
    except Exception as e:
        log(f"Web session login error: {e}")
    return session

# ---------------------------------------------------------------------------
# Investment list
# ---------------------------------------------------------------------------

def fetch_all_investments(token: str) -> list[dict]:
    """Fetch all investments from /api/investment/ (all pages), apply filters."""
    headers = make_headers(token)
    all_invs: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{BASE_URL}/api/investment/",
            params={"page": page},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("payload", [])
        if not payload:
            break
        all_invs.extend(payload)
        paginator = data.get("paginator", {})
        total = int(paginator.get("totalItems", 0))
        log(f"  Investments page {page}: +{len(payload)} (total: {total})")
        if len(all_invs) >= total:
            break
        page += 1
        time.sleep(0.1)

    # Filter
    filtered = [
        i for i in all_invs
        if str(i.get("deleted", "0")) == "0"
        and str(i.get("removed", "0")) == "0"
    ]
    if VOIVODESHIP_FILTER:
        filtered = [
            i for i in filtered
            if (i.get("voivodeship_name") or "").lower()
            in {v.lower() for v in VOIVODESHIP_FILTER}
        ]
    log(f"API total: {len(all_invs)}  →  after filter: {len(filtered)}")
    return filtered

# ---------------------------------------------------------------------------
# Unit fetching
# ---------------------------------------------------------------------------

def fetch_units_list(token: str, inv_uuid: str) -> list[dict]:
    """GET /api/realestate/?investment_id={uuid} — handles pagination.

    Deduplicates by "uuid": for large investments (~100+ units) ExPro's API
    has been observed returning the exact same page content again for
    page=2 (confirmed byte-for-byte identical payload, same uuid) instead
    of the next page — so pagination alone can silently double every unit.
    Verified 2026-07-21 against Lokum PORTO (expro_id 2463): raw fetch
    returned 200 entries for 100 real units, unit[0] and unit[100]
    byte-identical including uuid. Dedup here guards every downstream
    consumer (mieszkania_sync.py, single-inwestycja-mieszkania.php's
    rendered unit table/cards) without needing a fix in each of them.
    """
    headers = make_headers(token)
    all_units: list[dict] = []
    seen_uuids: set[str] = set()
    page = 1
    while True:
        resp = requests.get(
            f"{BASE_URL}/api/realestate/",
            params={"investment_id": inv_uuid, "page": page},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("payload", [])
        if not payload:
            break
        new_count = 0
        for u in payload:
            uid = u.get("uuid")
            if uid and uid in seen_uuids:
                continue
            if uid:
                seen_uuids.add(uid)
            all_units.append(u)
            new_count += 1
        if new_count == 0:
            # This page added nothing we hadn't already seen — either we're
            # truly done, or the API is repeating a page (the bug this
            # function guards against). Either way, stop: if totalItems is
            # ALSO inflated by the same duplication, len(all_units) would
            # never reach it and this would otherwise loop forever.
            break
        paginator = data.get("paginator", {})
        total = int(paginator.get("totalItems", 0))
        if len(all_units) >= total:
            break
        page += 1
    return all_units

def fetch_unit_detail(token: str, unit_uuid: str) -> Optional[dict]:
    """GET /api/realestate/{uuid}/ → payload with files, type_name, stage, completion_date."""
    try:
        resp = requests.get(
            f"{BASE_URL}/api/realestate/{unit_uuid}/",
            headers=make_headers(token),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("payload") or {}
    except Exception as e:
        log(f"  WARN unit detail {unit_uuid[:8]}: {e}")
    return None

def fetch_unit_details_concurrent(
    token: str, units: list[dict], known: dict[str, dict]
) -> dict[str, dict]:
    """Fetch unit details in parallel. `known` is UUID→detail cache from prev run."""
    result: dict[str, dict] = {}
    to_fetch = [u for u in units if u["uuid"] not in known]
    if known:
        result.update({u["uuid"]: known[u["uuid"]] for u in units if u["uuid"] in known})

    if not to_fetch:
        return result

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_unit_detail, token, u["uuid"]): u["uuid"] for u in to_fetch}
        for future in as_completed(futures):
            uid = futures[future]
            detail = future.result()
            if detail is not None:
                result[uid] = detail
    return result

# ---------------------------------------------------------------------------
# Image downloading
# ---------------------------------------------------------------------------

def download_image(url: str, local_path: Path, session: Optional[requests.Session] = None) -> bool:
    """Download image to local_path. Returns True on success."""
    if local_path.exists():
        return True
    try:
        getter = session or requests
        resp = getter.get(url, timeout=20, stream=True)
        ct = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ("image" in ct or "octet" in ct):
            local_path.write_bytes(resp.content)
            return True
    except Exception as e:
        log(f"    WARN download {url}: {e}")
    return False

# ---------------------------------------------------------------------------
# Description text parsing
# ---------------------------------------------------------------------------
# API `description` field format: "Label:\tValue\nLabel:\tValue\n..."
# Maps to the same description + extra dicts as old scraper.

_DESC_KEYS: list[tuple[list[str], str, str]] = [
    # (patterns, dest_dict, dest_key)
    (["udogodnienia", "wyposażeni"],                    "desc", "udogodnienia"),
    (["przynależności", "przynaleznosci", "pomieszcze"], "desc", "przynaleznosci"),
    (["bezpieczeństwo", "bezpieczenstwo"],               "desc", "bezpieczenstwo"),
    (["garaż", "garaże", "garaz", "miejsce postojow"],  "desc", "garaz"),
    (["komunikacja", "dojazd"],                         "desc", "komunikacja"),
    (["odległość od centrum", "centrum"],                "desc", "odleglosc_centrum"),
    (["winda"],                                          "extra", "winda"),
    (["ogrzewan"],                                       "extra", "rodzaj_ogrzewania"),
    (["okna", "okien"],                                  "extra", "rodzaj_okien"),
    (["forma własności", "forma wlasnosci"],             "extra", "forma_wlasnosci"),
    (["smart"],                                          "extra", "smart_home"),
    (["ładowania", "elektryczn", "ev"],                  "extra", "stacja_ev"),
    (["ochrony", "ochrona"],                             "extra", "rodzaje_ochrony"),
    (["wielkość projektu", "wielkosc projekt"],          "extra", "wielkosc_projektu"),
    (["pod klucz", "wykończen"],                         "extra", "pod_klucz"),
    (["komórka lokatorska", "komorka lokatorska"],       "extra", "komorka_cena"),
    (["komórk", "komork"],                               "extra", "komorki_lokatorskie"),
    (["miejsce postojowe naziemne"],                     "extra", "parking_naziemne_cena"),
    (["miejsce postojowe podziemne"],                    "extra", "parking_podziemne_cena"),
    (["miejsce postojowe"],                              "extra", "miejsce_postojowe"),
]

def parse_description_text(raw: str) -> tuple[dict, dict]:
    """Return (description_dict, extra_dict) with same keys as old scraper."""
    desc: dict[str, str] = {
        "udogodnienia": "", "przynaleznosci": "", "bezpieczenstwo": "",
        "garaz": "", "komunikacja": "", "odleglosc_centrum": "",
    }
    extra: dict[str, str] = {
        "miejsce_postojowe": "", "komorki_lokatorskie": "", "smart_home": "",
        "stacja_ev": "", "rodzaje_ochrony": "", "wielkosc_projektu": "",
        "winda": "", "forma_wlasnosci": "", "rodzaj_okien": "",
        "rodzaj_ogrzewania": "", "pod_klucz": "",
        "parking_naziemne_cena": "", "parking_podziemne_cena": "",
        "komorka_cena": "", "pietro_min": "", "pietro_max": "",
    }

    if not raw:
        return desc, extra

    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        # Split on ":\t" first (API format), fall back to first ":"
        if ":\t" in line:
            lbl, val = line.split(":\t", 1)
        elif ":" in line:
            lbl, _, val = line.partition(":")
        else:
            continue
        lbl_l = lbl.strip().lower()
        val = val.strip()
        if not val:
            continue

        for patterns, dest, key in _DESC_KEYS:
            if any(p in lbl_l for p in patterns):
                target = desc if dest == "desc" else extra
                if not target[key]:   # first match wins
                    target[key] = val
                break

    return desc, extra

# ---------------------------------------------------------------------------
# Zasady współpracy (commission terms) — HTML only, absent from the REST API
# ---------------------------------------------------------------------------

ZASADY_LABELS = {
    "stawka_standard":    "Stawka standard",
    "stawka_vip":         "Stawka VIP",
    "warunki":            "Warunki wynagrodzenia",
    "garaz_w_prowizji":   "Garaż/komórka wliczana do prowizji",
    "termin_wyplaty":     "Termin wypłaty prowizji (dni)",
    "komentarz":          "Komentarz dot. zgłoszeń",
    "waznosc_zgloszenia": "Ważność zgłoszenia",
}

def parse_zasady(html: str) -> dict:
    """Extract the 'Zasady współpracy' block from an investment detail page.

    Labels and values sit in separate tags, so the tag soup is flattened to
    lines and each label is paired with the first non-label line after it.
    """
    start = html.find("Zasady współpracy")
    if start < 0:
        return {}
    seg = re.sub(r"<script.*?</script>", "", html[start:start + 8000], flags=re.S)
    lines = [l.strip() for l in re.sub(r"<[^>]+>", "\n", seg).split("\n")]
    lines = [l for l in lines if l]
    labels = set(ZASADY_LABELS.values())
    out: dict[str, str] = {}
    for key, label in ZASADY_LABELS.items():
        for n, line in enumerate(lines):
            if line.rstrip(":").strip() != label:
                continue
            val = line.split(":", 1)[1].strip() if ":" in line else ""
            if not val:
                for nxt in lines[n + 1:n + 4]:
                    cand = nxt.lstrip(":").strip()
                    if cand and cand.rstrip(":") not in labels:
                        val = cand
                        break
            if val:
                out[key] = val
            break
    return out

def fetch_zasady(session: requests.Session, inv_id: str) -> dict:
    url = f"{BASE_URL}/investments/viewdetails/id/{inv_id}/"
    for _ in range(3):
        try:
            resp = session.get(url, timeout=60)
            if resp.status_code == 200:
                return parse_zasady(resp.text)
        except Exception:
            time.sleep(1)
    return {}

def fill_zasady_concurrent(session: requests.Session, results: list[dict]) -> int:
    """Fetch commission terms for every investment and refresh scrape_hash.

    Must run before the JSON is written: scrape_hash() covers
    zasady_wspolpracy, so a rate change has to flip the hash and trigger a
    wp_sync update.
    """
    filled = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_zasady, session, inv["expro_id"]): inv
            for inv in results
        }
        for fut in as_completed(futures):
            inv = futures[fut]
            try:
                zasady = fut.result()
            except Exception as e:
                log(f"  zasady ERROR ID={inv['expro_id']}: {e}")
                continue
            if zasady:
                inv["zasady_wspolpracy"] = zasady
                inv["scrape_hash"] = scrape_hash(inv)
                filled += 1
    return filled

# ---------------------------------------------------------------------------
# Hash (same logic as old scraper for change detection)
# ---------------------------------------------------------------------------

def scrape_hash(inv: dict) -> str:
    key = json.dumps(
        {k: inv.get(k) for k in
         ["name", "price_from_raw", "delivery", "units_count",
          "units_available", "zasady_wspolpracy"]},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.md5(key.encode()).hexdigest()

# ---------------------------------------------------------------------------
# Build single investment dict
# ---------------------------------------------------------------------------

def build_investment(
    api_inv: dict,
    token: str,
    session: requests.Session,
    known_unit_details: dict[str, dict],
) -> dict:
    inv_id   = api_inv["id"]
    inv_uuid = api_inv["uuid"]

    # ── Description & extra ──────────────────────────────────────────────
    description, extra = parse_description_text(api_inv.get("description", ""))

    # Floor range from dedicated API fields (more reliable than description text)
    if api_inv.get("floor_from") is not None:
        extra["pietro_min"] = str(api_inv["floor_from"])
    if api_inv.get("floor_to") is not None:
        extra["pietro_max"] = str(api_inv["floor_to"])

    # ── Prices ───────────────────────────────────────────────────────────
    def safe_float(v: object) -> Optional[float]:
        try:
            f = float(v or 0)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    price_from_f = safe_float(api_inv.get("price_from"))
    price_to_f   = safe_float(api_inv.get("price_to"))

    def fmt_price(v: Optional[float]) -> str:
        if not v:
            return ""
        # "965 000,00 PLN" — matching old scraper format
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} PLN"

    # ── Delivery ──────────────────────────────────────────────────────────
    delivery = (api_inv.get("completion_date_from") or
                api_inv.get("completion_date_to") or "")

    # ── Investment gallery images ────────────────────────────────────────────
    # The old code built URLs as /files/photos/{pic} — wrong path entirely
    # (real 404 from the backend, not a login redirect). Confirmed live
    # 2026-07-23 via a Playwright network trace of ExPro's own admin panel
    # (viewing an investment with photos, /investments/viewdetails/id/{id}):
    # the real path is /files/investments/{pic}, and it's PUBLIC — no
    # session needed, same as /files/files/ for unit plans. (There's also an
    # "h30_{pic}" thumbnail variant the admin UI uses for small previews;
    # the bare filename is the full-size original, used here.)
    # This was silently breaking gallery photos for every investment whose
    # media-import step ran with this code (Aleja Platanowa 2, Osiedle Nasza
    # Symfonia etap II, Lokum Porto Finale — 0 gallery images despite real
    # filenames in the data). Still downloaded via `session` (harmless extra
    # cookie on a public request) and stored locally, mirroring the existing
    # plan-image pattern, so import_media.py can SFTP-upload + `wp media
    # import` from the local path instead of a remote URL either way.
    #
    # `picture` is the one the developer designated as the main shot, and it is
    # a separate field from `pictures`; taking pictures[0] as the featured image
    # picks a different photo on 64 of 170 investments. Put `picture` first so
    # the thumbnail is the intended one. This does not retroactively fix those
    # 64: import_media.py skips any post that already has projekt_galeria, and
    # wp_sync only reaches its image block when scrape_hash changed — and the
    # hash does not cover photos. They need a one-off re-order pass.
    images: list[str] = []
    image_url_map: dict[str, str] = {}
    pics_raw = api_inv.get("pictures") or api_inv.get("picture") or ""
    main_pic = (api_inv.get("picture") or "").strip()
    pic_names = [p.strip() for p in pics_raw.split(",") if p.strip()]
    if main_pic:
        pic_names = [main_pic] + [p for p in pic_names if p != main_pic]
    seen_pics: set[str] = set()
    img_gallery_dir = Path(f"data/images/inv_{inv_id}")
    for pic in pic_names:
        if pic and pic not in seen_pics:
            seen_pics.add(pic)
            img_url = f"{BASE_URL}/files/investments/{pic}"
            images.append(img_url)
            img_gallery_dir.mkdir(parents=True, exist_ok=True)
            local = img_gallery_dir / pic
            if download_image(img_url, local, session=session):
                image_url_map[img_url] = str(local)

    # ── Units ─────────────────────────────────────────────────────────────
    try:
        raw_units = fetch_units_list(token, inv_uuid)
    except Exception as e:
        log(f"  WARN units list for {inv_id}: {e}")
        raw_units = []

    details = fetch_unit_details_concurrent(token, raw_units, known_unit_details)

    units: list[dict] = []
    units_available = 0

    for ru in raw_units:
        uid    = ru["uuid"]
        detail = details.get(uid, {})
        status = ru.get("status_name", "")
        if "dostępne" in status.lower():
            units_available += 1

        # Plan image — /files/files/ is PUBLIC (no auth required)
        plan_urls:    list[str] = []
        plan_url_map: dict[str, str] = {}
        card_urls:    list[str] = []

        files = detail.get("files") or {}
        plan_path = files.get("plan") or ""
        card_path = files.get("card") or ""

        if plan_path:
            plan_url = f"{BASE_URL}{plan_path}"
            plan_urls.append(plan_url)
            img_dir = Path(f"data/images/{uid}")
            img_dir.mkdir(parents=True, exist_ok=True)
            local = img_dir / "plan_0.jpg"
            if download_image(plan_url, local):
                plan_url_map[plan_url] = str(local)
        elif detail.get("_cached_plan_urls"):
            # Restore from cache — image already on disk from previous run
            plan_urls = list(detail["_cached_plan_urls"])
            plan_url_map = dict(detail.get("_cached_plan_map", {}))

        if card_path:
            card_urls.append(f"{BASE_URL}{card_path}")

        unit: dict = {
            "name":         _s(ru.get("name")),
            "status":       status,
            "stage":        _s(detail.get("stage")),
            "price_raw":    str(int(float(ru["price"]))) if ru.get("price") else "",
            "price_m2_raw": str(int(float(ru["pricemkw"]))) if ru.get("pricemkw") else "",
            "area_raw":     _s(ru.get("area")),
            "rooms":        _s(ru.get("rooms")),
            "floor":        _s(ru.get("floor")),
            "delivery":     _s(detail.get("completion_date")) or delivery,
            "type":         _s(detail.get("type_name")),
            # realestate_id is now UUID — mieszkania_sync.py handles the migration
            "realestate_id": uid,
            "plan_urls":     plan_urls,
            "plan_url_map":  plan_url_map,
            "photo_urls":    card_urls,
            "photo_url_map": {},
        }
        units.append(unit)

    # ── Contact (partial — Expander keeper name only; phone/email via pipeline) ──
    contact: dict[str, str] = {
        "name":  api_inv.get("expander_keeper", ""),
        "phone": "",
        "email": "",
    }

    # ── Assemble — identical keys to old scraper + new bonus fields ──────
    inv: dict = {
        # ── Core (same as old scraper) ──────────────────────────────────
        "expro_id":       inv_id,
        "expro_url":      f"{BASE_URL}/investments/viewdetails/id/{inv_id}/",
        "name":           api_inv.get("name") or f"Inwestycja {inv_id}",
        "developer":      api_inv.get("developer", ""),
        "developer_url":  "",          # not in API; wp_sync.py guards with `if value:`
        "street":         (api_inv.get("street") or "").strip(),
        "city":           api_inv.get("city", ""),
        "district":       api_inv.get("district") or "",
        "province":       api_inv.get("voivodeship_name", ""),
        "price_from_raw": fmt_price(price_from_f),
        "price_to_raw":   fmt_price(price_to_f),
        "price_from":     price_from_f,
        "area_from":      float(api_inv["area_from"]) if api_inv.get("area_from") else None,
        "area_to":        float(api_inv["area_to"])   if api_inv.get("area_to")   else None,
        "delivery":       delivery,
        "units_count":    int(api_inv.get("count_realestates") or 0),
        "units_available": units_available,
        "description":    description,
        "extra":          extra,
        "images":         images,
        "image_url_map":  image_url_map,
        "contact":        contact,
        "units":          units,
        "documents":      [],          # not in API; wp_sync.py guards with `if documents:`
        "zasady_wspolpracy": {},       # filled by fill_zasady_concurrent() (HTML pass)
        "last_updated_expro": api_inv.get("creation_date", ""),
        # ── Bonus fields (new, not from old scraper) ────────────────────
        "expro_uuid":     inv_uuid,
        "postal_code":    api_inv.get("postal_code", ""),
        "latitude":       api_inv.get("latitude", ""),
        "longitude":      api_inv.get("longitude", ""),
        "price_to":       price_to_f,
        # ExPro's own price-per-m² range. wp_sync derives the figure it shows
        # from the unit rows; this is the fallback for investments whose units
        # carry no per-m² price of their own.
        "price_m2_from":  safe_float(api_inv.get("pricemkw_from")),
        "price_m2_to":    safe_float(api_inv.get("pricemkw_to")),
        "rooms_from":     api_inv.get("rooms_from", ""),
        "rooms_to":       api_inv.get("rooms_to", ""),
        "building_type_id": api_inv.get("building_type_id", ""),
        # ── Hash ────────────────────────────────────────────────────────
        "scrape_hash":    "",
    }
    inv["scrape_hash"] = scrape_hash(inv)
    return inv

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    # Auth
    try:
        token = get_token()
    except Exception as e:
        log(f"FATAL: auth failed — {e}")
        sys.exit(1)

    # Session cookie for photo downloads (best-effort)
    session = create_web_session()

    # Investment list
    log("Fetching investment list …")
    try:
        investments = fetch_all_investments(token)
    except Exception as e:
        log(f"FATAL: could not fetch investments — {e}")
        sys.exit(1)

    if not investments:
        log("No investments after filtering. Adjust VOIVODESHIP_FILTER.")
        sys.exit(1)

    # Test mode: EXPRO_TEST_INV_ID=7498,7523
    test_env = os.environ.get("EXPRO_TEST_INV_ID", "").strip()
    if test_env:
        test_ids = set(test_env.split(","))
        investments = [i for i in investments if str(i.get("id", "")) in test_ids]
        log(f"TEST MODE: {len(investments)} investment(s) — {test_env}")
        if not investments:
            log("ERROR: none of the test IDs found in filtered list.")
            sys.exit(1)

    # Load previous scrape for unit detail cache (speed up repeat runs)
    known_unit_details: dict[str, dict] = {}
    prev_path = Path("data/expro_prev.json")
    if prev_path.exists():
        try:
            prev_data = json.loads(prev_path.read_text("utf-8"))
            for prev_inv in prev_data:
                for pu in prev_inv.get("units", []):
                    uid = pu.get("realestate_id", "")
                    if uid and "-" in uid:  # UUID format only (new api_scraper output)
                        known_unit_details[uid] = {
                            "files": {
                                "plan": "",   # URL cached in plan_urls below
                                "card": "",
                            },
                            "stage":           pu.get("stage", ""),
                            "type_name":       pu.get("type", ""),
                            "completion_date": pu.get("delivery", ""),
                            "_cached_plan_urls":  pu.get("plan_urls", []),
                            "_cached_plan_map":   pu.get("plan_url_map", {}),
                        }
            log(f"Loaded {len(known_unit_details)} cached unit details from prev scrape.")
        except Exception as e:
            log(f"WARNING: could not load prev data — {e}")

    # Scrape
    results: list[dict] = []
    total = len(investments)
    for idx, api_inv in enumerate(investments, 1):
        inv_id = api_inv.get("id", "?")
        name   = api_inv.get("name", "?")
        log(f"[{idx}/{total}] ID={inv_id} {name}")
        try:
            inv = build_investment(api_inv, token, session, known_unit_details)
            plan_count = sum(1 for u in inv["units"] if u.get("plan_urls"))
            log(f"  OK — {len(inv['units'])} units, {plan_count} plans, "
                f"{inv['units_available']} available")
            results.append(inv)
        except Exception as e:
            log(f"  ERROR: {e}")
        if idx < total:
            time.sleep(DELAY)

    # Commission terms — separate HTML pass, needs the web session
    log("Fetching zasady współpracy (commission terms) …")
    filled = fill_zasady_concurrent(session, results)
    log(f"  commission terms: {filled}/{len(results)} investments")

    # Save
    out_path = Path(DATA_FILE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"Saved {len(results)} investments → {out_path}")

    import shutil
    shutil.copy2(out_path, prev_path)
    log(f"Updated cache → {prev_path}")


if __name__ == "__main__":
    main()
