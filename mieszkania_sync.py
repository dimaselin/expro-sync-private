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
    $title     = $unit_name . ' — ' . $inv['name'];

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
    $delivery = $u['delivery'] ?? $inv['delivery'] ?? '';

    // ── Status → taxonomy slug ───────────────────────────────────────────
    $status_key  = strtolower(trim($u['status'] ?? $u['Status'] ?? 'dostępne'));
    $status_slug = $status_map[$status_key] ?? 'wolny';

    // ── Type → taxonomy slug ─────────────────────────────────────────────
    $typ      = strtolower($u['Typ'] ?? $u['type'] ?? '');
    $type_slug = 'mieszkanie';
    if (strpos($typ, 'lokal') !== false || strpos($typ, 'biuro') !== false) {
        $type_slug = 'lokal-uzytkowy';
    } elseif (strpos($typ, 'blizniak') !== false || strpos($typ, 'bliźniak') !== false) {
        $type_slug = 'blizniak';
    } elseif (strpos($typ, 'szereg') !== false) {
        $type_slug = 'dom-szeregowy';
    } elseif (strpos($typ, 'wolnostoj') !== false) {
        $type_slug = 'dom-wolnostojacy';
    } elseif (strpos($typ, 'dom') !== false) {
        $type_slug = 'dom-szeregowy';
    }

    // ── Create or update post ────────────────────────────────────────────
    if (!empty($existing)) {
        $post_id = (int)$existing[0];
        wp_update_post(['ID' => $post_id, 'post_title' => $title, 'post_status' => 'publish']);
        $updated++;
    } else {
        $slug_base = sanitize_title($unit_name . '-' . $inv['expro_id']);
        $post_id   = wp_insert_post([
            'post_title'  => $title,
            'post_name'   => 'lokal-' . $uid,
            'post_type'   => 'property',
            'post_status' => 'publish',
            'post_author' => 1,
        ]);
        if (is_wp_error($post_id)) {
            $errors[] = $uid . ': ' . $post_id->get_error_message();
            continue;
        }
        $created++;
    }

    // ── Map address ──────────────────────────────────────────────────────
    $street  = $inv['street'] ?? '';
    $city    = $inv['city']   ?? '';
    $address = trim(($street ? $street . ', ' : '') . $city);
    $lat     = (string)($inv['lat'] ?? '');
    $lng     = (string)($inv['lng'] ?? '');

    // ── Meta fields ──────────────────────────────────────────────────────
    $metas = [
        'expro_unit_id'             => $uid,
        'expro_id'                  => (string)$inv['expro_id'],
        'fave_property_id'          => $uid,
        'fave_property_price'       => $price,
        'fave_property_price_postfix' => 'PLN',
        'fave_property_size'        => $area,
        'fave_property_rooms'       => $rooms,
        'fave_property_location'    => $lat,
        'fave_property_location2'   => $lng,
        'houzez_geolocation_lat'    => $lat,
        'houzez_geolocation_long'   => $lng,
        'fave_property_map_address' => $address,
        'fave_property_project_id'  => (string)$parent_id,
        'fave_property_year'        => '',
        'lokal_pietro'              => $floor_disp,
        'lokal_pietro_nr'           => $floor_num,
        'lokal_termin_oddania'      => $delivery,
        'lokal_cena_m2'             => $price_m2,
        'lokal_status_expro'        => $status_key,
    ];
    foreach ($metas as $k => $v) {
        update_post_meta($post_id, $k, $v);
    }

    // ── Taxonomies ───────────────────────────────────────────────────────
    wp_set_object_terms($post_id, [$label_tid], 'property_label');
    wp_set_object_terms($post_id, [$status_slug], 'property_status');
    wp_set_object_terms($post_id, [$type_slug], 'property_type');

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

    // ── Year from delivery ───────────────────────────────────────────────
    preg_match('/\b(20\d{2})\b/', $delivery, $ym);
    if ($ym) update_post_meta($post_id, 'fave_property_year', $ym[1]);

    // ── Photo — sideload first photo_url, fallback to parent thumbnail ──
    if (!has_post_thumbnail($post_id)) {
        if (!function_exists('media_sideload_image')) {
            require_once ABSPATH . 'wp-admin/includes/media.php';
            require_once ABSPATH . 'wp-admin/includes/file.php';
            require_once ABSPATH . 'wp-admin/includes/image.php';
        }
        $sideloaded = false;
        $photo_url = $u['photo_urls'][0] ?? '';
        if ($photo_url) {
            $att_id = media_sideload_image($photo_url, $post_id, null, 'id');
            if (!is_wp_error($att_id)) {
                set_post_thumbnail($post_id, $att_id);
                $sideloaded = true;
            }
        }
        // fallback: reuse parent investment's featured image
        if (!$sideloaded && $parent_id) {
            $parent_thumb = get_post_thumbnail_id($parent_id);
            if ($parent_thumb) set_post_thumbnail($post_id, $parent_thumb);
        }
    }

    // ── Floor plan — sideload first plan_url ────────────────────────────
    $plan_url = $u['plan_urls'][0] ?? '';
    if ($plan_url && !get_post_meta($post_id, 'lokal_plan_attachment_id', true)) {
        if (!function_exists('media_sideload_image')) {
            require_once ABSPATH . 'wp-admin/includes/media.php';
            require_once ABSPATH . 'wp-admin/includes/file.php';
            require_once ABSPATH . 'wp-admin/includes/image.php';
        }
        $plan_att = media_sideload_image($plan_url, $post_id, null, 'id');
        if (!is_wp_error($plan_att)) {
            update_post_meta($post_id, 'lokal_plan_attachment_id', (string)$plan_att);
        } else {
            // store URL as fallback even if sideload fails (needs auth)
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
        'parent_id':      parent_id,
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
