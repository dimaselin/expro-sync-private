"""
ExPro API scraper — replaces Playwright-based scraper.py
Uses ExPro REST API: /api/auth, /api/investment/, /api/realestate/

Output schema is backward-compatible with wp_sync.py and mieszkania_sync.py.

New fields added (bonus):
  expro_uuid, postal_code, latitude, longitude, price_to, price_to_raw, rooms_from, rooms_to

Fields the REST API does not carry are read from the investment's detail page
instead — the same page already downloaded for the commission terms, so at no
extra cost (see parse_investment_page / fill_from_html_concurrent):
  developer_url, documents, zasady_wspolpracy, contact e-mail and phone,
  the real last-change date (the API only offers creation_date), and the
  thirteen `extra.*` amenity answers that live in "Więcej informacji".

Per-unit balcony/terrace/garden/garage flags come from the weekly Playwright
pass (amenity_patch.py); mieszkania_sync.py preserves whatever WordPress holds
when they are absent.

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
# ExPro's own offer-type dictionary
# ---------------------------------------------------------------------------
# Declared by the API itself in form.elements.realestate_type_id on
# /api/investment/, and usable as a filter there. Nine requests classify the
# whole catalogue from the source, which beats guessing the type from a free
# text label with strpos: that guess defaults to "Mieszkanie" whenever nothing
# matches, and 224 units are flats today only because no pattern hit them.
REALESTATE_TYPES: dict[int, str] = {
    1:  "Mieszkanie",
    7:  "Lokal użytkowy",
    8:  "Dom",
    9:  "Nieruchomość inwestycyjna",
    10: "Wykończenie",
    11: "Apartament inwestycyjny",
    12: "Segment",
    13: "Firmy budowlane/Domy modułowe",
    14: "Zarządzanie najmem",
}


def fetch_type_map(token: str) -> dict[str, list[str]]:
    """investment id → the ExPro offer types it is filed under.

    An investment can appear under several (mixed developments genuinely do),
    so the value is a list. Failures are logged and skipped rather than
    aborting: a missing entry means the classifier falls back to its other
    signals, which is far better than no scrape at all.
    """
    headers = make_headers(token)
    out: dict[str, list[str]] = {}
    for type_id, label in REALESTATE_TYPES.items():
        found = 0
        page = 1
        while True:
            try:
                resp = requests.get(
                    f"{BASE_URL}/api/investment/",
                    params={"realestate_type_id": type_id, "page": page},
                    headers=headers,
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log(f"  WARN type map {label}: {e}")
                break
            payload = data.get("payload", [])
            if not payload:
                break
            for i in payload:
                out.setdefault(str(i.get("id")), []).append(label)
                found += 1
            total = int(data.get("paginator", {}).get("totalItems", 0) or 0)
            if found >= total:
                break
            page += 1
        log(f"  type map: {label:<32} {found}")
    return out


# ---------------------------------------------------------------------------
# Unit fetching
# ---------------------------------------------------------------------------

# A slice wider than any Polish flat will ever cost, in złoty. Only ever used
# as the starting bound of the bisection below.
_PRICE_CEILING = 30_000_000
# Area slices, used when a price slice cannot be narrowed any further because
# every unit in it costs exactly the same.
_AREA_SLICES = [(0, 30), (30, 40), (40, 50), (50, 60), (60, 80), (80, 100000)]


def _fetch_units_page(token: str, inv_uuid: str, **filters) -> tuple[list[dict], int]:
    """One /api/realestate/ response: its payload and the total it reports."""
    params = {"investment_id": inv_uuid}
    params.update(filters)
    resp = requests.get(
        f"{BASE_URL}/api/realestate/",
        params=params,
        headers=make_headers(token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payload", []) or [], int(data.get("paginator", {}).get("totalItems", 0) or 0)


def _harvest_by_price(token: str, inv_uuid: str, lo: int, hi: int,
                      out: dict[str, dict], depth: int = 0) -> None:
    """Collect every unit in a price range, splitting it until a slice fits.

    The response is capped at 100 whatever you ask for, but a *filtered*
    request reports the total for that filter — so a slice that reports 100 or
    fewer is complete and can be taken whole.
    """
    payload, total = _fetch_units_page(token, inv_uuid, price_f=lo, price_t=hi)
    if total == 0:
        return
    if total <= 100 or depth >= 14:
        if total > 100:
            # The range has collapsed to a single price shared by more than
            # 100 units. Cut the same slice by area instead.
            for a_lo, a_hi in _AREA_SLICES:
                sub, _ = _fetch_units_page(token, inv_uuid, price_f=lo, price_t=hi,
                                           area_f=a_lo, area_t=a_hi)
                for u in sub:
                    if u.get("uuid"):
                        out[u["uuid"]] = u
        for u in payload:
            if u.get("uuid"):
                out[u["uuid"]] = u
        return
    mid = (lo + hi) // 2
    if mid <= lo or mid >= hi:
        for u in payload:
            if u.get("uuid"):
                out[u["uuid"]] = u
        return
    _harvest_by_price(token, inv_uuid, lo, mid, out, depth + 1)
    _harvest_by_price(token, inv_uuid, mid + 1, hi, out, depth + 1)


def fetch_units_list(token: str, inv_uuid: str) -> list[dict]:
    """Every unit of an investment, not just the first hundred.

    `page` is dead: the server answers page=2 with page=1's body and reports
    currentPage=1 no matter what, so pagination silently caps every investment
    at 100 units — 1333 of 5696 were unreachable, 20 investments truncated,
    Quorum Tower showing 100 of 349.

    The filters, however, work. They are simply not called what every previous
    attempt assumed: the response carries its own `form.elements` listing the
    names the server actually accepts — price_f, price_t, area_f, area_t,
    rooms_f, rooms_t, floor_f, floor_t. Earlier probes sent price_from, limit,
    offset, per_page and itemsPerPage, none of which appear there, so the
    server ignored them and the cap looked absolute.

    So: ask for the whole thing first, and if it does not fit, bisect the price
    range until each slice reports 100 or fewer and take those whole.
    Verified 2026-07-28 across all 20 oversized investments — 3333 of 3333
    units, at 11-21 requests each. Investments that already fit (150 of 170)
    still cost exactly one request.

    Dedup by uuid is kept throughout: slices overlap at their boundaries, and
    separately ExPro has been seen returning page 1's content twice for a
    single unfiltered request (Lokum PORTO, 2026-07-21 — 200 entries for 100
    real units, byte-identical including uuid).
    """
    payload, total = _fetch_units_page(token, inv_uuid, page=1)
    if total <= 100:
        out: dict[str, dict] = {}
        for u in payload:
            if u.get("uuid"):
                out[u["uuid"]] = u
        return list(out.values())

    harvested: dict[str, dict] = {}
    for u in payload:
        if u.get("uuid"):
            harvested[u["uuid"]] = u
    _harvest_by_price(token, inv_uuid, 0, _PRICE_CEILING, harvested)
    if len(harvested) < total:
        log(f"  WARN units: collected {len(harvested)} of {total} — some slice is still short")
    return list(harvested.values())
    return all_units

# UUIDs whose detail could not be fetched this run, after all retries. A unit
# without its detail has no type_name and no floor plan, and until the cache
# stopped storing incomplete entries that gap was permanent — so the count has
# to be visible rather than one WARN line among thousands.
_DETAIL_FAILURES: list[str] = []

def fetch_unit_detail(token: str, unit_uuid: str, attempts: int = 3) -> Optional[dict]:
    """GET /api/realestate/{uuid}/ → payload with files, type_name, stage, completion_date.

    Retried: this ran as a single 10-second attempt across 8 threads, and every
    dropped response produced a unit with an empty type that the previous run's
    cache then preserved for good. 84 units were sitting in that hole.
    """
    last = ""
    for n in range(attempts):
        try:
            resp = requests.get(
                f"{BASE_URL}/api/realestate/{unit_uuid}/",
                headers=make_headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("payload") or {}
            last = f"HTTP {resp.status_code}"
        except Exception as e:
            last = str(e)
        if n < attempts - 1:
            time.sleep(0.4 * (n + 1))
    log(f"  WARN unit detail {unit_uuid[:8]} failed after {attempts} tries: {last}")
    _DETAIL_FAILURES.append(unit_uuid)
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

# ---------------------------------------------------------------------------
# Everything else the detail page carries
# ---------------------------------------------------------------------------
# The REST API's `description` holds exactly seven labels; every other field the
# old Playwright scraper used to collect lived in the page's "Więcej informacji"
# block and was lost in the migration — all 13 of these read empty on all 173
# investments today, along with developer_url and documents. The page is
# already downloaded on every run for the commission terms, so this costs no
# extra requests.
#
# The page states all of them the same way: a div holding <b>Label</b> and the
# value, either inline after a colon or in a <span> under a <br>.

# The class carries modifiers on some blocks ("left-col-content-element d-flex"
# holds the contact details), so the match cannot demand an exact class.
_ELEMENT_RE = re.compile(r'<div class="[^"]*left-col-content-element[^"]*"[^>]*>(.*?)</div>', re.S)
_LABEL_RE = re.compile(r"<b>(.*?)</b>", re.S)


def _plain(fragment: str) -> str:
    """Tag soup to readable text."""
    import html as _html
    txt = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(txt)).strip()


def _page_elements(page: str):
    """Yield (label, value, href) for every labelled element on the page."""
    import html as _html
    for match in _ELEMENT_RE.finditer(page):
        chunk = match.group(1)
        label_m = _LABEL_RE.search(chunk)
        if not label_m:
            continue
        label = _plain(label_m.group(1)).rstrip(":").strip()
        rest = chunk[label_m.end():]
        href_m = re.search(r'href="([^"]+)"', rest)
        value = _plain(rest).lstrip(":").strip()
        yield label, value, (_html.unescape(href_m.group(1)) if href_m else "")


# Page label → key in the investment's `extra` block. Matched exactly, because
# "Miejsce postojowe" and "Miejsce postojowe obowiązkowe" are different
# questions and a substring match would confuse them.
_MORE_INFO_MAP = {
    "winda":                                "winda",
    "rodzaj ogrzewania":                    "rodzaj_ogrzewania",
    "rodzaj okien":                         "rodzaj_okien",
    "forma własności":                      "forma_wlasnosci",
    "smart home":                           "smart_home",
    "stacja ładowania sam. elektrycznych":  "stacja_ev",
    "rodzaje ochrony":                      "rodzaje_ochrony",
    "miejsce postojowe":                    "miejsce_postojowe",
    "miejsce postojowe obowiązkowe":        "parking_obowiazkowy",
    "komórki lokatorskie":                  "komorki_lokatorskie",
    "oferta wykończenia pod klucz":         "pod_klucz",
    # Prices. The labels state the thing, not "cena" — and note the singular
    # "Komórka lokatorska" is the price while the plural "Komórki lokatorskie"
    # above is the yes/no question.
    "miejsce postojowe naziemne od":        "parking_naziemne_cena",
    "miejsce postojowe podziemne pojedyncze": "parking_podziemne_cena",
    "komórka lokatorska":                   "komorka_cena",
}

# Price fields whose "no data" is written as a number. ExPro renders an unset
# price as "od 0,00 PLN/m2 do 0,00 PLN/m2", which would reach the site as a
# free parking space.
_PRICE_KEYS = {"parking_naziemne_cena", "parking_podziemne_cena", "komorka_cena"}


def _is_zero_price(value: str) -> bool:
    """True when every amount in the string is zero.

    Only amounts stated in PLN count. Scanning for bare numbers picks up the
    "2" in "PLN/m2" and concludes the value is non-zero, which is how a
    placeholder "od 0,00 PLN/m2 do 0,00 PLN/m2" nearly reached the site as a
    real price.
    """
    amounts = re.findall(r"([\d][\d\s]*(?:[.,]\d+)?)\s*PLN", value)
    if not amounts:
        return False
    return all(float(a.replace(" ", "").replace(",", ".")) == 0 for a in amounts)

def parse_investment_page(page: str) -> dict:
    """Pull the fields the REST API does not carry out of the detail page."""
    out: dict = {
        "extra": {},
        "zasady": {},
        "developer_url": "",
        "documents": [],
        "last_change": "",
        "danegov_sync": "",
        "danegov_price_update": "",
        "contact": {},
    }
    for label, value, href in _page_elements(page):
        low = label.lower()
        if low == "strona internetowa":
            # The anchor text is truncated with an ellipsis; the href is whole.
            out["developer_url"] = href or value
        elif low in _MORE_INFO_MAP and value:
            key = _MORE_INFO_MAP[low]
            if key in _PRICE_KEYS and _is_zero_price(value):
                continue
            out["extra"][key] = value
        elif low == "ostatnia zmiana w expro":
            out["last_change"] = value
        elif low == "synchronizacja z dane.gov.pl":
            out["danegov_sync"] = value
        elif low == "aktualizacja cen dewelopera na dane.gov.pl":
            out["danegov_price_update"] = value

    docs_m = re.search(
        r'<div class="left-col-content investment_documents">(.*?)</div>\s*</div>', page, re.S
    )
    if docs_m:
        block = docs_m.group(1)
        names = [_plain(x) for x in re.findall(r"<p[^>]*>(.*?)</p>", block, re.S)]
        links = re.findall(r'href="([^"]*/document/download/[^"]*)"', block)
        for idx, name in enumerate(names):
            if not name:
                continue
            url = links[idx] if idx < len(links) else ""
            out["documents"].append({"name": name, "url": f"{BASE_URL}{url}" if url else ""})

    # The contact block has no mailto:/tel: links — the values sit in a <p>
    # next to an icon, so the icon class is what identifies them.
    mail = re.search(r"fa-envelope.*?<p[^>]*>(.*?)</p>", page, re.S)
    if mail:
        out["contact"]["email"] = _plain(mail.group(1))
    tel = re.search(r"fa-phone.*?<p[^>]*>(.*?)</p>", page, re.S)
    if tel:
        out["contact"]["phone"] = re.sub(r"\s+", "", _plain(tel.group(1)))

    # Commission terms keep their own proven reader: it resolves 170 of 170
    # investments today and there is nothing to gain from rewriting it here.
    out["zasady"] = parse_zasady(page)
    return out


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

def fetch_investment_page(session: requests.Session, inv_id: str) -> dict:
    url = f"{BASE_URL}/investments/viewdetails/id/{inv_id}/"
    for _ in range(3):
        try:
            resp = session.get(url, timeout=60)
            if resp.status_code == 200:
                return parse_investment_page(resp.text)
        except Exception:
            time.sleep(1)
    return {}


def fill_from_html_concurrent(session: requests.Session, results: list[dict]) -> dict:
    """Merge everything the detail page carries that the REST API does not.

    Must run before the JSON is written: scrape_hash() covers these fields, so
    a changed commission rate or a newly published website has to flip the hash
    and trigger a wp_sync update.

    Nothing here overwrites a value the API already supplied — the page fills
    gaps, it does not arbitrate. The one exception is last_updated_expro, which
    the API cannot answer at all: it offers creation_date, and this page states
    the actual date of last change.
    """
    stats = {k: 0 for k in ("page", "extra", "developer_url", "documents",
                            "zasady", "contact", "last_change")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_investment_page, session, inv["expro_id"]): inv
            for inv in results
        }
        for fut in as_completed(futures):
            inv = futures[fut]
            try:
                page = fut.result()
            except Exception as e:
                log(f"  page ERROR ID={inv['expro_id']}: {e}")
                continue
            if not page:
                continue
            stats["page"] += 1

            filled_extra = False
            for key, value in (page.get("extra") or {}).items():
                if value and not inv["extra"].get(key):
                    inv["extra"][key] = value
                    filled_extra = True
            stats["extra"] += 1 if filled_extra else 0

            if page.get("developer_url") and not inv.get("developer_url"):
                inv["developer_url"] = page["developer_url"]
                stats["developer_url"] += 1
            if page.get("documents") and not inv.get("documents"):
                inv["documents"] = page["documents"]
                stats["documents"] += 1
            if page.get("zasady"):
                inv["zasady_wspolpracy"] = page["zasady"]
                stats["zasady"] += 1
            for key in ("email", "phone"):
                val = (page.get("contact") or {}).get(key)
                if val and not inv["contact"].get(key):
                    inv["contact"][key] = val
                    stats["contact"] += 1
            if page.get("last_change"):
                inv["last_updated_expro"] = page["last_change"]
                stats["last_change"] += 1
            # Useful to the open-data work: when ExPro last synced this
            # investment with dane.gov.pl, and when the developer last
            # published prices there.
            inv["danegov_sync"] = page.get("danegov_sync", "")
            inv["danegov_price_update"] = page.get("danegov_price_update", "")

            inv["scrape_hash"] = scrape_hash(inv)
    return stats

# ---------------------------------------------------------------------------
# Hash (same logic as old scraper for change detection)
# ---------------------------------------------------------------------------

def units_digest(units: list) -> str:
    """Order-independent fingerprint of what every unit costs and whether it is free."""
    parts = sorted(
        f"{u.get('name','')}|{u.get('price_raw','')}|{u.get('status','')}"
        for u in units or []
    )
    return hashlib.md5("\n".join(parts).encode()).hexdigest()


def scrape_hash(inv: dict) -> str:
    inv = dict(inv)
    inv["units_digest"] = units_digest(inv.get("units") or [])
    key = json.dumps(
        {k: inv.get(k) for k in
         ["name", "price_from_raw", "delivery", "units_count",
          "units_available", "zasady_wspolpracy",
          # Added with the HTML pass: without them a developer publishing a
          # website, a new document or a changed lift answer would never
          # reach WordPress, because wp_sync skips on an unchanged hash.
          "extra", "developer_url", "documents",
          # Added with the API fields below. A field that is not in the hash
          # is a field that never reaches WordPress: wp_sync skips any
          # investment whose hash matches, so the first test of these wrote
          # nothing at all.
          "source_name", "count_all", "rating",
          "delivery_to", "developer_id",
          # A digest of every unit's price and status. Without it the hash only
          # moved when investment-level figures did, so a developer repricing a
          # single flat changed nothing wp_sync could see and the unit table on
          # the investment page kept the old number — 179 units were stale that
          # way when this was measured. The digest rather than the units
          # themselves keeps the hash input small and order-independent.
          "units_digest"]},
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
        elif detail.get("_cached_card_urls"):
            card_urls = list(detail["_cached_card_urls"])

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
            # Carried per unit because ExPro states it per unit; every value in
            # the current feed is PLN, but nothing should assume that.
            "currency":     _s(detail.get("currency")),
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
        "zasady_wspolpracy": {},       # filled by fill_from_html_concurrent() (HTML pass)
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
        # ── Fields the API always sent and nothing was reading ──────────────
        # Measured across all 170 before adding: build_year, investment_type
        # and investment_type_id are empty on every single one, so they are
        # deliberately absent rather than carried as dead keys.
        "developer_id":   _s(api_inv.get("developer_id")),
        # Where the listing comes from: a direct contract with the developer
        # (92), the open dane.gov.pl registry (68) or the VoxCrm API (10).
        # That distinction is commercial, not technical.
        "source_name":    _s(api_inv.get("source_name")),
        "count_condo":    _s(api_inv.get("count_condo")),
        "count_utility":  _s(api_inv.get("count_utility")),
        "count_all":      _s(api_inv.get("count_all_realestates")),
        # parking_space_required is deliberately absent: the same fact already
        # arrives from the detail page as extra.parking_obowiazkowy in words,
        # and the API's "0"/"1" reads as empty to every PHP truthiness check
        # the templates use. One fact, one field, in the encoding that cannot
        # be misread.
        "rating":         _s(api_inv.get("rating")),
        "currency":       _s(api_inv.get("currency")),
        # The far end of the completion range. `delivery` above collapses the
        # range to its start, which is what the templates read; this keeps the
        # other end available without changing that.
        "delivery_to":    _s(api_inv.get("completion_date_to")),
        # ExPro's own offer types for this investment (see REALESTATE_TYPES).
        # The classifier's most authoritative signal after a manual override.
        "expro_types":    api_inv.get("_expro_types", []),
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

    # ExPro's own classification, straight from the source rather than guessed
    # from a label. Attached to each investment so build_investment carries it
    # into the JSON without changing its signature.
    log("Fetching ExPro offer-type map …")
    try:
        type_map = fetch_type_map(token)
    except Exception as e:
        log(f"WARNING: type map unavailable ({e}) — classifier falls back to its other signals")
        type_map = {}
    for api_inv in investments:
        api_inv["_expro_types"] = type_map.get(str(api_inv.get("id")), [])
    untyped = sum(1 for i in investments if not i["_expro_types"])
    log(f"  offer types resolved for {len(investments) - untyped}/{len(investments)} investments")

    # Load previous scrape for unit detail cache (speed up repeat runs)
    known_unit_details: dict[str, dict] = {}
    prev_path = Path("data/expro_prev.json")
    # Even a complete cached entry is only as fresh as the day it was first
    # seen — if ExPro replaces a unit's floor plan or corrects its type, a
    # cached unit never notices. EXPRO_IGNORE_CACHE=1 forces a full re-read of
    # every unit detail; worth running periodically, and the workflow cache is
    # restored with a rolling key so nothing else expires it.
    ignore_cache = os.environ.get("EXPRO_IGNORE_CACHE", "").strip().lower() in ("1", "true", "yes")
    if ignore_cache:
        log("EXPRO_IGNORE_CACHE set — re-fetching every unit detail.")
    if prev_path.exists() and not ignore_cache:
        try:
            prev_data = json.loads(prev_path.read_text("utf-8"))
            incomplete = 0
            for prev_inv in prev_data:
                for pu in prev_inv.get("units", []):
                    uid = pu.get("realestate_id", "")
                    if not uid or "-" not in uid:   # UUID format only (new api_scraper output)
                        continue
                    # Only a complete entry earns a place in the cache. This
                    # used to store whatever the previous run ended up with,
                    # including the empty type and empty plan left by a dropped
                    # detail response — and since a cached unit is never
                    # re-fetched, that gap became permanent. 84 units had no
                    # type at all and were classified as flats by default, and
                    # a plan added on ExPro's side after the first sight of a
                    # unit could never appear. An incomplete entry is simply
                    # left out, so the unit is fetched again.
                    # `currency` joins the completeness test deliberately:
                    # entries cached before it existed carry no currency, and
                    # without this they would keep an empty one for good. The
                    # cost is one full re-read, once.
                    if not pu.get("type") or not pu.get("plan_urls") or not pu.get("currency"):
                        incomplete += 1
                        continue
                    known_unit_details[uid] = {
                        "files": {
                            "plan": "",   # URL cached in plan_urls below
                            "card": "",
                        },
                        "stage":           pu.get("stage", ""),
                        "type_name":       pu.get("type", ""),
                        "completion_date": pu.get("delivery", ""),
                        "currency":        pu.get("currency", ""),
                        "_cached_plan_urls":  pu.get("plan_urls", []),
                        "_cached_plan_map":   pu.get("plan_url_map", {}),
                        "_cached_card_urls":  pu.get("photo_urls", []),
                    }
            log(f"Loaded {len(known_unit_details)} cached unit details from prev scrape"
                f" ({incomplete} incomplete → will be re-fetched).")
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
    log("Reading investment detail pages (fields the API does not carry) …")
    st = fill_from_html_concurrent(session, results)
    n = len(results)
    log(f"  pages read: {st['page']}/{n}   commission terms: {st['zasady']}   "
        f"extra filled: {st['extra']}   developer_url: {st['developer_url']}   "
        f"documents: {st['documents']}   contacts: {st['contact']}   "
        f"real last-change date: {st['last_change']}")

    # Health line for the job summary. A unit whose detail never arrived has no
    # type and no plan; it used to be silently classified as a flat and, once
    # cached, stayed that way. Empty types are counted from the result itself,
    # so a regression anywhere upstream shows here too.
    typeless = sum(1 for inv in results for u in inv["units"] if not u.get("type"))
    planless = sum(1 for inv in results for u in inv["units"] if not u.get("plan_urls"))
    total_units = sum(len(inv["units"]) for inv in results)
    log(f"  unit details: {len(_DETAIL_FAILURES)} failed after retries; "
        f"{typeless}/{total_units} units without a type, {planless} without a plan")
    if typeless:
        log("  WARNING: units without a type are classified by default — "
            "check the failures above before trusting projekt_typ")

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
