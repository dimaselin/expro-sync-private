#!/usr/bin/env python3
"""
Sync apartment (Mieszkanie) investments from ExPro to WordPress.
Creates inwestycja posts with projekt_typ=mieszkanie and apartment-specific meta.

Usage:
  python3 mieszkania_sync.py --expro-ids 7567,7161,5758   # test 3
  python3 mieszkania_sync.py --all                         # all 106
"""
import argparse, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, update_meta, import_image_to_wp, log

DATA = Path(__file__).parent / 'data' / 'expro_data.json'
WP_URL = 'https://realsymanagement.pl'

# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_price(raw: str) -> int:
    """Extract first number from price string like '517 355,78 PLN'."""
    m = re.search(r'[\d\s]+', raw.replace(',', '.'))
    if m:
        try:
            return int(float(re.sub(r'\s', '', m.group(0).strip())))
        except:
            pass
    return 0

def format_price_pln(amount: int) -> str:
    if not amount:
        return ''
    return f'{amount:,} PLN'.replace(',', ' ')

def _is_apartment_unit(u: dict) -> bool:
    t = (u.get('type') or u.get('Typ') or '').lower().strip()
    return not t or 'mieszkanie' in t or 'apartament' in t or 'lokal' in t

def derive_apartment_meta(inv: dict) -> dict:
    """Calculate apartment-specific meta from ExPro units."""
    units = [u for u in inv.get('units', []) if _is_apartment_unit(u)]
    if not units:
        units = inv.get('units', [])

    # Area range
    areas = []
    for u in units:
        raw = u.get('area_raw', '').replace(',', '.').replace('m2','').replace('m²','').strip()
        try:
            areas.append(float(raw))
        except:
            pass
    area_min = min(areas) if areas else 0
    area_max = max(areas) if areas else 0
    area_str = f'od {area_min:.0f} do {area_max:.0f} m²' if area_min and area_max else ''

    # Rooms range
    rooms = set()
    for u in units:
        r = str(u.get('rooms', '')).strip()
        if r and r.isdigit():
            rooms.add(int(r))
    if rooms:
        rmin, rmax = min(rooms), max(rooms)
        if rmin == rmax:
            room_str = f'{rmin} {"pokój" if rmin == 1 else "pokoje" if rmin < 5 else "pokoi"}'
        else:
            room_str = f'{rmin}–{rmax} pokoi'
    else:
        room_str = ''

    # Floor range
    floors = set()
    for u in units:
        fl = str(u.get('floor', '')).strip()
        if fl.isdigit():
            floors.add(int(fl))
        elif fl.lower() in ('parter', '0', 'ground'):
            floors.add(0)
    floor_max = max(floors) if floors else 0

    # Cena od
    price_raw = inv.get('price_from_raw', '')
    # Take just "od X PLN" — strip "do ..." part
    price_clean = re.sub(r'\s*do\s+.+', '', price_raw).strip()
    price_clean = re.sub(r'^od\s+', '', price_clean, flags=re.I).strip()

    # Status
    statuses = set(u.get('status', '').lower() for u in units)
    if 'dostępne' in statuses:
        status = 'dostepne'
    elif 'w budowie' in ' '.join(statuses):
        status = 'w_budowie'
    else:
        status = 'w_budowie'

    # Count available
    available = sum(1 for u in units if 'dostępn' in u.get('status','').lower())
    total = len(units)

    return {
        'pow_mieszkalna': area_str,
        'pokoje': room_str,
        'floor_max': floor_max,
        'cena_od': price_clean,
        'status': status,
        'liczba_lokali': str(total),
        'available': available,
    }

def build_projekt_cechy(inv: dict) -> str:
    """Build projekt_cechy JSON for page-inwestycja-mieszkania.php (format: [{title, desc}])."""
    desc = inv.get('description', {})
    cechy = []
    mapping = [
        ('udogodnienia',    'Udogodnienia'),
        ('bezpieczenstwo',  'Bezpieczeństwo'),
        ('komunikacja',     'Komunikacja'),
        ('garaz',           'Garaż / Parking'),
        ('przynaleznosci',  'Przynależności'),
        ('odleglosc_centrum', 'Odległość od centrum'),
    ]
    for key, label in mapping:
        val = desc.get(key, '').strip()
        if val:
            cechy.append({'title': label, 'desc': val})
    return json.dumps(cechy, ensure_ascii=False) if cechy else ''


def units_to_expro_json(units: list) -> str:
    """Convert scraped units to template-compatible format (capitalized keys)."""
    out = []
    for u in units:
        if not _is_apartment_unit(u):
            continue
        floor = str(u.get('floor', ''))
        floor_disp = 'Parter' if floor in ('0', 'parter') else (f'Piętro {floor}' if floor.isdigit() else floor)
        area = u.get('area_raw', '').replace(' m2', ' m²').replace('m2', ' m²')
        price = u.get('price_raw', '').replace(' PLN', ' zł').replace(',00', '')
        price_m2 = u.get('price_m2_raw', '').replace(' PLN/m2', ' zł/m²').replace(',00', '')
        entry = {
            'Nazwa': u.get('name', ''),
            'Piętro': floor_disp,
            'Pokoje': u.get('rooms', ''),
            'Powierzchnia': area,
            'Status': u.get('status', ''),
            'Termin oddania': u.get('delivery', ''),
            'Cena': price,
            'Cena m2': price_m2,
            'realestate_id': u.get('realestate_id', ''),
        }
        out.append(entry)
    return json.dumps(out, ensure_ascii=False)

def get_or_create_post(ssh: SSHClient, expro_id: str, name: str) -> int:
    """Find existing WP post by expro_id, or create new one."""
    try:
        out = ssh.run_wp_cli(
            f'post list --post_type=inwestycja --meta_key=expro_id --meta_value={expro_id} '
            f'--fields=ID --format=csv'
        )
        for line in out.strip().splitlines():
            if line.strip().isdigit():
                return int(line.strip())
    except:
        pass

    # Create new post
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')[:60]
    out = ssh.run_wp_cli(
        f'post create --post_type=inwestycja --post_status=publish '
        f'--post_title={json.dumps(name)} --post_name={json.dumps(slug)} --porcelain'
    )
    post_id = int(out.strip())
    log(f'  Created post {post_id} for {name}')
    return post_id

# ── Main sync ─────────────────────────────────────────────────────────────────

def sync_investment(ssh: SSHClient, inv: dict) -> int:
    expro_id = str(inv['expro_id'])
    name = inv.get('name', f'Inwestycja {expro_id}')
    log(f'\n{"="*60}')
    log(f'Syncing [{expro_id}] {name}')

    post_id = get_or_create_post(ssh, expro_id, name)
    log(f'  Post ID: {post_id}')

    # Derived apartment meta
    apt_meta = derive_apartment_meta(inv)

    # Location
    city = inv.get('city', '')
    district = inv.get('district', '')
    street = inv.get('street', '')
    lokalizacja_parts = [p for p in [street, city] if p]
    lokalizacja = ', '.join(lokalizacja_parts)
    tagline_parts = [p for p in [district, city] if p]
    tagline = ' · '.join(tagline_parts) if tagline_parts else city

    # Delivery + status from delivery text
    delivery_raw = inv.get('delivery', '')
    m_del = re.match(r'od\s+(.+?)\s+do\s+\1$', delivery_raw.strip())
    delivery = m_del.group(1) if m_del else re.sub(r'^od\s+', '', delivery_raw)
    # Override status based on delivery text
    delivery_lc = delivery.lower()
    if re.search(r'oddane|oddano|oddany|gotowe|zamieszkani|zakończ', delivery_lc):
        apt_meta['status'] = 'gotowe'
    elif re.search(r'brak|brak danych|—|nieznany', delivery_lc):
        apt_meta['status'] = 'w_budowie'

    # Cena za m2 from best available unit
    units = inv.get('units', [])
    m2_prices = []
    for u in units:
        raw = u.get('price_m2_raw', '').replace(' PLN/m2', '').replace(',', '.').strip()
        raw = re.sub(r'\s', '', raw)
        try:
            m2_prices.append(float(raw))
        except:
            pass
    cena_m2 = int(sum(m2_prices) / len(m2_prices)) if m2_prices else 0

    meta_fields = [
        ('expro_id',              expro_id),
        ('expro_url',             inv.get('expro_url', '')),
        ('projekt_typ',           'mieszkanie'),
        ('projekt_developer',     inv.get('developer', '')),
        ('projekt_lokalizacja',   lokalizacja),
        ('projekt_tagline',       tagline),
        ('projekt_cena_od',       apt_meta['cena_od']),
        ('projekt_cena_za_m2',    str(cena_m2) if cena_m2 else ''),
        ('projekt_pow_mieszkalna', apt_meta['pow_mieszkalna']),
        ('projekt_pokoje',        apt_meta['pokoje']),
        ('projekt_termin_oddania', delivery),
        ('projekt_liczba_lokali', apt_meta['liczba_lokali']),
        ('projekt_status',        apt_meta['status']),
        ('projekt_lat',           inv.get('lat', '')),
        ('projekt_lng',           inv.get('lng', '')),
        # ExPro extra
        ('expro_winda',           inv.get('extra', {}).get('winda', '')),
        ('expro_parking',         inv.get('extra', {}).get('miejsce_postojowe', '')),
        ('expro_komorki',         inv.get('extra', {}).get('komorki_lokatorskie', '')),
        ('expro_ogrzewanie',      inv.get('extra', {}).get('rodzaj_ogrzewania', '')),
        ('expro_okna',            inv.get('extra', {}).get('rodzaj_okien', '')),
        ('expro_forma_wlasnosci', inv.get('extra', {}).get('forma_wlasnosci', '')),
        ('expro_pod_klucz',       inv.get('extra', {}).get('pod_klucz', '')),
        ('expro_wielkosc',        inv.get('extra', {}).get('wielkosc_projektu', '')),
        ('expro_parking_podziemne_cena', inv.get('extra', {}).get('parking_podziemne_cena', '')),
        ('expro_komorka_cena',    inv.get('extra', {}).get('komorka_cena', '')),
        ('projekt_subdomain_url', inv.get('developer_url', '')),
    ]

    for key, value in meta_fields:
        if value:
            update_meta(ssh, post_id, key, value)

    # Full investment JSON — page-inwestycja-mieszkania.php reads $extra from here
    update_meta(ssh, post_id, 'expro_investment_json', json.dumps(inv, ensure_ascii=False))

    # projekt_cechy JSON for page-inwestycja-mieszkania.php features block
    cechy_json = build_projekt_cechy(inv)
    if cechy_json:
        update_meta(ssh, post_id, 'projekt_cechy', cechy_json)

    # Individual cecha fields (backward compat with single-inwestycja.php)
    desc = inv.get('description', {})
    cecha_defs = [
        ('dashicons-admin-home', 'Udogodnienia',    desc.get('udogodnienia', '')),
        ('dashicons-shield',     'Bezpieczeństwo',  desc.get('bezpieczenstwo', '')),
        ('dashicons-car',        'Komunikacja',     desc.get('komunikacja', '')),
        ('dashicons-admin-home', 'Garaż / Parking', desc.get('garaz', '')),
    ]
    for i, (ikona, tytul, opis) in enumerate(cecha_defs, 1):
        if opis:
            update_meta(ssh, post_id, f'projekt_cecha_{i}_ikona', ikona)
            update_meta(ssh, post_id, f'projekt_cecha_{i}_tytul', tytul)
            update_meta(ssh, post_id, f'projekt_cecha_{i}_opis',  opis)

    # projekt_ogrzewanie (single-inwestycja.php reads this key)
    ogrzewanie = inv.get('extra', {}).get('rodzaj_ogrzewania', '')
    if ogrzewanie:
        update_meta(ssh, post_id, 'projekt_ogrzewanie', ogrzewanie)

    # Units table
    units_json = units_to_expro_json(units)
    update_meta(ssh, post_id, 'expro_lokale_json', units_json)

    # Documents
    docs = inv.get('documents', [])
    if docs:
        update_meta(ssh, post_id, 'expro_dokumenty_json',
                    json.dumps(docs, ensure_ascii=False))

    # Gallery images
    images = inv.get('images', [])
    try:
        existing_gal = ssh.run_wp_cli(f'post meta get {post_id} projekt_galeria').strip()
    except:
        existing_gal = ''

    if images and not existing_gal:
        log(f'  Importing {len(images)} images...')
        img_ids = []
        for url in images:
            aid = import_image_to_wp(ssh, url, post_id)
            if aid:
                img_ids.append(str(aid))
        if img_ids:
            update_meta(ssh, post_id, '_thumbnail_id', img_ids[0])
            update_meta(ssh, post_id, 'projekt_galeria', ','.join(img_ids))
            log(f'  Gallery: {len(img_ids)} images, featured={img_ids[0]}')

    log(f'  ✓ Done: {name} → post {post_id}')
    return post_id

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expro-ids', help='Comma-separated ExPro IDs')
    parser.add_argument('--all', action='store_true', help='Sync all Mieszkanie investments')
    args = parser.parse_args()

    with open(DATA) as f:
        all_data = json.load(f)

    if args.expro_ids:
        ids = set(args.expro_ids.split(','))
        investments = [d for d in all_data if str(d['expro_id']) in ids]
    elif args.all:
        investments = [
            d for d in all_data
            if any(u.get('type') == 'Mieszkanie' for u in d.get('units', []))
        ]
    else:
        parser.print_help()
        return

    log(f'Investments to sync: {len(investments)}')

    ssh = SSHClient()
    ssh._connect()

    synced = []
    for inv in investments:
        pid = sync_investment(ssh, inv)
        if pid:
            synced.append(pid)

    ssh.run_wp_cli('litespeed-purge all')
    ssh.close()

    log(f'\nDone: {len(synced)} synced')
    if synced:
        log(f'Post IDs: {synced}')

if __name__ == '__main__':
    main()
