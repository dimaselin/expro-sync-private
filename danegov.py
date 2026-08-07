"""
dane.gov.pl → локальний кеш офіційних цін оферт забудовників.

Крок D1-матчингу з плану «шар відкритих даних». Джерело — ustawa o jawności cen:
з 11.09.2025 кожен забудовник щодня публікує повний прайс. Ліцензія CC0.

ЦЕЙ МОДУЛЬ НІЧОГО НЕ ПИШЕ У WORDPRESS. Він лише тягне, нормалізує й зіставляє,
а рішення що робити з результатом ухвалюється окремо (D3 — валідатор цін,
D4 — латка стелі 100 юнітів).

Використання:
    python3 danegov.py fetch --all      # УСІ забудовники Польщі (~1.5 год, резюмується)
    python3 danegov.py fetch            # лише з сідзибою у Вроцлаві (дірява вибірка)
    python3 danegov.py restale          # викинути несвіжі записи, щоб перебрати їх
    python3 danegov.py parse            # перепарсити кеш, без перекачування
    python3 danegov.py match            # зіставити з нашими інвестиціями
    python3 danegov.py verify           # КРОК D3: наша ціна проти офіційної
    python3 danegov.py report           # звіт по кешу і матчингу

Що з чим зіставляємо:
    наша інвестиція (expro_data.json: city/street/postal_code/units[].name)
    ↔ група рядків реєстру за адресою «lokalizacji przedsięwzięcia deweloperskiego»

Матчинг за НАЗВОЮ забудовника свідомо не використовується: за законом публікує
юрособа, що продає, а забудовники реєструють окрему SPV на кожну інвестицію —
назва бренду не збігається з назвою в реєстрі (перевірено: збіг 11/19).
Натомість адреса + перетин номерів лока́лів. Перетин номерів заразом і
самоперевірка: якщо номери збігаються, інвестиція та сама напевно.

Три уроки, здобуті боляче — не відкочувати:
  1. Схема НЕ єдина. Широкі запасні правила («name», «id», «symbol» → номер юніта)
     дали 21% сміттєвих записів. Тому validate_records() відкидає набори,
     де номери повторюються / без цифр / немає жодної ціни. Менше, зате чисто.
  2. verify успадковує якість матчингу. На medium-зіставленнях порівнюються
     різні будинки. Тому verify працює ТІЛЬКИ по high.
  3. Велике відхилення ≠ помилка ціни. Медіана > 25% означає «зіставлено інший
     обʼєкт», а не «ціна не та».
  4. НАЙВАЖЛИВІШЕ: спершу перевіряй ВІК зрізу. 44% датасетів мають найновіший
     зріз старіший за пів року — забудовник перестав публікувати (часто після
     консолідації SPV). Порівняння сьогоднішньої ціни з торішньою показує
     інфляцію, а не дефект. Без гейта свіжості я «знайшов» 106 розбіжностей;
     з гейтом їх виявилась ОДНА на 200 юнітів. Те саме стосується D4:
     імпорт юнітів зі зрізу піврічної давнини заллє застарілий інвентар.
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


def read_spreadsheetml(data: bytes):
    """Excel 2003 XML (<Workbook>…<Row><Cell><Data>) → рядки, далі табличним шляхом."""
    x = data.decode("utf8", "replace")
    rows = []
    for rm in re.findall(r"<Row[^>]*>(.*?)</Row>", x, re.S):
        cells, idx = {}, 0
        for cm in re.finditer(r"<Cell([^>]*)>(.*?)</Cell>|<Cell([^>]*)/>", rm, re.S):
            attrs = cm.group(1) or cm.group(3) or ""
            ix = re.search(r'ss:Index="(\d+)"', attrs)
            if ix:
                idx = int(ix.group(1)) - 1
            body = cm.group(2) or ""
            val = "".join(re.findall(r"<Data[^>]*>(.*?)</Data>", body, re.S))
            val = re.sub(r"<[^>]+>", "", val)
            cells[idx] = (val.replace("&lt;", "<").replace("&gt;", ">")
                             .replace("&quot;", '"').replace("&amp;", "&").strip())
            idx += 1
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return [r for r in rows if any(str(c).strip() for c in r)]


def read_native_xml(data: bytes):
    """
    Нативний <ceny_nieruchomosci>: <meta> з даними забудовника й адресами,
    <nieruchomosci><nieruchomosc> зі списком лока́лів.
    Адреса інвестиції — той блок у <meta>, чий тег згадує локалізацію/інвестицію.
    """
    x = data.decode("utf8", "replace")

    def tag(scope, name):
        m = re.search(rf"<{name}>(.*?)</{name}>", scope, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    meta = re.search(r"<meta>(.*?)</meta>", x, re.S)
    meta = meta.group(1) if meta else ""
    dev_name = tag(meta, "nazwa")
    nip = re.sub(r"\D", "", tag(meta, "nip"))

    inw = ""
    for bm in re.finditer(r"<([a-z_]*(?:lokaliza|inwestyc|przedsiewz)[a-z_]*)>(.*?)</\1>", meta, re.S):
        inw = bm.group(2)
        break
    if not inw:                                    # запасний варіант — адреса продажу
        m = re.search(r"<adres_sprzedazy>(.*?)</adres_sprzedazy>", meta, re.S)
        inw = m.group(1) if m else ""

    city = tag(inw, "miejscowosc")
    street = tag(inw, "ulica")
    house = tag(inw, "nr_nieruchomosci") or tag(inw, "nr_lokalu")
    postal = tag(inw, "kod_pocztowy")

    recs = []
    for um in re.finditer(r"<nieruchomosc>(.*?)</nieruchomosc>", x, re.S):
        u = um.group(1)
        unit = tag(u, "numer") or tag(u, "oznaczenie") or tag(u, "nr_lokalu")
        if not unit:
            continue
        area = num(tag(u, "powierzchnia") or tag(u, "powierzchnia_uzytkowa"))
        pm2 = num(tag(u, "cena_m2"))
        price = num(tag(u, "cena"))
        if not price and area and pm2:
            price = round(area * pm2, 2)
        recs.append({
            "dev_name": dev_name, "nip": nip, "inw_name": tag(meta, "nazwa_inwestycji"),
            "inw_city": city, "inw_street": street,
            "inw_house": house or house_no(street), "inw_postal": postal,
            "unit_no": unit, "area": area, "price": price, "price_m2": pm2,
            "price_prev": 0.0, "status": tag(u, "status"),
            "date": tag(u, "data_obowiazywania_ceny")[:10],
        })
    return recs


def read_any(path: Path, fmt: str):
    """
    Повертає (kind, payload):
      ('rows', [[…]])   — таблиця, далі йде через rows_to_records()
      ('recs', [{…}])   — уже готові записи (вкладений JSON)
      ('skip', причина) — не дані (напр. XML-маніфест харвестера)
    """
    data = path.read_bytes()
    head = data[:400].lstrip()

    if head[:5] == b"<?xml" or head[:1] == b"<":
        if b"otwarte-dane:harvester" in data[:2000]:
            return "skip", "xml-маніфест харвестера, не дані"
        if b"office:spreadsheet" in data[:1500] or b"<Workbook" in data[:1500]:
            rows = read_spreadsheetml(data)
            return ("rows", rows) if rows else ("skip", "spreadsheetml без рядків")
        if b"<nieruchomosc" in data[:200000] or b"ceny_nieruchomosci" in data[:1000]:
            recs = read_native_xml(data)
            return ("recs", recs) if recs else ("skip", "xml без розпізнаних лока́лів")
        return "skip", "xml невідомої структури"
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

# Кваліфікатор адреси ІНВЕСТИЦІЇ. Написання різні: «…lokalizacji przedsięwzięcia
# deweloperskiego», «Lokalizacja przedsięwzięcia…», «…lokalizacji inwestycji».
INW_RE = re.compile(r"lokaliza\w* (?:przedsiewziecia|inwestycji)")

# Номер лока́лю, наданий забудовником. Написань щонайменше пʼять.
UNIT_RE = re.compile(
    r"nadany przez dewelopera|nr lokalu dewelopera|oznaczenie lokalu|"
    r"^identyfikator lokalu$|^nr lokalu$"
)

# Колонка-ДАТА. Мусить перевірятись ПЕРШОЮ: «Data od której obowiązuje cena lokalu»
# містить слово «cena» і без цього чека їде в ціну.
DATE_RE = re.compile(r"^data\b|data od ktorej|obowiazuje od|^cena obowiazuje")

# «Cena lokalu (m2 * powierzchnia)» — це ПОВНА ціна, у назві якої описана формула.
# Без цього правила вона розпізнається як ціна за метр.
PRICE_FORMULA_RE = re.compile(r"m ?2 ?\*|\* ?m ?2|m ?2 powierzchnia|powierzchnia ?\* ?m ?2")

# Колонки з цінами, які НЕ є ціною лока́лю: оздоблення, частка в ділянці,
# паркомісце, комірка. Без цього «Wykończenie cena/m2» їде в ціну за метр.
NOT_PRICE = re.compile(
    r"wykonczenie|udzial w gruncie|postojow|garaz|komork|piwnic|dodatkow|"
    r"swiadczenia|oplat|czynsz|zaliczk"
)

# Кастомні (нестатутні) заголовки, які реально трапляються у забудовників.
CUSTOM = {
    # snake_case варіант
    "identyfikator lokalu": "unit_no", "inwestycja": "inw_name",
    "powierzchnia m2": "area", "cena brutto": "price", "cena za m2": "price_m2",
    "cena mkw": "price_m2", "poziom": "floor", "pokoje": "rooms",
    # мінімалістичний варіант
    "lokal": "unit_no", "cena": "price",
    # англомовний варіант (Value.*)
    "value gross": "price", "value areagross": "area",
    "value investmentname": "inw_name", "value productname": "inw_name",
    "value date": "date",
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
        # ДАТИ — першими. «Data od której obowiązuje cena lokalu» містить «cena»,
        # і без цього чека колонка з датою розпізнається як ціна.
        if DATE_RE.search(n):
            out.setdefault("date", i)
            continue
        is_inw = bool(INW_RE.search(n))
        # Ціна за метр упізнається за «m2» будь-де в назві, а не лише за «cena za m»:
        # трапляється «Cena brutto/m2 powierzchni produktu».
        per_m2 = bool(re.search(r"\bm ?2\b", n)) and not PRICE_FORMULA_RE.search(n)
        has_cena = "cena" in n

        if UNIT_RE.search(n):
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

    # Запасний ключ юніта — лише ВУЗЬКИЙ список. Свідомо без загальних 'id', 'name',
    # 'symbol': вони підхоплюють колонки-прапорці й дають сміття (перевірено —
    # 36 906 записів з номером «X» на один прогін). Краще менше, але чисто.
    if "unit_no" not in out:
        for i, h in enumerate(header):
            if norm(h) in ("id nieruchomosci", "id lokalu", "numer lokalu",
                           "nr mieszkania", "oznaczenie nieruchomosci"):
                out["unit_no"] = i
                break
    return out


# Значення, які точно не є номером лока́лю.
JUNK_UNIT = {"x", "prices", "cena", "lokal", "nazwa", "suma", "razem", "name", "id",
             "-", "brak", "n a", "nd", "m", "st post", "lokal mieszkalny", "dom",
             "mieszkanie", "razem suma", "total"}


def validate_records(recs, src=""):
    """
    Відсіює набори, де колонка «номер юніта» насправді не номер.
    Три сигнали, кожен сам собою достатній:
      1. надто багато однакових значень — це прапорець, а не ідентифікатор
      2. більшість значень без жодної цифри
      3. жодної ціни в наборі
    Повертає (records, причина_відкидання або None).
    """
    if not recs:
        return [], "порожньо"

    kept = [r for r in recs
            if norm(r["unit_no"]) not in JUNK_UNIT and len(str(r["unit_no"]).strip()) <= 28]
    if not kept:
        return [], "усі номери лока́лів — сміття"

    vals = [str(r["unit_no"]).strip() for r in kept]
    uniq = len(set(vals))
    if uniq <= 1 and len(vals) > 3:
        return [], "номер лока́лю однаковий у всіх рядках"
    if uniq / len(vals) < 0.6 and len(vals) > 10:
        return [], f"номери лока́лів повторюються ({uniq} унікальних на {len(vals)})"

    with_digit = sum(1 for v in vals if re.search(r"\d", v))
    if with_digit / len(vals) < 0.5:
        return [], "більшість номерів без цифр"

    if not any(r["price"] or r["price_m2"] for r in kept):
        return [], "жодної ціни в наборі"

    return kept, None


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

        # У однієї інституції буває КІЛЬКА датасетів «Ceny ofertowe»: старий закинутий
        # і поточний. Брати перший-ліпший не можна — саме через це половина кешу
        # виявилась торішньою (Archicom Nieruchomości 9: ds 4971 замер на 11.09.2025,
        # а ds 11076 живий і має 308 зрізів). Обираємо той, чий НАЙНОВІШИЙ ресурс свіжіший.
        cands = [it for it in ds.get("data", [])
                 if "ceny ofertowe" in norm(it["attributes"].get("title", ""))]
        if not cands:
            empty += 1
            continue

        best, best_res, best_date = None, None, ""
        for it in cands:
            try:
                rs = api(f"datasets/{it['id']}/resources", per_page=100, sort="-created")
            except Exception:
                continue
            rl = sorted(rs.get("data", []),
                        key=lambda r: (r["attributes"].get("created") or ""), reverse=True)
            top = (rl[0]["attributes"].get("created") or "") if rl else ""
            if top > best_date:
                best, best_res, best_date = it, rl, top
            time.sleep(0.05)
        if not best:
            failed += 1
            continue
        target, res = best, best_res
        rs = {"meta": {"count": len(best_res)}}
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


def cmd_restale(max_age=45):
    """
    Викидає з каталогу записи з несвіжим зрізом, щоб `fetch --all` перебрав саме їх
    уже з виправленим вибором датасету (найсвіжіший, а не перший-ліпший).
    Свіжі записи й їхні файли не чіпаються — обхід резюмується.
    """
    import datetime as _dt
    if not CATALOG.exists():
        print("Немає каталогу.")
        return
    cat = json.loads(CATALOG.read_text(encoding="utf8"))
    now = _dt.datetime.now(_dt.timezone.utc)
    keep, drop = [], []
    for c in cat:
        created = (c.get("created") or "")[:19]
        age = None
        if created:
            try:
                age = (now - _dt.datetime.fromisoformat(created).replace(
                    tzinfo=_dt.timezone.utc)).days
            except ValueError:
                age = None
        (keep if (age is not None and age <= max_age) else drop).append(c)

    for c in drop:
        p = HERE / c["file"]
        if p.exists():
            p.unlink()
    CATALOG.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf8")
    print(f"каталог: було {len(cat)} → лишилось {len(keep)} свіжих, "
          f"викинуто {len(drop)} несвіжих (їх переберемо заново)")


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
            if recs:
                recs, why = validate_records(recs, p.name)
                if why:
                    skipped["відкинуто якістю: " + why.split("(")[0].strip()] += 1
                    if verbose:
                        print(f"   ~ {p.name}: {why}")
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


# --------------------------------------------------------------------------- #
# VERIFY (крок D3) — наша ціна на сайті проти офіційно опублікованої
# --------------------------------------------------------------------------- #

OUR_UNITS = CACHE / "our_units.json"


def fetch_our_units(refresh=False):
    """
    Живі ціни з сайту. Номер лока́лю лежить у post_title до « — »:
        «A.1.1 — Elewator - Mieszkania i Lofty»
    Кешується, бо для повторного порівняння SSH не потрібен.
    """
    if OUR_UNITS.exists() and not refresh:
        return json.loads(OUR_UNITS.read_text(encoding="utf8"))
    try:
        import paramiko
        from config import SSH, WP
    except ImportError as e:
        print(f"   ! потрібен paramiko і config.py: {e}")
        return {}

    sql = (
        "SELECT i.meta_value AS expro_id, p.ID, p.post_title, "
        "MAX(CASE WHEN m.meta_key='fave_property_price' THEN m.meta_value END) AS price, "
        "MAX(CASE WHEN m.meta_key='lokal_cena_m2' THEN m.meta_value END) AS m2, "
        "MAX(CASE WHEN m.meta_key='lokal_status_expro' THEN m.meta_value END) AS status "
        "FROM k5ew_posts p "
        "JOIN k5ew_postmeta m ON m.post_id = p.ID "
        "JOIN k5ew_postmeta link ON link.post_id = p.ID AND link.meta_key='fave_property_project_id' "
        "JOIN k5ew_postmeta i ON i.post_id = link.meta_value AND i.meta_key='expro_id' "
        "WHERE p.post_type='property' AND p.post_status='publish' "
        "GROUP BY p.ID"
    )
    cmd = f"cd {WP['path']} && wp db query \"{sql}\" --skip-column-names"
    print("   тягну живі ціни з сайту …")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(SSH["host"], port=SSH["port"], username=SSH["username"],
              password=SSH["password"], timeout=45)
    _, out, err = c.exec_command(cmd, timeout=300)
    text = out.read().decode("utf8", "replace")
    e = err.read().decode("utf8", "replace")
    c.close()
    if e.strip() and not text.strip():
        print("   ! " + e[:200])
        return {}

    by_inv = defaultdict(dict)
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        expro_id, pid, title, price, m2, status = parts[:6]
        unit = title.split("—")[0].strip() or title.strip()
        by_inv[expro_id][unit_key(unit)] = {
            "wp_id": int(pid), "unit_no": unit,
            "price": num(price), "price_m2": num(m2), "status": status,
        }
    OUR_UNITS.parent.mkdir(parents=True, exist_ok=True)
    OUR_UNITS.write_text(json.dumps(by_inv, ensure_ascii=False), encoding="utf8")
    print(f"   → {sum(len(v) for v in by_inv.values())} юнітів по {len(by_inv)} інвестиціях")
    return by_inv


MAX_SNAPSHOT_AGE_DAYS = 45


def cmd_verify(refresh=False, tol_pct=1.0, tol_abs=1000.0, max_age=MAX_SNAPSHOT_AGE_DAYS):
    """
    Порівнює нашу ціну з офіційно опублікованою по зіставлених інвестиціях.
    Розбіжністю вважається відхилення більше за ОБИДВА пороги — щоб копійчані
    округлення й курсові дрібниці не сипали шумом.

    ⚠ ГЕЙТ СВІЖОСТІ — найважливіше в цій функції.
    Частина забудовників перестала публікувати (напр. після консолідації SPV):
    у їхньому датасеті найновіший зріз може бути піврічної давнини. Порівняння
    нашої сьогоднішньої ціни з торішньою показує не дефект, а інфляцію.
    Перевірено боляче: усі «розбіжності» першого прогону (+10% Atrium,
    −8.8% Legnicka Vita, −9% Traugutta) виявились саме цим — зрізи були
    з вересня-жовтня 2025. Зі свіжих зрізів розбіжностей майже немає.
    """
    import datetime as _dt
    if not MAP_FILE.exists() or not PARSED.exists():
        print("Спершу: python3 danegov.py fetch --all && python3 danegov.py match")
        return
    mp = json.loads(MAP_FILE.read_text(encoding="utf8"))
    parsed = json.loads(PARSED.read_text(encoding="utf8"))
    ours = fetch_our_units(refresh)
    if not ours:
        return

    # реєстрові юніти згруповані так само, як у match()
    groups = defaultdict(dict)
    for blob in parsed.values():
        for r in blob["records"]:
            sk = street_key(r["inw_street"])
            key = ((norm(r["inw_city"]), sk, house_no(r["inw_house"])) if sk
                   else ("name", norm(r.get("inw_name")), ""))
            groups[key][unit_key(r["unit_no"])] = r

    report, suspect, stale, tot = [], [], [], defaultdict(int)
    now = _dt.datetime.now(_dt.timezone.utc)
    for x in mp:
        # Тільки high. На medium перетин юнітів 1–2, і порівняння цін тоді
        # порівнює нас із чужим будинком — перевірено на «Osiedle Zielony Zakątek»,
        # де «офіційна ціна» 25 000 приїхала з іншої інвестиції.
        if x.get("confidence") != "high":
            continue
        reg_meta = x.get("registry") or {}
        sk = street_key(reg_meta.get("street"))
        key = ((norm(reg_meta.get("city")), sk, house_no(reg_meta.get("house"))) if sk
               else ("name", norm(reg_meta.get("inw_name")), ""))
        reg = groups.get(key, {})
        mine = ours.get(str(x["expro_id"]), {})
        if not reg or not mine:
            continue

        # Гейт свіжості — до будь-яких порівнянь.
        created = (reg_meta.get("created") or "")[:19]
        age = None
        if created:
            try:
                age = (now - _dt.datetime.fromisoformat(created).replace(
                    tzinfo=_dt.timezone.utc)).days
            except ValueError:
                age = None
        if age is None or age > max_age:
            stale.append({"name": x["name"], "expro_id": x["expro_id"],
                          "snapshot": created[:10] or "?", "age_days": age,
                          "registry_units": x.get("registry_units")})
            tot["stale_skipped"] += 1
            continue

        diffs, same, only_ours, only_reg = [], 0, 0, 0
        for uk, u in mine.items():
            r = reg.get(uk)
            if not r:
                only_ours += 1
                continue
            a, b = u["price"], r["price"]
            if a <= 0 or b <= 0:
                continue
            d = a - b
            if abs(d) > tol_abs and abs(d) / b * 100 > tol_pct:
                diffs.append({"unit": u["unit_no"], "wp_id": u["wp_id"],
                              "ours": a, "official": b, "delta": round(d, 2),
                              "pct": round(d / b * 100, 2)})
            else:
                same += 1
        only_reg = len([k for k in reg if k not in mine])

        # Санітарна перевірка ЗІСТАВЛЕННЯ, а не цін: якщо розбігається більшість
        # юнітів або медіанне відхилення величезне — це майже напевно не наші ціни
        # помилкові, а зіставлено інший будинок. Такі не звітуємо як цінові дефекти.
        compared = same + len(diffs)
        drift = None
        if compared >= 4 and diffs:
            share = len(diffs) / compared
            pcts = sorted(abs(d["pct"]) for d in diffs)
            med = pcts[len(pcts) // 2]
            # Порядок величини розрізняє два різні явища:
            #   медіана > 25%  — зіставлено не той будинок (бачили 1453%)
            #   медіана мала, але розбігається більшість — ціни справді
            #     системно поїхали по всій інвестиції. Це НЕ шум, це знахідка.
            if med > 25:
                suspect.append({
                    "name": x["name"], "expro_id": x["expro_id"],
                    "compared": compared, "differing": len(diffs),
                    "median_abs_pct": med,
                    "why": "порядок цін не збігається — схоже, зіставлено інший обʼєкт",
                    "registry": reg_meta,
                })
                tot["suspect_match"] += 1
                continue
            if share > 0.6:
                sign = "вище" if sum(d["delta"] for d in diffs) > 0 else "нижче"
                drift = {"share": round(share * 100), "median_pct": med, "direction": sign}
                tot["systematic_drift"] += 1

        tot["investments"] += 1
        tot["compared"] += compared
        tot["diff"] += len(diffs)
        tot["only_ours"] += only_ours
        tot["only_reg"] += only_reg
        if diffs or only_ours:
            report.append({
                "name": x["name"], "expro_id": x["expro_id"],
                "confidence": x["confidence"],
                "compared": same + len(diffs), "identical": same,
                "differing": len(diffs),
                "only_on_our_site": only_ours, "only_in_registry": only_reg,
                "systematic_drift": drift,
                "worst": sorted(diffs, key=lambda d: -abs(d["delta"]))[:10],
            })

    out = HERE / "data" / "danegov_verify.json"
    out.write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "tolerance": {"pct": tol_pct, "abs": tol_abs},
         "max_snapshot_age_days": max_age,
         "totals": dict(tot), "investments": report,
         "suspect_matches": suspect, "stale_snapshots": stale},
        ensure_ascii=False, indent=1), encoding="utf8")

    print(f"\n=== D3: звірка цін (зрізи не старші за {max_age} дн.) ===")
    print(f"  інвестицій звірено : {tot['investments']}")
    print(f"  юнітів звірено     : {tot['compared']}")
    print(f"  ціна збігається    : {tot['compared'] - tot['diff']}")
    print(f"  РОЗБІЖНОСТЕЙ       : {tot['diff']}")
    print(f"  є в нас, нема в реєстрі : {tot['only_ours']}")
    print(f"  є в реєстрі, нема в нас : {tot['only_reg']}   ← ціль D4")
    if stale:
        print(f"\n  ⊘ пропущено — забудовник давно не публікує: {len(stale)}")
        for z in sorted(stale, key=lambda q: -(q['age_days'] or 0))[:8]:
            print(f"     {z['name'][:38]:<38} зріз {z['snapshot']} ({z['age_days']} дн. тому)")
    if suspect:
        print(f"\n  ⚠ відкинуто як хибне ЗІСТАВЛЕННЯ (не цінова помилка): {len(suspect)}")
        for s_ in suspect[:6]:
            print(f"     {s_['name'][:38]:<38} {s_['differing']}/{s_['compared']} "
                  f"розбіг, медіана {s_['median_abs_pct']:.0f}% — {s_['why']}")
    for r in sorted(report, key=lambda z: -z["differing"])[:8]:
        if not r["differing"]:
            continue
        d = r.get("systematic_drift")
        tagline = (f"  ⇢ СИСТЕМНИЙ ЗСУВ: {d['share']}% юнітів, медіана {d['median_pct']:.1f}% "
                   f"{d['direction']} за офіційну") if d else ""
        print(f"\n  {r['name'][:40]} — {r['differing']} з {r['compared']}{tagline}")
        for d in r["worst"][:3]:
            print(f"     {d['unit']:<14} наша {d['ours']:>12,.0f} | офіційна {d['official']:>12,.0f}"
                  f" | {d['pct']:+.1f}%".replace(",", " "))
    print(f"\n→ {out.relative_to(HERE)}")


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
    elif cmd == "restale":
        cmd_restale()
    elif cmd == "parse":
        cmd_parse(verbose="--verbose" in sys.argv)
    elif cmd == "match":
        cmd_match()
    elif cmd == "verify":
        cmd_verify(refresh="--refresh" in sys.argv)
    else:
        cmd_report()
