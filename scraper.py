from typing import Optional, Tuple
"""
ExPro scraper — logs in, scrapes all investments, saves to data/expro_data.json
"""

import json
import re
import hashlib
import os
import sys
import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
try:
    from config import EXPRO, DATA_FILE
except ImportError:
    EXPRO = {
        "base_url": "https://expro.expander.pl",
        "login_url": "https://expro.expander.pl/user/login",
        "username": "",
        "password": "",
    }
    DATA_FILE = "data/expro_data.json"

BASE_URL = EXPRO["base_url"]
LOGIN_URL = EXPRO["login_url"]
USERNAME = EXPRO["username"]
PASSWORD = EXPRO["password"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def extract_field(label: str, text: str, default: str = "") -> str:
    """Extract value that follows a label in page text."""
    pattern = rf"{re.escape(label)}\s*[:\s]*\n?\s*([^\n]+)"
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return default


def parse_price(raw: str) -> Optional[float]:
    """Turn '965 000,00 PLN' → 965000.0"""
    if not raw:
        return None
    # keep only digits, spaces, comma/dot
    cleaned = re.sub(r"[^\d\s,.]", "", raw)
    # remove spaces (thousand separator)
    cleaned = cleaned.replace(" ", "")
    # replace comma decimal separator
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_area(raw: str) -> Tuple[Optional[float], Optional[float]]:
    """'30–120 m²' → (30.0, 120.0)"""
    if not raw:
        return None, None
    nums = re.findall(r"\d+(?:[.,]\d+)?", raw)
    floats = []
    for n in nums:
        try:
            floats.append(float(n.replace(",", ".")))
        except ValueError:
            pass
    if len(floats) >= 2:
        return floats[0], floats[1]
    if len(floats) == 1:
        return floats[0], floats[0]
    return None, None


def scrape_hash(inv: dict) -> str:
    key = json.dumps(
        {k: inv.get(k) for k in ["name", "price_from_raw", "delivery", "units_count", "units_available"]},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page):
    log(f"Logging in as {USERNAME} …")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

    # fill username
    username_sel = 'input[name="username"], input[name="login"], input[type="text"]'
    page.wait_for_selector(username_sel, timeout=10_000)
    page.fill(username_sel, USERNAME)

    # fill password
    page.fill('input[type="password"]', PASSWORD)

    # submit
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("domcontentloaded", timeout=15_000)

    # verify login success
    if "login" in page.url.lower() and "logout" not in page.content().lower():
        raise RuntimeError(f"Login failed — still on login page: {page.url}")
    log("Login successful.")


# ---------------------------------------------------------------------------
# Collect investment IDs from listing pages
# ---------------------------------------------------------------------------

def collect_investment_ids(page) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    page_num = 1

    while True:
        url = f"{BASE_URL}/investments/index/page/{page_num}"
        log(f"  Listing page {page_num}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log(f"  Timeout on listing page {page_num}, stopping.")
            break

        # find links to investment detail pages
        links = page.query_selector_all('a[href*="/investments/viewdetails/id/"]')
        if not links:
            log(f"  No investment links found on page {page_num} — done.")
            break

        found_new = False
        for link in links:
            href = link.get_attribute("href") or ""
            m = re.search(r"/investments/viewdetails/id/(\d+)", href)
            if m:
                inv_id = m.group(1)
                if inv_id not in seen:
                    seen.add(inv_id)
                    ids.append(inv_id)
                    found_new = True

        if not found_new:
            log("  No new IDs on this page — done.")
            break

        page_num += 1

    log(f"Collected {len(ids)} investment IDs across {page_num - 1} listing pages.")
    return ids


# ---------------------------------------------------------------------------
# Scrape single investment detail page
# ---------------------------------------------------------------------------

def scrape_detail(page, inv_id: str) -> Optional[dict]:
    url = f"{BASE_URL}/investments/viewdetails/id/{inv_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except PWTimeout:
        log(f"  Timeout loading {url}")
        return None

    try:
        text = page.inner_text("body")
    except Exception as e:
        log(f"  Could not get body text for {inv_id}: {e}")
        return None

    # ── name & developer ────────────────────────────────────────────────────
    name = ""
    developer = ""

    # Section headings that must NOT be used as the investment name
    GENERIC = {
        "szczegóły inwestycji", "o inwestycji", "projekty", "inwestycje",
        "aktualności", "lokale", "galeria", "mapa", "kontakt", "",
    }

    # Primary: first h3 on the page (investment name is always first h3)
    for el in page.query_selector_all("h3"):
        candidate = el.inner_text().strip()
        if candidate.lower() not in GENERIC and len(candidate) > 2:
            name = candidate
            # Developer is the next sibling text or line after the name in body
            break

    # Extract developer from body text (line right after name)
    if name:
        idx = text.find(name)
        if idx >= 0:
            rest = text[idx + len(name):]
            for line in rest.splitlines():
                line = line.strip()
                if line and line.lower() not in GENERIC and "Zgoda" not in line \
                        and "Historia" not in line and "Publikacja" not in line \
                        and len(line) > 2:
                    developer = line
                    break

    # Fallback: first h2 that isn't a section heading
    if not name:
        for el in page.query_selector_all("h2"):
            candidate = el.inner_text().strip()
            if candidate.lower() not in GENERIC:
                name = candidate
                break

    # Fallback 2: page <title> (only if not generic)
    if not name:
        raw_title = page.title()
        candidate = re.split(r"\s*[-–|]\s*ExPro", raw_title, flags=re.IGNORECASE)[0].strip()
        if candidate.lower() not in GENERIC:
            name = candidate

    # Developer fallback: explicit selectors
    if not developer:
        for sel in [".developer", ".developer-name", '[class*="developer"]', '[class*="deweloper"]']:
            el = page.query_selector(sel)
            if el:
                developer = el.inner_text().strip()
                break

    # Developer fallback: label in text
    if not developer:
        developer = extract_field("Deweloper", text)

    # ── info table fields ────────────────────────────────────────────────────
    street = extract_field("Ulica", text)
    city = extract_field("Miejscowość", text)
    district = extract_field("Dzielnica", text)
    province = extract_field("Województwo", text)
    price_from_raw = extract_field("Cena od", text)
    price_to_raw = extract_field("Cena do", text)
    delivery = extract_field("Termin oddania", text)

    # area
    area_raw = extract_field("Powierzchnia", text)
    area_from, area_to = parse_area(area_raw)

    price_from = parse_price(price_from_raw)

    # ── description table ────────────────────────────────────────────────────
    description_keys = {
        "udogodnienia": ["Udogodnienia", "Wyposażenie"],
        "przynaleznosci": ["Przynależności", "Pomieszczenia"],
        "bezpieczenstwo": ["Bezpieczeństwo"],
        "garaz": ["Garaż", "Parking", "Miejsce postojowe"],
        "komunikacja": ["Komunikacja", "Dojazd"],
        "odleglosc_centrum": ["Odległość od centrum", "Centrum"],
    }
    description: dict[str, str] = {k: "" for k in description_keys}

    # Try to parse table rows
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) >= 2:
            label = cells[0].inner_text().strip().rstrip(":")
            value = cells[1].inner_text().strip()
            for key, labels in description_keys.items():
                if any(l.lower() in label.lower() for l in labels):
                    description[key] = value
                    break

    # Fallback: regex from full text
    for key, labels in description_keys.items():
        if not description[key]:
            for label in labels:
                val = extract_field(label, text)
                if val:
                    description[key] = val
                    break

    # ── images ───────────────────────────────────────────────────────────────
    img_els = page.query_selector_all("img")
    images: list[str] = []
    seen_imgs: set[str] = set()
    for img in img_els:
        src = img.get_attribute("src") or ""
        if "/files/investments/" in src:
            # make absolute
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            if src not in seen_imgs:
                seen_imgs.add(src)
                images.append(src)

    # ── units table ──────────────────────────────────────────────────────────
    units: list[dict] = []
    units_available = 0

    # Find the units table — usually has "Status" header
    tables = page.query_selector_all("table")
    units_table = None
    for tbl in tables:
        header_text = tbl.inner_text()
        if "Status" in header_text and ("Dostępne" in header_text or "Pow" in header_text or "Cena" in header_text):
            units_table = tbl
            break

    if units_table:
        tbl_rows = units_table.query_selector_all("tr")
        headers: list[str] = []
        for i, row in enumerate(tbl_rows):
            # Prefer <th> elements for header detection
            th_els = row.query_selector_all("th")
            if th_els:
                if not headers:
                    headers = [el.inner_text().strip().lower() for el in th_els]
                continue

            cells = row.query_selector_all("td")
            cell_texts = [c.inner_text().strip() for c in cells]
            if not cell_texts:
                continue

            # First <td> row with no headers yet — treat as header fallback
            if not headers and i == 0:
                headers = [t.lower() for t in cell_texts]
                continue

            if not headers:
                continue

            def get_col(name_fragments: list[str]) -> str:
                for frag in name_fragments:
                    for idx, h in enumerate(headers):
                        if frag in h:
                            if idx < len(cell_texts):
                                return cell_texts[idx]
                return ""

            status_val = get_col(["status"])
            unit = {
                "name": get_col(["nazwa", "lokal", "numer"]),
                "status": status_val,
                "stage": get_col(["etap"]),
                "price_raw": get_col(["cena", "price"]) if "m2" not in get_col(["cena"]).lower() else "",
                "price_m2_raw": get_col(["cena m", "m2", "m²"]),
                "area_raw": get_col(["pow", "m²", "m2"]),
                "rooms": get_col(["pok", "room"]),
                "floor": get_col(["piętro", "pietro", "floor"]),
                "delivery": get_col(["termin"]),
                "type": get_col(["typ", "type"]),
            }
            units.append(unit)
            if "dostępne" in status_val.lower():
                units_available += 1

    units_count = len(units)

    # ── developer website URL + extra fields from #collapseMoreInfo ──────────
    developer_url = ""
    extra: dict[str, str] = {
        "miejsce_postojowe": "",
        "komorki_lokatorskie": "",
        "smart_home": "",
        "stacja_ev": "",
        "rodzaje_ochrony": "",
        "wielkosc_projektu": "",
        "winda": "",
        "forma_wlasnosci": "",
        "rodzaj_okien": "",
        "rodzaj_ogrzewania": "",
        "pod_klucz": "",
        "parking_naziemne_cena": "",
        "parking_podziemne_cena": "",
        "komorka_cena": "",
        "pietro_min": "",
        "pietro_max": "",
    }
    more_info_el = page.query_selector("#collapseMoreInfo")
    if more_info_el:
        # Expand if collapsed
        try:
            page.evaluate("document.querySelector('#collapseMoreInfo').classList.add('show')")
        except Exception:
            pass
        more_text = more_info_el.inner_text()
        # Developer URL — get actual href from link, not truncated display text
        for a_el in more_info_el.query_selector_all("a[href^='http']"):
            href = a_el.get_attribute("href") or ""
            if href and "expro.expander.pl" not in href and "google.com" not in href:
                developer_url = href
                break
        # Fallback: regex on text (may be truncated)
        if not developer_url:
            url_m = re.search(r"Strona internetowa[:\s]*(https?://\S+)", more_text, re.IGNORECASE)
            if url_m:
                developer_url = url_m.group(1).strip().rstrip(")")
        # Parse key:value pairs line by line (works for any HTML structure)
        for line in more_text.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            lbl, _, val = line.partition(":")
            lbl_l = lbl.strip().lower()
            val = val.strip()
            if not val:
                continue
            if "miejsce postojowe naziemne" in lbl_l:
                extra["parking_naziemne_cena"] = val
            elif "miejsce postojowe podziemne" in lbl_l:
                extra["parking_podziemne_cena"] = val
            elif "miejsce postojowe" in lbl_l and "obowi" not in lbl_l:
                extra["miejsce_postojowe"] = val
            elif "komórka lokatorska" in lbl_l or "komorka lokatorska" in lbl_l:
                extra["komorka_cena"] = val
            elif "komórk" in lbl_l or "komork" in lbl_l:
                extra["komorki_lokatorskie"] = val
            elif "smart" in lbl_l:
                extra["smart_home"] = val
            elif "ładowania" in lbl_l or "elektryczn" in lbl_l:
                extra["stacja_ev"] = val
            elif "ochrony" in lbl_l or "ochrona" in lbl_l:
                extra["rodzaje_ochrony"] = val
            elif "wielkość projektu" in lbl_l or "wielkosc" in lbl_l:
                extra["wielkosc_projektu"] = val
            elif "winda" in lbl_l:
                extra["winda"] = val
            elif "forma własności" in lbl_l or "forma wlasnosci" in lbl_l:
                extra["forma_wlasnosci"] = val
            elif "rodzaj okien" in lbl_l:
                extra["rodzaj_okien"] = val
            elif "ogrzewania" in lbl_l or "ogrzewanie" in lbl_l:
                extra["rodzaj_ogrzewania"] = val
            elif "pod klucz" in lbl_l or "wykończenia" in lbl_l:
                extra["pod_klucz"] = val
            elif "piętro" in lbl_l and "od" in val.lower():
                nums = re.findall(r"\d+", val)
                if len(nums) >= 2:
                    extra["pietro_min"] = nums[0]
                    extra["pietro_max"] = nums[1]
    # Fallback: extract developer URL from all external links on page
    if not developer_url:
        for a in page.query_selector_all("a[href^='http']"):
            href = a.get_attribute("href") or ""
            if "expro.expander.pl" not in href and "google.com" not in href \
                    and "openstreetmap" not in href and href.startswith("http"):
                developer_url = href
                break

    # ── documents from ExPro ─────────────────────────────────────────────────
    documents: list[dict] = []
    seen_doc_hrefs: set[str] = set()
    for a_el in page.query_selector_all('a[href*="/document/download/"]'):
        doc_href = a_el.get_attribute("href") or ""
        if not doc_href or doc_href in seen_doc_hrefs:
            continue
        seen_doc_hrefs.add(doc_href)
        # Structure: <div><i/><p>NAME</p><a href=...><i/></a></div>
        # Name is in <p> sibling within the same parent div
        doc_name = ""
        parent = a_el.evaluate_handle("el => el.parentElement")
        if parent:
            p_el = parent.query_selector("p")
            if p_el:
                doc_name = p_el.inner_text().strip()
        # Fallback: link's own text (some investments have it differently)
        if not doc_name or len(doc_name) < 2:
            doc_name = a_el.inner_text().strip()
        if not doc_name or len(doc_name) < 2:
            doc_name = f"dokument_{len(documents)+1}"
        full_url = doc_href if doc_href.startswith("http") else BASE_URL + doc_href
        documents.append({
            "name": doc_name,
            "url": full_url,
            "href": doc_href,
        })

    # ── per-lokal realestate_id ───────────────────────────────────────────────
    realestate_pat = re.compile(r"/realestate/index/create-offer-file/realestate_id/(\d+)/")
    if units_table:
        data_rows = [r for r in units_table.query_selector_all("tr") if r.query_selector_all("td")]
        for i, unit in enumerate(units):
            if i < len(data_rows):
                row_html = data_rows[i].inner_html()
                rid_m = realestate_pat.search(row_html)
                if rid_m:
                    unit["realestate_id"] = rid_m.group(1)

    # ── zasady współpracy (commission terms) ─────────────────────────────────
    zasady: dict[str, str] = {}
    zasady_labels = {
        "stawka_standard":   "Stawka standard",
        "stawka_vip":        "Stawka VIP",
        "warunki":           "Warunki wynagrodzenia",
        "garaz_w_prowizji":  "Garaż/komórka wliczana do prowizji",
        "termin_wyplaty":    "Termin wypłaty prowizji (dni)",
        "waznosc_zgloszenia":"Ważność zgłoszenia",
    }
    for key, label in zasady_labels.items():
        val = extract_field(label, text)
        if val:
            zasady[key] = val

    # ── contact ──────────────────────────────────────────────────────────────
    contact: dict[str, str] = {"name": "", "phone": "", "email": ""}
    phone_m = re.search(r"(\+?48[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{3}|\d{3}[\s-]?\d{3}[\s-]?\d{3})", text)
    if phone_m:
        contact["phone"] = phone_m.group(1).strip()
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text)
    if email_m:
        contact["email"] = email_m.group(0)
    for sel in [".contact-name", ".agent-name", '[class*="contact"] strong', '[class*="agent"] strong']:
        el = page.query_selector(sel)
        if el:
            contact["name"] = el.inner_text().strip()
            break

    # ── last updated ─────────────────────────────────────────────────────────
    last_updated_expro = extract_field("Ostatnia zmiana w ExPro", text)

    # ── assemble ─────────────────────────────────────────────────────────────
    inv = {
        "expro_id": inv_id,
        "expro_url": url,
        "name": name or f"Inwestycja {inv_id}",
        "developer": developer,
        "developer_url": developer_url,
        "street": street,
        "city": city,
        "district": district,
        "province": province,
        "price_from_raw": price_from_raw,
        "price_to_raw": price_to_raw,
        "price_from": price_from,
        "area_from": area_from,
        "area_to": area_to,
        "delivery": delivery,
        "units_count": units_count,
        "units_available": units_available,
        "description": description,
        "extra": extra,
        "images": images,
        "contact": contact,
        "units": units,
        "documents": documents,
        "zasady_wspolpracy": zasady,
        "last_updated_expro": last_updated_expro,
        "scrape_hash": "",  # filled below
    }
    inv["scrape_hash"] = scrape_hash(inv)
    return inv


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # Login
        try:
            login(page)
        except Exception as e:
            log(f"FATAL: Login failed — {e}")
            browser.close()
            sys.exit(1)

        # Collect IDs
        log("Collecting investment IDs from listing pages …")
        try:
            inv_ids = collect_investment_ids(page)
        except Exception as e:
            log(f"FATAL: Could not collect investment IDs — {e}")
            browser.close()
            sys.exit(1)

        if not inv_ids:
            log("No investment IDs found. Check login or page structure.")
            browser.close()
            sys.exit(1)

        # Scrape each detail page
        results: list[dict] = []
        total = len(inv_ids)
        for idx, inv_id in enumerate(inv_ids, 1):
            log(f"Scraping {idx}/{total}: ID={inv_id} …")
            try:
                inv = scrape_detail(page, inv_id)
                if inv:
                    results.append(inv)
                    log(f"  OK — {inv['name']}")
                else:
                    log(f"  SKIP — detail page returned None")
            except Exception as e:
                log(f"  ERROR scraping ID={inv_id}: {e}")
                continue

        browser.close()

    # Save
    out_path = DATA_FILE
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    log(f"Saved {len(results)} investments to {out_path}")


if __name__ == "__main__":
    main()
