"""
dane.gov.pl → локальний кеш офіційних цін оферт забудовників.

Крок D1-матчингу з плану «шар відкритих даних». Джерело — ustawa o jawności cen:
з 11.09.2025 кожен забудовник щодня публікує повний прайс. Ліцензія CC0.

ЦЕЙ МОДУЛЬ НІЧОГО НЕ ПИШЕ У WORDPRESS. Він лише тягне, нормалізує й зіставляє,
а рішення що робити з результатом ухвалюється окремо (D3 — валідатор цін,
D4 — латка стелі 100 юнітів).

Використання:
    python3 danegov.py fetch            # каталог + найсвіжіші зрізи → data/danegov/
    python3 danegov.py fetch --limit 20 # те саме, але на пробу
    python3 danegov.py match            # зіставити з нашими інвестиціями
    python3 danegov.py report           # звіт по кешу і матчингу

Що з чим зіставляємо:
    наша інвестиція (expro_data.json: city/street/postal_code/units[].name)
    ↔ група рядків реєстру за адресою «lokalizacji przedsięwzięcia deweloperskiego»

Матчинг за НАЗВОЮ забудовника свідомо не використовується: за законом публікує
юрособа, що продає, а забудовники реєструють окрему SPV на кожну інвестицію —
назва бренду не збігається з назвою в реєстрі (перевірено: збіг 11/19).
Натомість адреса + перетин номерів лока́лів. Перетин номерів заразом і
самоперевірка: якщо номери збігаються, інвестиція та сама напевно.
"""

import json
import os
import re
import sys
import time
import zipfile
import io
import csv
import urllib.parse
import urllib.request
from pathlib import Path
from collections import defaultdict

API = "https://api.dane.gov.pl/1.4"
CITY = "Wrocław"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "danegov"
RAW = CACHE / "raw"
CATALOG = CACHE / "catalog.json"
PARSED = CACHE / "parsed.json"
MAP_FILE = HERE / "data" / "danegov_map.json"
EXPRO = HERE / "data" / "expro_data.json"

UA = {"Accept": "application/json", "User-Agent": "realsy-danegov/1.0"}
SLEEP = 0.25


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def api(path, **params):
    url = f"{API}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as f:
        return json.load(f)


def download(url, dest: Path) -> bool:
    """Качає ресурс у кеш. Повертає False, якщо не вдалося — не падаємо через один файл."""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=90) as f:
            data = f.read()
        if not data:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"      ! завантаження впало: {type(e).__name__}: {str(e)[:80]}")
        return False


# --------------------------------------------------------------------------- #
# Нормалізація тексту й адрес
# --------------------------------------------------------------------------- #

_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def norm(s) -> str:
    s = str(s or "").translate(_PL).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def street_key(s) -> str:
    """Вулиця без типу, без номера — 'ul. Aleja Platanowa 12' → 'platanowa'."""
    s = norm(s)
    s = re.sub(r"\b(ul|ulica|al|aleja|aleje|os|osiedle|pl|plac|generala|gen|swietego|sw)\b", " ", s)
    s = re.sub(r"\b\d+[a-z]?\b", " ", s)          # номери будинків
    s = re.sub(r"\s+", " ", s).strip()
    # Відкидаємо імʼя, лишаємо прізвище: 'marcelego bacciarellego' → 'bacciarellego'
    parts = s.split()
    return parts[-1] if parts else ""


def house_no(s) -> str:
    m = re.search(r"\b(\d+[a-zA-Z]?)\b", str(s or ""))
    return m.group(1).lower() if m else ""


def unit_key(s) -> str:
    """Номер лока́лю до порівнюваного вигляду: 'AP-D-0-01' → 'apd001'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# --------------------------------------------------------------------------- #
# Читання форматів. Схема колонок єдина (статутна), формати різні.
# --------------------------------------------------------------------------- #

def read_csv_bytes(data: bytes):
    for enc in ("utf-8-sig", "utf-8", "cp1250", "iso-8859-2"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    sample = text[:4000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def read_xlsx_bytes(data: bytes):
    """Мінімальний рідер XLSX без зовнішніх залежностей — лише перший аркуш."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return []
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        xml = z.read("xl/sharedStrings.xml").decode("utf8", "replace")
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
            shared.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
    sheets = [n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    if not sheets:
        return []
    xml = z.read(sorted(sheets)[0]).decode("utf8", "replace")

    def unesc(t):
        return (t.replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))

    rows = []
    for rm in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
        cells = {}
        maxi = -1
        for cm in re.finditer(r'<c([^>]*)>(.*?)</c>|<c([^>]*)/>', rm, re.S):
            attrs = cm.group(1) or cm.group(3) or ""
            body = cm.group(2) or ""
            ref = re.search(r'r="([A-Z]+)\d+"', attrs)
            idx = 0
            if ref:
                for ch in ref.group(1):
                    idx = idx * 26 + (ord(ch) - 64)
                idx -= 1
            val = ""
            if 't="s"' in attrs:
                v = re.search(r"<v>(\d+)</v>", body)
                if v and int(v.group(1)) < len(shared):
                    val = shared[int(v.group(1))]
            elif 't="inlineStr"' in attrs:
                val = "".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S))
            else:
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                val = v.group(1) if v else ""
            cells[idx] = unesc(val)
            maxi = max(maxi, idx)
        rows.append([cells.get(i, "") for i in range(maxi + 1)])
    return [r for r in rows if any(str(c).strip() for c in r)]


def read_any(path: Path, fmt: str):
    """
    Повертає (kind, payload):
      ('rows', [[…]])   — таблиця, далі йде через rows_to_records()
      ('recs', [{…}])   — уже готові записи (вкладений JSON)
      ('skip', причина) — не дані (напр. XML-маніфест харвестера)
    """
    data = path.read_bytes()
    head = data[:400].lstrip()

    if head[:5] == b"<?xml" and b"otwarte-dane:harvester" in data[:2000]:
        return "skip", "xml-маніфест харвестера, не дані"
    if head[:1] in (b"[", b"{"):
        recs = read_json_bytes(data)
        return ("recs", recs) if recs else ("skip", "json без розпізнаних юнітів")
    if data[:2] == b"PK":
        rows = read_xlsx_bytes(data)
        if rows:
            return "rows", rows
        return "skip", "xlsx не прочитався"
    rows = read_csv_bytes(data)
    if rows:
        return "rows", rows
    return "skip", "формат не розпізнано"


# --------------------------------------------------------------------------- #
# Мапінг статутних колонок
# --------------------------------------------------------------------------- #

# Кваліфікатор адреси ІНВЕСТИЦІЇ. Відмінок різний у різних забудовників:
# «…lokalizacji przedsięwzięcia deweloperskiego» і «Lokalizacja przedsięwzięcia…».
INW_RE = re.compile(r"lokaliza\w* przedsiewziecia")

# Колонки з цінами, які НЕ є ціною лока́лю: оздоблення, частка в ділянці,
# паркомісце, комірка. Без цього «Wykończenie cena/m2» їде в ціну за метр.
NOT_PRICE = re.compile(
    r"wykonczenie|udzial w gruncie|postojow|garaz|komork|piwnic|dodatkow|"
    r"swiadczenia|oplat|czynsz|zaliczk"
)

# Кастомні (нестатутні) заголовки, які реально трапляються у забудовників.
CUSTOM = {
    "identyfikator_lokalu": "unit_no", "inwestycja": "inw_name",
    "powierzchnia_m2": "area", "cena_brutto": "price", "cena_za_m2": "price_m2",
    "poziom": "floor", "pokoje": "rooms",
}


def map_columns(header):
    """
    Назва колонки → канонічний ключ.

    Пасток дві:
      1. «Miejscowość» зустрічається тричі (сідзіба забудовника / місце продажу /
         локалізація інвестиції) — розрізняє кваліфікатор INW.
      2. Номер юніта має ЩОНАЙМЕНШЕ два статутні написання:
         «Nr lokalu lub domu jednorodzinnego nadany przez dewelopera» і
         «Nr nieruchomości nadany przez dewelopera». Спільне в них —
         `nadany przez dewelopera`, і саме воно відрізняє номер юніта від
         «Nr nieruchomości lokalizacji przedsięwzięcia…», тобто номера будинку.
    """
    out = {}
    for i, h in enumerate(header):
        n = norm(h)
        if not n:
            continue
        if n in CUSTOM:
            out.setdefault(CUSTOM[n], i)
            continue
        is_inw = bool(INW_RE.search(n))
        # Ціна за метр упізнається за «m2» будь-де в назві, а не лише за «cena za m»:
        # трапляється «Cena brutto/m2 powierzchni produktu».
        per_m2 = bool(re.search(r"\bm ?2\b", n))
        has_cena = "cena" in n

        if "nadany przez dewelopera" in n:
            out.setdefault("unit_no", i)
        elif "nazwa dewelopera" in n:
            out.setdefault("dev_name", i)
        elif "nazwa inwestycji" in n:
            out.setdefault("inw_name", i)
        elif re.search(r"\bnr nip\b", n):
            out.setdefault("nip", i)
        elif "nr regon" in n or "nr reggon" in n:      # у частини забудовників друкарська помилка
            out.setdefault("regon", i)
        elif is_inw and "miejscowosc" in n:
            out.setdefault("inw_city", i)
        elif is_inw and "ulica" in n:
            # «ulica i numer nieruchomości» — вулиця й номер в одній колонці.
            out.setdefault("inw_street", i)
            if "numer" in n or "nr" in n:
                out.setdefault("street_has_house", i)
        elif is_inw and "nr nieruchomosci" in n:
            out.setdefault("inw_house", i)
        elif is_inw and "kod pocztowy" in n:
            out.setdefault("inw_postal", i)
        elif "powierzchnia" in n and "dzialki" not in n and not has_cena:
            out.setdefault("area", i)
        elif has_cena and NOT_PRICE.search(n):
            continue                                    # оздоблення, паркомісце тощо — не ціна лока́лю
        elif has_cena and "przed zmiana" in n:
            (out.setdefault("price_prev_m2", i) if per_m2 else out.setdefault("price_prev", i))
        elif has_cena and per_m2:
            out.setdefault("price_m2", i)
        elif has_cena:
            out.setdefault("price", i)
        elif "data" in n and ("aktualizacji" in n or "obowiazuje" in n):
            out.setdefault("date", i)
    return out


def read_json_bytes(data: bytes):
    """
    Вкладений JSON-варіант: список інвестицій, у кожної `products` зі списком юнітів.
    Повертає вже готові записи, повз табличний шлях.
    """
    try:
        doc = json.loads(data.decode("utf8", "replace"))
    except Exception:
        return []
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("items") or [doc]
    if not isinstance(doc, list):
        return []

    recs = []
    for prj in doc:
        if not isinstance(prj, dict):
            continue
        prods = prj.get("products") or prj.get("lokale") or []
        # УВАГА: `address` тут — сідзиба фірми, а НЕ адреса інвестиції. Підставляти
        # її як inw_street не можна: усі проєкти забудовника злипаються в одну адресу
        # (перевірено — 4 880 лока́лів під однією вулицею). Адреси проєкту в цьому
        # форматі немає взагалі, тому єдиний ключ зіставлення — project_name.
        addr = ""
        for u in prods:
            if not isinstance(u, dict):
                continue
            unit = u.get("area_no") or u.get("nr") or u.get("id")
            if not unit:
                continue
            area = num(u.get("area"))
            pm2 = num(u.get("price_s2m") or u.get("price_m2"))
            price = num(u.get("price"))
            if not price and area and pm2:
                price = round(area * pm2, 2)
            recs.append({
                "dev_name": str(prj.get("company_name") or ""),
                "nip": re.sub(r"\D", "", str(prj.get("nip") or "")),
                "inw_name": str(prj.get("project_name") or ""),
                "inw_city": str(prj.get("city") or ""),
                "inw_street": addr,
                "inw_house": house_no(addr),
                "inw_postal": str(prj.get("post") or ""),
                "unit_no": str(unit),
                "area": area,
                "price": price,
                "price_m2": pm2,
                "price_prev": 0.0,
                "status": str(u.get("status") or ""),
                "date": str(u.get("last_price_change") or "")[:10],
            })
    return recs


def num(v):
    s = re.sub(r"[^\d,.\-]", "", str(v or "")).replace(",", ".")
    s = re.sub(r"\.(?=.*\.)", "", s)
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def rows_to_records(rows):
    """Знаходить рядок заголовка (він не завжди перший) і повертає нормалізовані записи."""
    hdr_i, cols = None, {}
    for i, r in enumerate(rows[:12]):
        c = map_columns(r)
        if "unit_no" in c and ("price" in c or "price_m2" in c):
            hdr_i, cols = i, c
            break
    if hdr_i is None:
        return [], {}

    recs = []
    for r in rows[hdr_i + 1:]:
        def g(k):
            i = cols.get(k)
            return (r[i].strip() if i is not None and i < len(r) and r[i] is not None else "")
        unit = g("unit_no")
        if not unit:
            continue
        area = num(g("area"))
        pm2 = num(g("price_m2"))
        price = num(g("price"))
        if not price and area and pm2:
            price = round(area * pm2, 2)
        street = g("inw_street")
        house = g("inw_house")
        if not house and "street_has_house" in cols:
            house = house_no(street)          # «ulica i numer» одним полем
        recs.append({
            "dev_name":   g("dev_name"),
            "nip":        re.sub(r"\D", "", g("nip")),
            "inw_name":   g("inw_name"),
            "inw_city":   g("inw_city"),
            "inw_street": street,
            "inw_house":  house,
            "inw_postal": g("inw_postal"),
            "unit_no":    unit,
            "area":       area,
            "price":      price,
            "price_m2":   pm2,
            "price_prev": num(g("price_prev")),
            "status":     "",
            "date":       g("date"),
        })
    return recs, cols


# --------------------------------------------------------------------------- #
# FETCH
# --------------------------------------------------------------------------- #

def cmd_fetch(limit=None, scope="city"):
    """
    scope='city'  — лише інституції з сідзибою у Вроцлаві (швидко, але дірява вибірка:
                    забудовник може бути зареєстрований у Варшаві й будувати у Вроцлаві)
    scope='all'   — усі забудовники Польщі (~6 600). Довго, зате повно. Резюмується:
                    вже завантажені файли пропускаються.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    done = {p.name.split("_")[0] for p in RAW.glob("*.*")}

    if scope == "all":
        print("1. Усі інституції типу developer …")
        insts, page, seen = [], 1, 0
        while True:
            d = api("institutions", per_page=100, page=page)
            batch = d.get("data", [])
            if not batch:
                break
            seen += len(batch)
            for it in batch:
                a = it["attributes"]
                if a.get("institution_type") != "developer":
                    continue
                insts.append({"id": it["id"], "title": a.get("title", ""),
                              "regon": a.get("regon", ""), "street": a.get("street", "")})
            if seen >= d["meta"]["count"]:
                break
            page += 1
            time.sleep(0.1)
        print(f"   переглянуто {seen}, забудовників: {len(insts)}, "
              f"уже в кеші: {len(done & {i['id'] for i in insts})}")
    else:
        print(f"1. Інституції з city={CITY} …")
        insts, page = [], 1
        while True:
            d = api("institutions", city=CITY, per_page=100, page=page)
            batch = d.get("data", [])
            if not batch:
                break
            for it in batch:
                a = it["attributes"]
                insts.append({"id": it["id"], "title": a.get("title", ""),
                              "regon": a.get("regon", ""), "street": a.get("street", "")})
            if len(insts) >= d["meta"]["count"]:
                break
            page += 1
            time.sleep(SLEEP)
        print(f"   знайдено: {len(insts)}")
    if limit:
        insts = insts[:limit]
        print(f"   обмежено до {len(insts)} (--limit)")

    print("2. Датасети й найсвіжіші зрізи …")
    # Резюмування: наявний каталог зберігаємо, інституції з нього не перезапитуємо.
    catalog = json.loads(CATALOG.read_text(encoding="utf8")) if CATALOG.exists() else []
    have = {c["id"] for c in catalog}
    todo = [x for x in insts if x["id"] not in have]
    print(f"   уже в каталозі: {len(have)}, до обробки: {len(todo)}")
    ok, empty, failed = 0, 0, 0
    for n, ins in enumerate(todo, 1):
        try:
            ds = api(f"institutions/{ins['id']}/datasets", per_page=20)
        except Exception as e:
            print(f"   [{n}/{len(todo)}] {ins['title'][:34]:<34} ! {type(e).__name__}")
            failed += 1
            continue

        target = None
        for it in ds.get("data", []):
            if "ceny ofertowe" in norm(it["attributes"].get("title", "")):
                target = it
                break
        if not target:
            empty += 1
            continue

        try:
            rs = api(f"datasets/{target['id']}/resources", per_page=100, sort="-created")
        except Exception:
            failed += 1
            continue

        res = sorted(
            rs.get("data", []),
            key=lambda r: (r["attributes"].get("created") or ""), reverse=True
        )
        picked = None
        for r in res:
            a = r["attributes"]
            fmt = (a.get("format") or "").lower()
            url = a.get("file_url") or a.get("link") or ""
            if fmt in ("csv", "xlsx", "xls", "json", "xml") and url:
                picked = (r["id"], fmt, url, a.get("created", ""))
                break
        if not picked:
            empty += 1
            continue

        rid, fmt, url, created = picked
        dest = RAW / f"{ins['id']}_{rid}.{fmt}"
        if download(url, dest):
            catalog.append({**ins, "dataset_id": target["id"], "resource_id": rid,
                            "format": fmt, "url": url, "created": created,
                            "file": str(dest.relative_to(HERE)),
                            "resource_count": rs["meta"]["count"]})
            ok += 1
        else:
            failed += 1
        if n % 100 == 0:
            print(f"   [{n}/{len(todo)}]  ok={ok} без_датасету={empty} збій={failed}", flush=True)
            CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf8")
        time.sleep(SLEEP)

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf8")
    print(f"   → {CATALOG.relative_to(HERE)}  (ok={ok}, без датасету={empty}, збій={failed})")

    print("3. Парсинг …")
    cmd_parse()


def cmd_parse(verbose=False):
    """Парсинг того, що вже в кеші. Окремо від fetch, щоб правити рідери без перекачування."""
    if not CATALOG.exists():
        print("Спершу: python3 danegov.py fetch")
        return
    catalog = json.loads(CATALOG.read_text(encoding="utf8"))
    parsed, skipped = {}, defaultdict(int)

    for c in catalog:
        p = HERE / c["file"]
        if not p.exists():
            skipped["файл зник"] += 1
            continue
        try:
            kind, payload = read_any(p, c["format"])
            if kind == "recs":
                recs = payload
            elif kind == "rows":
                recs, _ = rows_to_records(payload)
                if not recs:
                    skipped["таблиця без розпізнаного заголовка"] += 1
            else:
                recs = []
                skipped[payload] += 1
        except Exception as e:
            recs = []
            skipped[f"{type(e).__name__}"] += 1
            if verbose:
                print(f"   ! {p.name}: {e}")
        if recs:
            parsed[c["id"]] = {"inst": c["title"], "format": c["format"],
                               "created": c["created"], "records": recs}

    PARSED.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf8")
    total = sum(len(v["records"]) for v in parsed.values())
    print(f"   → {PARSED.relative_to(HERE)}: {len(parsed)}/{len(catalog)} забудовників, "
          f"{total} лока́лів")
    if skipped:
        print("   не розпарсилось:")
        for k, v in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"     {v:>4}  {k}")


# --------------------------------------------------------------------------- #
# MATCH
# --------------------------------------------------------------------------- #

def cmd_match():
    if not PARSED.exists():
        print("Спершу: python3 danegov.py fetch")
        return
    parsed = json.loads(PARSED.read_text(encoding="utf8"))
    inv = json.loads(EXPRO.read_text(encoding="utf8"))

    # Реєстр → групи «інвестиція». Ключ групування — адреса, а коли її немає
    # (JSON-формат її просто не містить) — назва проєкту.
    groups = defaultdict(lambda: {"units": {}, "meta": {}})
    for inst_id, blob in parsed.items():
        for r in blob["records"]:
            sk = street_key(r["inw_street"])
            key = ((norm(r["inw_city"]), sk, house_no(r["inw_house"])) if sk
                   else ("name", norm(r.get("inw_name")), ""))
            if key[1] == "":
                continue                       # ні адреси, ні назви — зіставляти нічим
            g = groups[key]
            g["units"][unit_key(r["unit_no"])] = r
            if not g["meta"]:
                g["meta"] = {"inst_id": inst_id, "inst": blob["inst"],
                             "dev_name": r["dev_name"], "nip": r["nip"],
                             "inw_name": r.get("inw_name", ""),
                             "city": r["inw_city"], "street": r["inw_street"],
                             "house": r["inw_house"], "postal": r["inw_postal"],
                             "created": blob["created"], "format": blob["format"]}
    n_addr = sum(1 for k in groups if k[0] != "name")
    print(f"реєстр: {len(parsed)} забудовників → {len(groups)} груп "
          f"({n_addr} за адресою, {len(groups)-n_addr} за назвою), "
          f"{sum(len(g['units']) for g in groups.values())} лока́лів\n")

    by_street = defaultdict(list)   # (місто, вулиця) — номер будинку часто відсутній у нас
    by_name = defaultdict(list)     # назва проєкту
    for key, g in groups.items():
        if key[0] == "name":
            by_name[key[1]].append((key, g))
        else:
            by_street[(key[0], key[1])].append((key, g))

    out, stats = [], defaultdict(int)
    for i in inv:
        our_units = {unit_key(u.get("name")) for u in (i.get("units") or []) if u.get("name")}
        ck, sk = norm(i.get("city")), street_key(i.get("street"))
        nk = norm(i.get("name"))
        cands = list(by_street.get((ck, sk), []))
        cands += by_name.get(nk, [])
        # Назва проєкту в реєстрі може бути довшою за нашу («Ślężna Vita» ↔ «Ślężna Vita II»).
        if nk:
            for name_key, lst in by_name.items():
                if name_key != nk and (nk in name_key or name_key in nk):
                    cands += lst

        best, best_ov = None, -1
        for key, g in cands:
            ov = len(our_units & set(g["units"].keys()))
            if ov > best_ov:
                best, best_ov = (key, g), ov

        rec = {
            "expro_id": i.get("expro_id"),
            "name": i.get("name"),
            "city": i.get("city"),
            "street": i.get("street"),
            "our_units": len(our_units),
        }
        if not cands:
            rec["match"] = "none"
            stats["none"] += 1
        else:
            key, g = best
            reg_n = len(g["units"])
            overlap = best_ov
            rec.update({
                "match": "units" if overlap > 0 else "address",
                "confidence": ("high" if overlap >= 3 else "medium" if overlap > 0 else "low"),
                "overlap_units": overlap,
                "registry_units": reg_n,
                "missing_here": reg_n - overlap,     # скільки реєстр знає, а ми ні → ціль D4
                "registry": g["meta"],
            })
            stats[rec["confidence"]] += 1
            if reg_n > len(our_units):
                stats["registry_richer"] += 1
        out.append(rec)

    MAP_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf8")

    tot = len(inv)
    print(f"наших інвестицій: {tot}")
    print(f"  high   (перетин ≥3 лока́лів) : {stats['high']}")
    print(f"  medium (перетин 1–2)        : {stats['medium']}")
    print(f"  low    (тільки адреса)      : {stats['low']}")
    print(f"  не знайдено                 : {stats['none']}")
    print(f"\n  де реєстр багатший за нас  : {stats['registry_richer']}  ← ціль D4")
    print(f"→ {MAP_FILE.relative_to(HERE)}")


def cmd_report():
    if CATALOG.exists():
        c = json.loads(CATALOG.read_text(encoding="utf8"))
        fmts = defaultdict(int)
        for x in c:
            fmts[x["format"]] += 1
        print(f"каталог: {len(c)} забудовників, формати: {dict(fmts)}")
        hist = sorted((x.get("resource_count", 0) for x in c), reverse=True)[:5]
        print(f"  найдовші історії (к-ть щоденних зрізів): {hist}")
    if MAP_FILE.exists():
        m = json.loads(MAP_FILE.read_text(encoding="utf8"))
        good = [x for x in m if x.get("confidence") in ("high", "medium")]
        print(f"\nматчинг: {len(good)}/{len(m)} інвестицій зіставлено")
        for x in sorted(good, key=lambda r: -r.get("missing_here", 0))[:12]:
            print(f"  {str(x['name'])[:38]:<38} наших {x['our_units']:>3} | "
                  f"реєстр {x['registry_units']:>3} | бракує {x['missing_here']:>3} "
                  f"| {x['confidence']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    if cmd == "fetch":
        cmd_fetch(lim, scope="all" if "--all" in sys.argv else "city")
    elif cmd == "parse":
        cmd_parse(verbose="--verbose" in sys.argv)
    elif cmd == "match":
        cmd_match()
    else:
        cmd_report()
