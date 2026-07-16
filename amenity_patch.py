#!/usr/bin/env python3
"""
Phase 2 amenity scraper — GitHub Actions (Ubuntu + Playwright/Chromium).

Reads:  data/expro_data.json   (from api_scraper.py)
Writes: data/amenity_data.json  { uuid: {has_balcony, has_terrace, ...} }

Units already in amenity_data.json are skipped (persistent cache).
mieszkania_sync.py reads amenity_data.json at startup and merges into unit dicts.

Run:
  python3 amenity_patch.py --all
  python3 amenity_patch.py --expro-ids 7567,7563
"""
import argparse, json, os, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
import requests

BASE_URL     = os.environ.get("EXPRO_URL",      "https://expro.expander.pl")
USERNAME     = os.environ.get("EXPRO_USERNAME",  "biuro@realsymanagement.pl")
PASSWORD     = os.environ.get("EXPRO_PASSWORD",  "Firmastart2026")
MAX_WORKERS  = int(os.environ.get("AMENITY_WORKERS", "5"))

DATA_DIR     = Path(__file__).parent / "data"
EXPRO_DATA   = DATA_DIR / "expro_data.json"
AMENITY_DATA = DATA_DIR / "amenity_data.json"

# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def _requests_login() -> requests.Session:
    sess = requests.Session()
    r = sess.get(f"{BASE_URL}/user/login", timeout=15)
    nocsrf = re.search(r'name="nocsrf"\s+value="([^"]+)"', r.text)
    if not nocsrf:
        raise RuntimeError("nocsrf token not found on login page")
    sess.post(
        f"{BASE_URL}/user/login",
        data={"username": USERNAME, "password": PASSWORD, "nocsrf": nocsrf.group(1)},
        allow_redirects=True, timeout=15,
    )
    return sess


def _get_name_to_numeric_id(sess: requests.Session, inv_id: int) -> dict[str, int]:
    """AJAX table → {unit_name: numeric_realestate_id}."""
    url = f"{BASE_URL}/investments/get-realestates-table/id/{inv_id}"
    r = sess.get(url, timeout=30, headers={"X-Requested-With": "XMLHttpRequest"})
    if r.status_code != 200:
        return {}

    result: dict[str, int] = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL):
        rid_m = re.search(r'data-realestate-id="(\d+)"', row_html)
        if not rid_m:
            continue
        numeric_id = int(rid_m.group(1))
        # First non-empty <td> = unit name
        td_m = re.search(r"<td[^>]*>\s*([^\s<][^<]{0,60}?)\s*</td>", row_html)
        if td_m:
            name = re.sub(r"\s+", " ", td_m.group(1)).strip()
            if name and len(name) < 50:
                result[name] = numeric_id
    return result


def _parse_amenities(page_text: str) -> dict:
    """Parse rendered page body text for amenity label:value lines."""
    result: dict = {}
    for line in page_text.splitlines():
        line = line.strip()
        if ":" not in line or len(line) > 300:
            continue
        lbl, _, val = line.partition(":")
        lbl_l = lbl.strip().lower()
        val_s = val.strip()
        val_l = val_s.lower()
        if not val_s or val_l in {"nie", "n/d", "-", "brak", "0", ""}:
            continue
        area_m = re.search(r"([\d,\.]+)\s*m", val_s)

        if "balkon" in lbl_l:
            result["has_balcony"] = True
            if area_m:
                result["balcony_area"] = area_m.group(1).replace(",", ".")
        elif "taras" in lbl_l:
            result["has_terrace"] = True
            if area_m:
                result["terrace_area"] = area_m.group(1).replace(",", ".")
        elif "ogród" in lbl_l or "ogrodek" in lbl_l:
            result["has_garden"] = True
            if area_m:
                result["garden_area"] = area_m.group(1).replace(",", ".")
        elif "garaż" in lbl_l or "garaz" in lbl_l:
            result["has_garage"] = True
        elif "piwnic" in lbl_l or "komórka" in lbl_l or "komorka" in lbl_l:
            result["has_basement"] = True
        elif "sypialn" in lbl_l:
            num_m = re.search(r"\d+", val_s)
            if num_m:
                result.setdefault("bedrooms", num_m.group(0))
        elif "łazienk" in lbl_l or "wc" in lbl_l:
            num_m = re.search(r"\d+", val_s)
            if num_m:
                result.setdefault("bathrooms", num_m.group(0))
    return result


def _playwright_login(browser) -> object:
    """Login and return a new authenticated browser context."""
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = ctx.new_page()
    page.goto(f"{BASE_URL}/user/login", wait_until="domcontentloaded", timeout=20_000)
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    page.close()
    return ctx


def run(target_ids: set[int] | None = None) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    if not EXPRO_DATA.exists():
        log(f"ERROR: {EXPRO_DATA} not found")
        sys.exit(1)

    all_data: list[dict] = json.loads(EXPRO_DATA.read_text("utf-8"))

    cache: dict[str, dict] = {}
    if AMENITY_DATA.exists():
        cache = json.loads(AMENITY_DATA.read_text("utf-8"))

    # Collect pairs needing scraping
    to_scrape: list[tuple[dict, dict]] = []
    for inv in all_data:
        if target_ids and inv.get("expro_id") not in target_ids:
            continue
        for unit in inv.get("units", []):
            uid = unit.get("realestate_id", "")
            if uid and uid not in cache:
                to_scrape.append((inv, unit))

    total_units = sum(
        len(inv.get("units", [])) for inv in all_data
        if not target_ids or inv.get("expro_id") in (target_ids or set())
    )
    log(f"Amenity patch: {len(to_scrape)} to scrape / {total_units} total "
        f"({len(cache)} cached) | workers={MAX_WORKERS}")

    if not to_scrape:
        log("All units already cached.")
        return

    # Pre-build name→numeric_id maps via requests (fast, no browser needed)
    log("Building unit ID maps via AJAX tables …")
    sess = _requests_login()
    id_maps: dict[int, dict[str, int]] = {}
    inv_ids_needed = {inv["expro_id"] for inv, _ in to_scrape}
    for inv_id in sorted(inv_ids_needed):
        id_maps[inv_id] = _get_name_to_numeric_id(sess, inv_id)
        log(f"  inv {inv_id}: {len(id_maps[inv_id])} numeric IDs mapped")

    # Build task list: (inv_name, unit_name, uuid, numeric_id)
    tasks: list[tuple[str, str, str, int]] = []
    skipped_no_id = 0
    for inv, unit in to_scrape:
        uid  = unit["realestate_id"]
        name = unit.get("name", "")
        nid  = id_maps.get(inv["expro_id"], {}).get(name)
        if not nid:
            cache[uid] = {}   # mark attempted, don't retry
            skipped_no_id += 1
        else:
            tasks.append((inv["name"], name, uid, nid))

    if skipped_no_id:
        log(f"  Skipped {skipped_no_id} units — no numeric ID in table")
    log(f"  Ready to scrape: {len(tasks)} units")

    # Thread-safe cache + counter
    cache_lock   = threading.Lock()
    scraped_ok   = 0
    found_amenity = 0
    counter_lock = threading.Lock()

    def _save_cache() -> None:
        AMENITY_DATA.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        # Each worker gets its own authenticated context (isolated session)
        contexts = [_playwright_login(browser) for _ in range(MAX_WORKERS)]
        log(f"  {MAX_WORKERS} browser contexts ready")

        # Round-robin context assignment per thread
        ctx_local = threading.local()
        ctx_idx   = 0
        idx_lock  = threading.Lock()

        def _get_ctx():
            if not hasattr(ctx_local, "ctx"):
                nonlocal ctx_idx
                with idx_lock:
                    ctx_local.ctx = contexts[ctx_idx % MAX_WORKERS]
                    ctx_idx += 1
            return ctx_local.ctx

        def scrape_unit(task: tuple[str, str, str, int]) -> None:
            nonlocal scraped_ok, found_amenity
            inv_name, unit_name, uid, numeric_id = task
            url = f"{BASE_URL}/realestate/view/realestate_id/{numeric_id}/"
            ctx = _get_ctx()
            tab = None
            try:
                tab = ctx.new_page()
                tab.goto(url, wait_until="domcontentloaded", timeout=20_000)
                pg_text = tab.inner_text("body")
                amenities = _parse_amenities(pg_text)
                with cache_lock:
                    cache[uid] = amenities
                with counter_lock:
                    scraped_ok += 1
                    if amenities:
                        found_amenity += 1
                        log(f"  ✓ [{inv_name}] {unit_name}: {amenities}")
                    # Periodic save
                    if scraped_ok % 100 == 0:
                        _save_cache()
                        log(f"  Cache saved ({scraped_ok} scraped)")
            except PwTimeout:
                log(f"  TIMEOUT [{inv_name}] {unit_name} ({numeric_id})")
                with cache_lock:
                    cache[uid] = {}
            except Exception as e:
                log(f"  ERR [{inv_name}] {unit_name}: {e}")
                with cache_lock:
                    cache[uid] = {}
            finally:
                if tab:
                    try:
                        tab.close()
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(scrape_unit, t) for t in tasks]
            for _ in as_completed(futures):
                pass   # exceptions are caught inside scrape_unit

        for ctx in contexts:
            try:
                ctx.close()
            except Exception:
                pass
        browser.close()

    _save_cache()
    log(f"\nDone. Scraped {scraped_ok} units, {found_amenity} had amenity data.")
    log(f"Total cache: {len(cache)} units → {AMENITY_DATA}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--expro-ids", help="Comma-separated ExPro investment IDs")
    args = parser.parse_args()

    if args.expro_ids:
        ids = {int(x.strip()) for x in args.expro_ids.split(",") if x.strip()}
        run(target_ids=ids)
    elif args.all:
        run(target_ids=None)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
