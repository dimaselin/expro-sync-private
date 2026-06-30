#!/usr/bin/env python3
"""
Mieszkania sync — creates/updates individual `property` WP posts for each ExPro unit.
Each apartment/house unit from ExPro becomes one property post in Houzez.

Usage:
  python3 mieszkania_sync.py --all
  python3 mieszkania_sync.py --expro-ids 7567,7563
"""
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

DATA = Path(__file__).parent / 'data' / 'expro_data.json'

# ── Taxonomy term IDs (verified on site) ─────────────────────────────────────
LABEL_RYNEK_PIERWOTNY = 181   # property_label: Rynek Pierwotny

STATUS_SLUG_MAP = {
    'dostępne':      'wolny',
    'dostepne':      'wolny',
    'wolne':         'wolny',
    'zarezerwowane': 'zarezerwowany',
    'rezerwacja':    'zarezerwowany',
    'sprzedane':     'sprzedany',
    'sprzedany':     'sprzedany',
}

TYPE_SLUG_MAP = {
    'mieszkanie':    'mieszkanie',
    'apartament':    'mieszkanie',
    'dom szereg':    'dom-szeregowy',
    'blizniak':      'blizniak',
    'dom wolno':     'dom-wolnostojacy',
    'lokal':         'lokal-uzytkowy',
    'biuro':         'lokal-uzytkowy',
    'garaz':         'lokal-uzytkowy',
    'komercja':      'komercja',
}

# ── PHP template executed via eval-file ──────────────────────────────────────

PHP_SYNC_UNITS = r"""<?php
$raw  = file_get_contents('/tmp/esm_units_data.json');
$data = json_decode($raw, true);
if (!$data) { echo json_encode(['error'=>'bad json']); exit; }

$units         = $data['units'];
$inv           = $data['inv'];
$inv_extra     = $data['inv_extra'] ?? [];
$parent_id     = (int)$data['parent_id'];
$projekt_tid   = (int)$data['projekt_term_id'];
$label_tid     = 181; // Rynek Pierwotny

$status_map = [
    'dostępne'      => 'wolny',
    'dostepne'      => 'wolny',
    'wolne'         => 'wolny',
    'zarezerwowane' => 'zarezerwowany',
    'rezerwacja'    => 'zarezerwowany',
    'sprzedane'     => 'sprzedany',
    'sprzedany'     => 'sprzedany',
];

// Ensure media functions are available once
if (!function_exists('media_sideload_image')) {
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';
}

$created = $updated = $skipped = 0;
$errors  = [];

foreach ($units as $u) {
    $uid = trim($u['realestate_id'] ?? '');
    if (!$uid) { $skipped++; continue; }

    // ── Find existing property post ──────────────────────────────────────
    $existing = get_posts([
        'post_type'      => 'property',
        'meta_key'       => 'expro_unit_id',
        'meta_value'     => $uid,
        'posts_per_page' => 1,
        'fields'         => 'ids',
        'post_status'    => 'any',
    ]);

    $unit_name = trim($u['name'] ?? $u['Nazwa'] ?? $uid);

    // ── Price ────────────────────────────────────────────────────────────
    $price_raw = $u['price_raw'] ?? $u['Cena'] ?? '';
    $price     = preg_replace('/[^\d]/', '', strtok($price_raw, ','));

    // ── Price per m² ─────────────────────────────────────────────────────
    $pm2_raw  = $u['price_m2_raw'] ?? $u['Cena m2'] ?? '';
    preg_match('/[\d\s]+/', str_replace(',', '.', $pm2_raw), $pm2m);
    $price_m2 = $pm2m ? preg_replace('/\s/', '', $pm2m[0]) : '';

    // ── Area ─────────────────────────────────────────────────────────────
    $area_raw = $u['area_raw'] ?? $u['Powierzchnia'] ?? '';
    preg_match('/[\d,\.]+/', $area_raw, $am);
    $area = $am ? str_replace(',', '.', $am[0]) : '';

    // ── Rooms ────────────────────────────────────────────────────────────
    $rooms = (string)($u['rooms'] ?? $u['Pokoje'] ?? '');

    // ── Bedrooms (sypialnie = pokoje - 1, min 1) ─────────────────────────
    $bedrooms_raw = (string)($u['bedrooms'] ?? '');
    if ($bedrooms_raw !== '') {
        $bedrooms = $bedrooms_raw;
    } else {
        $bedrooms = (string)max((int)$rooms - 1, 1);
    }

    // ── Bathrooms ────────────────────────────────────────────────────────
    $bathrooms = (string)($u['bathrooms'] ?? '1');

    // ── Garage ───────────────────────────────────────────────────────────
    $garage = ($u['has_garage'] ?? false) ? '1' : '0';

    // ── Floor display ────────────────────────────────────────────────────
    $floor_raw = (string)($u['floor'] ?? $u['Piętro'] ?? '');
    if ($floor_raw === '0' || strtolower($floor_raw) === 'parter') {
        $floor_disp = 'Parter';
        $floor_num  = '0';
    } elseif (is_numeric($floor_raw)) {
        $floor_disp = 'Piętro ' . $floor_raw;
        $floor_num  = $floor_raw;
    } else {
        $floor_disp = $floor_raw;
        $floor_num  = '';
    }

    // ── Delivery ─────────────────────────────────────────────────────────
    $delivery = trim($u['delivery'] ?? $inv['delivery'] ?? '');

    // ── Stage / Etap ─────────────────────────────────────────────────────
    $etap = trim($u['stage'] ?? '');

    // ── Status → taxonomy slug ───────────────────────────────────────────
    $status_key  = strtolower(trim($u['status'] ?? $u['Status'] ?? 'dostępne'));
    $status_slug = $status_map[$status_key] ?? 'wolny';

    // ── Type → taxonomy slug ─────────────────────────────────────────────
    $typ      = strtolower($u['Typ'] ?? $u['type'] ?? '');
    $type_slug = 'mieszkanie';
    $typ_label = 'Mieszkanie';
    if (strpos($typ, 'lokal') !== false || strpos($typ, 'biuro') !== false) {
        $type_slug = 'lokal-uzytkowy'; $typ_label = 'Lokal użytkowy';
    } elseif (strpos($typ, 'blizniak') !== false || strpos($typ, 'bliźniak') !== false) {
        $type_slug = 'blizniak'; $typ_label = 'Bliźniak';
    } elseif (strpos($typ, 'szereg') !== false) {
        $type_slug = 'dom-szeregowy'; $typ_label = 'Dom szeregowy';
    } elseif (strpos($typ, 'wolnostoj') !== false) {
        $type_slug = 'dom-wolnostojacy'; $typ_label = 'Dom wolnostojący';
    } elseif (strpos($typ, 'dom') !== false) {
        $type_slug = 'dom-szeregowy'; $typ_label = 'Dom';
    }

    // ── Address ──────────────────────────────────────────────────────────
    $street  = $inv['street'] ?? '';
    $city    = $inv['city']   ?? '';
    $address = trim(($street ? $street . ', ' : '') . $city);
    $lat     = (string)($inv['lat'] ?? '');
    $lng     = (string)($inv['lng'] ?? '');

    // ── Description (generated from available data) ───────────────────────
    $rooms_txt = $rooms ? $rooms . '-pokojowe' : '';
    $content = "{$typ_label}";
    if ($rooms_txt) $content .= " {$rooms_txt}";
    if ($area) $content .= " o powierzchni {$area} m²";
    if ($floor_disp && in_array($type_slug, ['mieszkanie', 'lokal-uzytkowy'])) {
        $content .= ", {$floor_disp}";
    }
    $content .= ". Inwestycja: <strong>{$inv['name']}</strong>";
    if ($city) $content .= ", {$city}";
    if ($street) $content .= ", ul. {$street}";
    $content .= ".";
    if ($delivery) $content .= " Termin oddania: {$delivery}.";
    if ($price) {
        $price_fmt = number_format((int)$price, 0, ',', ' ');
        $content .= " Cena: {$price_fmt} PLN";
        if ($price_m2) $content .= " ({$price_m2} PLN/m²)";
        $content .= ".";
    }
    if ($etap) $content .= " Etap: {$etap}.";

    // Investment amenities in description
    $amenity_lines = [];
    if (!empty($inv_extra['winda'])) $amenity_lines[] = "winda";
    if (!empty($inv_extra['smart_home'])) $amenity_lines[] = "Smart Home";
    if (!empty($inv_extra['stacja_ev'])) $amenity_lines[] = "stacja ładowania EV";
    if (!empty($inv_extra['miejsce_postojowe'])) $amenity_lines[] = "miejsce postojowe";
    if (!empty($inv_extra['komorki_lokatorskie'])) $amenity_lines[] = "komórka lokatorska";
    if ($amenity_lines) {
        $content .= " Inwestycja wyposażona m.in. w: " . implode(', ', $amenity_lines) . ".";
    }
    $content .= " Skontaktuj się z naszym doradcą, aby uzyskać więcej informacji i umówić prezentację.";

    // ── Title ─────────────────────────────────────────────────────────────
    $title = $unit_name . ' — ' . $inv['name'];

    // ── Create or update post ────────────────────────────────────────────
    if (!empty($existing)) {
        $post_id   = (int)$existing[0];
        $upd_args  = ['ID' => $post_id, 'post_title' => $title, 'post_status' => 'publish'];
        // Skip overwriting description if manually locked
        if (!get_post_meta($post_id, '_description_locked', true)) {
            $upd_args['post_content'] = $content;
        }
        wp_update_post($upd_args);
        $updated++;
    } else {
        $post_id = wp_insert_post([
            'post_title'   => $title,
            'post_name'    => 'lokal-' . $uid,
            'post_content' => $content,
            'post_type'    => 'property',
            'post_status'  => 'publish',
            'post_author'  => 1,
        ]);
        if (is_wp_error($post_id)) {
            $errors[] = $uid . ': ' . $post_id->get_error_message();
            continue;
        }
        $created++;
    }

    // ── Year from delivery ───────────────────────────────────────────────
    preg_match('/\b(20\d{2})\b/', $delivery, $ym);
    $year = $ym ? $ym[1] : '';

    // ── Meta fields ──────────────────────────────────────────────────────
    $metas = [
        'expro_unit_id'               => $uid,
        'expro_id'                    => (string)$inv['expro_id'],
        'fave_property_id'            => $uid,
        'fave_property_price'         => $price,
        'fave_property_price_postfix' => 'PLN',
        'fave_property_size'          => $area,
        'fave_property_rooms'         => $rooms,
        'fave_property_bedrooms'      => $bedrooms,
        'fave_property_bathrooms'     => $bathrooms,
        'fave_property_garage'        => $garage,
        'fave_property_location'      => $lat,
        'fave_property_location2'     => $lng,
        'houzez_geolocation_lat'      => $lat,
        'houzez_geolocation_long'     => $lng,
        'fave_property_map_address'   => $address,
        'fave_property_project_id'    => (string)$parent_id,
        'fave_property_year'          => $year,
        'lokal_pietro'                => $floor_disp,
        'lokal_pietro_nr'             => $floor_num,
        'lokal_termin_oddania'        => $delivery,
        'lokal_cena_m2'               => $price_m2,
        'lokal_status_expro'          => $status_key,
        'lokal_etap'                  => $etap,
        // Unit amenities
        'lokal_balkon'                => ($u['has_balcony'] ?? false) ? '1' : '0',
        'lokal_balkon_m2'             => $u['balcony_area'] ?? '',
        'lokal_taras'                 => ($u['has_terrace'] ?? false) ? '1' : '0',
        'lokal_taras_m2'              => $u['terrace_area'] ?? '',
        'lokal_ogrodek'               => ($u['has_garden'] ?? false) ? '1' : '0',
        'lokal_ogrodek_m2'            => $u['garden_area'] ?? '',
        'lokal_piwnica'               => ($u['has_basement'] ?? false) ? '1' : '0',
        // Investment extras
        'inw_winda'                   => $inv_extra['winda'] ?? '',
        'inw_parking_naziemne_cena'   => $inv_extra['parking_naziemne_cena'] ?? '',
        'inw_parking_podziemne_cena'  => $inv_extra['parking_podziemne_cena'] ?? '',
        'inw_komorka_cena'            => $inv_extra['komorka_cena'] ?? '',
        'inw_forma_wlasnosci'         => $inv_extra['forma_wlasnosci'] ?? '',
        'inw_ogrzewanie'              => $inv_extra['rodzaj_ogrzewania'] ?? '',
        'inw_okna'                    => $inv_extra['rodzaj_okien'] ?? '',
        'inw_pod_klucz'               => $inv_extra['pod_klucz'] ?? '',
        'inw_smart_home'              => $inv_extra['smart_home'] ?? '',
        'inw_stacja_ev'               => $inv_extra['stacja_ev'] ?? '',
        'inw_ochrona'                 => $inv_extra['rodzaje_ochrony'] ?? '',
    ];
    foreach ($metas as $k => $v) {
        update_post_meta($post_id, $k, (string)$v);
    }

    // ── Taxonomies ───────────────────────────────────────────────────────
    wp_set_object_terms($post_id, [$label_tid], 'property_label');
    wp_set_object_terms($post_id, [$status_slug], 'property_status');
    wp_set_object_terms($post_id, [$type_slug],   'property_type');
    if ($projekt_tid) {
        wp_set_object_terms($post_id, [$projekt_tid], 'projekt');
    }

    // property_city — find or create term
    if ($city) {
        $ct = term_exists($city, 'property_city');
        if (!$ct) $ct = wp_insert_term($city, 'property_city');
        if ($ct && !is_wp_error($ct)) {
            $ctid = is_array($ct) ? (int)$ct['term_id'] : (int)$ct;
            wp_set_object_terms($post_id, [$ctid], 'property_city');
        }
    }

    // ── property_feature taxonomy ────────────────────────────────────────
    $features = [];
    if ($u['has_balcony'] ?? false)  $features[] = 'Balkon';
    if ($u['has_terrace'] ?? false)  $features[] = 'Taras';
    if ($u['has_garden']  ?? false)  $features[] = 'Ogródek';
    if ($u['has_garage']  ?? false)  $features[] = 'Garaż';
    if ($u['has_basement'] ?? false) $features[] = 'Piwnica';
    if (!empty($inv_extra['winda']))      $features[] = 'Winda';
    if (!empty($inv_extra['smart_home'])) $features[] = 'Smart Home';
    if (!empty($inv_extra['stacja_ev']))  $features[] = 'Stacja EV';
    if (!empty($inv_extra['miejsce_postojowe'])) $features[] = 'Miejsce postojowe';
    if (!empty($inv_extra['komorki_lokatorskie'])) $features[] = 'Komórka lokatorska';
    if (!empty($inv_extra['pod_klucz'])) $features[] = 'Wykończenie pod klucz';

    if ($features) {
        $feat_tids = [];
        foreach ($features as $feat_name) {
            $ft = term_exists($feat_name, 'property_feature');
            if (!$ft) $ft = wp_insert_term($feat_name, 'property_feature');
            if ($ft && !is_wp_error($ft)) {
                $feat_tids[] = (int)(is_array($ft) ? $ft['term_id'] : $ft);
            }
        }
        if ($feat_tids) {
            wp_set_object_terms($post_id, $feat_tids, 'property_feature');
        }
    }

    // ── Photos: full gallery + thumbnail ─────────────────────────────────
    $photo_urls  = $u['photo_urls'] ?? [];
    $gallery_ids = (array)(get_post_meta($post_id, 'fave_property_images', true) ?: []);

    foreach ($photo_urls as $img_url) {
        if (!$img_url) continue;
        // Skip if already imported (check by source URL in attachment meta)
        $existing_att = get_posts([
            'post_type'      => 'attachment',
            'meta_key'       => '_source_url',
            'meta_value'     => $img_url,
            'posts_per_page' => 1,
            'fields'         => 'ids',
        ]);
        if ($existing_att) {
            $att_id = (int)$existing_att[0];
        } else {
            $att_id = media_sideload_image($img_url, $post_id, null, 'id');
        }
        if (!is_wp_error($att_id) && !in_array($att_id, $gallery_ids)) {
            $gallery_ids[] = $att_id;
        }
    }

    if ($gallery_ids) {
        update_post_meta($post_id, 'fave_property_images', $gallery_ids);
        if (!has_post_thumbnail($post_id)) {
            set_post_thumbnail($post_id, $gallery_ids[0]);
        }
    } elseif (!has_post_thumbnail($post_id) && $parent_id) {
        // Fallback: reuse parent investment thumbnail
        $parent_thumb = get_post_thumbnail_id($parent_id);
        if ($parent_thumb) set_post_thumbnail($post_id, $parent_thumb);
    }

    // ── Floor plan ───────────────────────────────────────────────────────
    $plan_url = $u['plan_urls'][0] ?? '';
    if ($plan_url && !get_post_meta($post_id, 'lokal_plan_attachment_id', true)) {
        $existing_plan = get_posts([
            'post_type'      => 'attachment',
            'meta_key'       => '_source_url',
            'meta_value'     => $plan_url,
            'posts_per_page' => 1,
            'fields'         => 'ids',
        ]);
        if ($existing_plan) {
            $plan_att = (int)$existing_plan[0];
        } else {
            $plan_att = media_sideload_image($plan_url, $post_id, null, 'id');
        }
        if (!is_wp_error($plan_att)) {
            update_post_meta($post_id, 'lokal_plan_attachment_id', (string)$plan_att);
        } else {
            update_post_meta($post_id, 'lokal_plan_url_expro', $plan_url);
        }
    }
}

echo json_encode([
    'created' => $created,
    'updated' => $updated,
    'skipped' => $skipped,
    'errors'  => $errors,
]);
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_post_map(ssh: SSHClient) -> dict:
    """Return {expro_id_str: wp_post_id} for all inwestycja posts."""
    php = (
        "<?php\n"
        "$posts=get_posts(['post_type'=>'inwestycja','posts_per_page'=>-1,'fields'=>'ids','post_status'=>'any']);\n"
        "foreach($posts as $id){\n"
        "  $eid=get_post_meta($id,'expro_id',true);\n"
        "  if($eid) echo $id.','.$eid.\"\\n\";\n"
        "}\n"
    )
    ssh.write_remote_file(php, '/tmp/esm_postmap.php')
    out = ssh.run_wp_cli('eval-file /tmp/esm_postmap.php')
    result = {}
    for line in out.strip().splitlines():
        parts = line.strip().split(',', 1)
        if len(parts) == 2 and parts[0].isdigit():
            result[parts[1].strip()] = int(parts[0])
    return result


def get_projekt_term_id(ssh: SSHClient, post_id: int) -> int:
    """Return projekt taxonomy term ID for a given inwestycja post."""
    php = (
        "<?php\n"
        f"$t=wp_get_post_terms({post_id},'projekt',['fields'=>'ids']);\n"
        "echo(!is_wp_error($t)&&!empty($t))?$t[0]:'0';\n"
    )
    ssh.write_remote_file(php, '/tmp/esm_getterm.php')
    out = ssh.run_wp_cli('eval-file /tmp/esm_getterm.php')
    return int(out.strip() or '0')


def sync_units(ssh: SSHClient, inv: dict, parent_id: int, projekt_term_id: int) -> tuple[int, int]:
    """Batch-create/update property posts for all units of an investment."""
    units = inv.get('units', [])
    if not units:
        return 0, 0

    payload = {
        'units': units,
        'inv': {
            'name':     inv.get('name', ''),
            'expro_id': str(inv.get('expro_id') or inv.get('id', '')),
            'city':     inv.get('city', ''),
            'street':   inv.get('street', ''),
            'district': inv.get('district', ''),
            'lat':      str(inv.get('lat', '')),
            'lng':      str(inv.get('lng', '')),
            'delivery': inv.get('delivery', ''),
        },
        'inv_extra':       inv.get('extra', {}),
        'parent_id':       parent_id,
        'projekt_term_id': projekt_term_id,
    }

    data_json = json.dumps(payload, ensure_ascii=False)
    ssh.write_remote_file(data_json, '/tmp/esm_units_data.json')
    ssh.write_remote_file(PHP_SYNC_UNITS, '/tmp/esm_sync_units.php')

    out = ssh.run_wp_cli('eval-file /tmp/esm_sync_units.php')
    out = out.strip()

    # Parse JSON result
    try:
        result = json.loads(out)
        if result.get('errors'):
            for e in result['errors']:
                log(f'  ERROR: {e}')
        return result.get('created', 0), result.get('updated', 0)
    except Exception:
        log(f'  WARNING: unexpected output: {out[:200]}')
        return 0, 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expro-ids', help='Comma-separated ExPro IDs to sync')
    parser.add_argument('--all', action='store_true', help='Sync all investments with units')
    args = parser.parse_args()

    if not DATA.exists():
        log(f'ERROR: {DATA} not found — run scrape first')
        sys.exit(1)

    all_data = json.loads(DATA.read_text('utf-8'))

    if args.expro_ids:
        ids = set(args.expro_ids.split(','))
        investments = [d for d in all_data if str(d.get('expro_id') or d.get('id', '')) in ids]
    elif args.all:
        investments = [d for d in all_data if d.get('units')]
    else:
        parser.print_help()
        return

    log(f'Investments with units to sync: {len(investments)}')

    ssh = SSHClient()
    ssh._connect()
    log('SSH connected.')

    # Build expro_id → WP post_id map
    post_map = get_post_map(ssh)
    log(f'Found {len(post_map)} inwestycja posts in WP.')

    total_created = total_updated = total_skipped = 0

    for inv in investments:
        expro_id = str(inv.get('expro_id') or inv.get('id', ''))
        name     = inv.get('name', expro_id)
        units    = inv.get('units', [])

        log(f'\n{"="*60}')
        log(f'[{expro_id}] {name} — {len(units)} units')

        parent_id = post_map.get(expro_id)
        if not parent_id:
            log(f'  SKIP: no inwestycja post found for expro_id={expro_id}')
            total_skipped += 1
            continue

        log(f'  Parent post: {parent_id}')

        projekt_term_id = get_projekt_term_id(ssh, parent_id)
        if projekt_term_id:
            log(f'  Projekt term ID: {projekt_term_id}')
        else:
            log(f'  WARNING: no projekt term for post {parent_id}')

        c, u = sync_units(ssh, inv, parent_id, projekt_term_id)
        log(f'  ✓ {c} created, {u} updated')
        total_created += c
        total_updated += u

    ssh.close()
    log(f'\nMieszkania sync complete: {total_created} created, {total_updated} updated, {total_skipped} skipped.')


if __name__ == '__main__':
    main()
