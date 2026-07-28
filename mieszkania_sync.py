#!/usr/bin/env python3
"""
Mieszkania sync — creates/updates individual `property` WP posts for each ExPro unit.
Each apartment/house unit from ExPro becomes one property post in Houzez.

Usage:
  python3 mieszkania_sync.py --all
  python3 mieszkania_sync.py --expro-ids 7567,7563
"""
import argparse, json, re, sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

try:
    from plan_redactor import redact_plan_image
except ImportError:
    redact_plan_image = None  # tesseract/pytesseract not installed — skip redaction gracefully

DATA         = Path(__file__).parent / 'data' / 'expro_data.json'
AMENITY_DATA = Path(__file__).parent / 'data' / 'amenity_data.json'
PROGRESS     = Path(__file__).parent / 'data' / 'mieszkania_sync_progress.json'
LOG_FILE     = Path(__file__).parent / 'data' / 'mieszkania_sync.log'

# ExPro IDs that are ONLY Dom — never overwrite their projekt_typ
_DOM_ONLY_IDS = None  # type: ignore

def _dom_only_ids() -> set:
    global _DOM_ONLY_IDS
    if _DOM_ONLY_IDS is None:
        data = json.loads(DATA.read_text('utf-8'))
        _DOM_ONLY_IDS = {
            str(d['expro_id']) for d in data
            if all(u.get('type', '') in ('Dom', 'Segment', '') for u in d.get('units', []))
            and any(u.get('type', '') == 'Dom' for u in d.get('units', []))
        }
    return _DOM_ONLY_IDS

# ── Taxonomy term IDs (verified on site) ─────────────────────────────────────
LABEL_RYNEK_PIERWOTNY = 181   # property_label: Rynek Pierwotny

# Unused by the sync itself (the PHP template carries its own copy) but kept in
# step with it so the two never disagree.
STATUS_SLUG_MAP = {
    'dostępne':          'wolny',
    'dostepne':          'wolny',
    'wolne':             'wolny',
    'zarezerwowane':     'zarezerwowany',
    'rezerwacja':        'zarezerwowany',
    'rezerwacja ustna':  'zarezerwowany',
    'sprzedane':         'sprzedany',
    'sprzedany':         'sprzedany',
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
    // ExPro also emits "Rezerwacja ustna" (41 units in the current feed). It
    // was missing from this map, so those units fell through to the 'wolny'
    // default and were advertised as free.
    'rezerwacja ustna' => 'zarezerwowany',
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

    $unit_name = trim($u['name'] ?? $u['Nazwa'] ?? $uid);

    // ── Find existing property post ──────────────────────────────────────
    $existing = get_posts([
        'post_type'      => 'property',
        'meta_key'       => 'expro_unit_id',
        'meta_value'     => $uid,
        'posts_per_page' => 1,
        'fields'         => 'ids',
        'post_status'    => 'any',
    ]);

    // ── UUID migration fallback ──────────────────────────────────────────
    // Old scraper stored numeric realestate_id; API scraper uses UUID.
    // When UUID not found, match by investment expro_id meta + unit name prefix.
    // On match, migrate expro_unit_id to UUID (runs once per unit, then fast path).
    if (empty($existing) && $unit_name && strpos($uid, '-') !== false) {
        global $wpdb;
        $found_id = $wpdb->get_var($wpdb->prepare(
            "SELECT p.ID FROM {$wpdb->posts} p
             INNER JOIN {$wpdb->postmeta} pm ON pm.post_id = p.ID
             WHERE p.post_type = 'property'
               AND p.post_status != 'trash'
               AND pm.meta_key = 'expro_id'
               AND pm.meta_value = %s
               AND p.post_title LIKE %s
             LIMIT 1",
            (string)$inv['expro_id'],
            $wpdb->esc_like($unit_name) . '%'
        ));
        if ($found_id) {
            $existing = [(int)$found_id];
            update_post_meta((int)$found_id, 'expro_unit_id', $uid);
        }
    }

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

    // ── Bedrooms ─────────────────────────────────────────────────────────
    // Was "pokoje - 1, min 1" whenever ExPro sent nothing, which is always —
    // the unit payload has no bedrooms field. A guess written into the database
    // reads exactly like a measurement, so an empty value stays empty.
    $bedrooms = (string)($u['bedrooms'] ?? '');

    // ── Bathrooms ────────────────────────────────────────────────────────
    // Was hardcoded to '1' for every unit; ExPro has no bathrooms field at all,
    // so all 7015 units claimed one bathroom nobody had counted.
    $bathrooms = (string)($u['bathrooms'] ?? '');

    // ── Garage ───────────────────────────────────────────────────────────
    $garage = ($u['has_garage'] ?? false) ? 'Tak' : '';

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
    $lat     = (string)($inv['latitude'] ?? $inv['lat'] ?? '');
    $lng     = (string)($inv['longitude'] ?? $inv['lng'] ?? '');

    // ── Description (generated from available data) ───────────────────────
    $rooms_txt = $rooms ? $rooms . '-pokojowe' : '';
    $content = "{$typ_label}";
    if ($rooms_txt) $content .= " {$rooms_txt}";
    // Polish decimal comma, matching every other number on the page. The raw
    // value carries a dot ("99.88"), which the rendered page never uses.
    if ($area) { $area_txt = str_replace('.', ',', (string)$area); $content .= " o powierzchni {$area_txt} m²"; }
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
        // Was printed raw ("15008 PLN/m²") while the same figure appeared as
        // "15 008" everywhere else on the page.
        if ($price_m2) { $pm2_fmt = number_format((int)$price_m2, 0, ',', ' '); $content .= " ({$pm2_fmt} PLN/m²)"; }
        $content .= ".";
    }
    // "b/d" is ExPro's "no data", not a stage — it went into the description
    // verbatim on 5642 of 7015 units as "Etap: b/d.".
    if ($etap && strcasecmp(trim($etap), 'b/d') !== 0) $content .= " Etap: {$etap}.";

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
        'fave_property_id'            => $unit_name ?: $uid,
        'fave_property_price'         => $price,
        'fave_property_price_postfix' => 'PLN',
        'fave_property_size'          => $area,
        'fave_property_rooms'         => $rooms,
        'fave_property_bedrooms'      => $bedrooms,
        'fave_property_bathrooms'     => $bathrooms,
        'fave_property_garage'        => $garage,
        'fave_land_area'              => $u['garden_area'] ?? '',
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
        // Unit amenities — only write if explicitly provided (absent = preserve existing WP value)
        'lokal_balkon_m2'             => $u['balcony_area'] ?? '',
        'lokal_taras_m2'              => $u['terrace_area'] ?? '',
        'lokal_ogrodek_m2'            => $u['garden_area'] ?? '',
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
        // Real building height. The unit template used to draw its floor tower
        // from max($floor_nr + 3, 8) — an invented block — because this never
        // reached the unit. ExPro fills it on 157 of 173 investments.
        'inw_pietro_max'              => $inv_extra['pietro_max'] ?? '',
        'inw_wielkosc_projektu'       => $inv_extra['wielkosc_projektu'] ?? '',
    ];
    foreach ($metas as $k => $v) {
        update_post_meta($post_id, $k, (string)$v);
    }

    // Unit amenities — only overwrite if scraper explicitly provided the key
    if (array_key_exists('has_balcony',  $u)) update_post_meta($post_id, 'lokal_balkon',  $u['has_balcony']  ? '1' : '0');
    if (array_key_exists('has_terrace',  $u)) update_post_meta($post_id, 'lokal_taras',   $u['has_terrace']  ? '1' : '0');
    if (array_key_exists('has_garden',   $u)) update_post_meta($post_id, 'lokal_ogrodek', $u['has_garden']   ? '1' : '0');
    if (array_key_exists('has_basement', $u)) update_post_meta($post_id, 'lokal_piwnica', $u['has_basement'] ? '1' : '0');
    if (array_key_exists('has_garage',   $u)) update_post_meta($post_id, 'lokal_garaz',   $u['has_garage']   ? '1' : '0');

    // ── Taxonomies ───────────────────────────────────────────────────────
    wp_set_object_terms($post_id, [$label_tid], 'property_label');
    wp_set_object_terms($post_id, [$status_slug, 'sprzedaz'], 'property_status');
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
    $unit_amenity_names = ['Balkon', 'Taras', 'Ogródek', 'Garaż', 'Piwnica'];
    $has_amenity_keys   = array_key_exists('has_balcony', $u) || array_key_exists('has_terrace', $u)
                       || array_key_exists('has_garden',  $u) || array_key_exists('has_garage',  $u)
                       || array_key_exists('has_basement',$u);
    $features = [];

    if ($has_amenity_keys) {
        // Full data from scraper — set amenities from scraper values
        if ($u['has_balcony'] ?? false)  $features[] = 'Balkon';
        if ($u['has_terrace'] ?? false)  $features[] = 'Taras';
        if ($u['has_garden']  ?? false)  $features[] = 'Ogródek';
        if ($u['has_garage']  ?? false)  $features[] = 'Garaż';
        if ($u['has_basement'] ?? false) $features[] = 'Piwnica';
    } else {
        // No amenity data from scraper — preserve existing WP unit amenity terms
        $ex_terms = wp_get_post_terms($post_id, 'property_feature', ['fields' => 'names']);
        if (!is_wp_error($ex_terms)) {
            foreach ($ex_terms as $tn) {
                if (in_array($tn, $unit_amenity_names)) $features[] = $tn;
            }
        }
    }

    // Investment-level features — always refresh from current inv_extra
    if (!empty($inv_extra['winda']))               $features[] = 'Winda';
    if (!empty($inv_extra['smart_home']))           $features[] = 'Smart Home';
    if (!empty($inv_extra['stacja_ev']))            $features[] = 'Stacja EV';
    if (!empty($inv_extra['miejsce_postojowe']))    $features[] = 'Miejsce postojowe';
    if (!empty($inv_extra['komorki_lokatorskie'])) $features[] = 'Komórka lokatorska';
    if (!empty($inv_extra['pod_klucz']))            $features[] = 'Wykończenie pod klucz';

    if ($features) {
        $features   = array_unique($features);
        $feat_tids  = [];
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

    // ── Photos: 1-2 investment photos as unit gallery ────────────────────
    $gallery_ids = (array)(get_post_meta($post_id, 'fave_property_images', true) ?: []);
    if (!$gallery_ids) {
        $raw = get_post_meta($parent_id, 'projekt_galeria', true) ?: '';
        if ($raw) {
            $all_ids = is_array($raw) ? $raw : explode(',', $raw);
            $all_ids = array_values(array_filter(array_map('intval', $all_ids)));
            $gallery_ids = array_slice($all_ids, 0, 2);
            if ($gallery_ids) {
                update_post_meta($post_id, 'fave_property_images', $gallery_ids);
            }
        }
    }

    if ($gallery_ids) {
        if (!has_post_thumbnail($post_id)) {
            set_post_thumbnail($post_id, $gallery_ids[0]);
        }
    } elseif (!has_post_thumbnail($post_id) && $parent_id) {
        // Fallback: reuse parent investment thumbnail
        $parent_thumb = get_post_thumbnail_id($parent_id);
        if ($parent_thumb) set_post_thumbnail($post_id, $parent_thumb);
    }

    // ── Floor plan ───────────────────────────────────────────────────────
    // Plan is always the main card image (_thumbnail_id)
    // ExPro returns its own company logo (expander-logo-new.png) as
    // plan_urls[0]/photo_urls[0] for units that genuinely have no floor
    // plan uploaded on their side — not an empty value, a real image URL
    // that happens to be their branding. Importing it verbatim makes the
    // unit LOOK like it has a photo (thumbnail is set) when it doesn't.
    // Treat it as "no plan available" instead of real content.
    $plan_url        = $u['plan_urls'][0] ?? '';
    if (str_contains($plan_url, 'expander-logo')) $plan_url = '';
    $plan_server_map = $u['plan_server_map'] ?? [];
    $plan_att        = (int)get_post_meta($post_id, 'lokal_plan_attachment_id', true);

    if (!$plan_att && $plan_url) {
        $existing_plan = get_posts([
            'post_type'      => 'attachment',
            'meta_key'       => '_source_url',
            'meta_value'     => $plan_url,
            'posts_per_page' => 1,
            'fields'         => 'ids',
        ]);
        if ($existing_plan) {
            $plan_att = (int)$existing_plan[0];
        } elseif (!empty($plan_server_map[$plan_url]) && file_exists($plan_server_map[$plan_url])) {
            $tmp = tempnam(sys_get_temp_dir(), 'esm_plan_');
            copy($plan_server_map[$plan_url], $tmp);
            $file_array = ['name' => basename($plan_server_map[$plan_url]), 'tmp_name' => $tmp];
            $plan_att_new = media_handle_sideload($file_array, $post_id);
            if (!is_wp_error($plan_att_new)) { $plan_att = $plan_att_new; }
        } else {
            $plan_att_new = media_sideload_image($plan_url, $post_id, null, 'id');
            if (!is_wp_error($plan_att_new)) { $plan_att = $plan_att_new; }
        }
        if ($plan_att) {
            update_post_meta($plan_att, '_source_url',              $plan_url);
            update_post_meta($post_id,  'lokal_plan_attachment_id', (string)$plan_att);
        } else {
            update_post_meta($post_id, 'lokal_plan_url_expro', $plan_url);
        }
    }

    // Use floor plan as card thumbnail if available
    if ($plan_att) {
        set_post_thumbnail($post_id, $plan_att);
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


def upload_unit_images(ssh: SSHClient, units: list[dict], extra_terms: list = None,
                        server_base: str = '/tmp/esm_imgs') -> None:
    """SFTP-upload unit floor plan images to WP server before PHP import.
    Gallery photos are taken from investment-level projekt_galeria — no upload needed.

    Before uploading, each plan image is checked for developer/investment
    name, sales-office phone/email/address baked into the image itself
    (common — different developers export their floor plans from their own
    branded PDF template) and blurred in place. extra_terms should be the
    developer name and investment name for THIS investment, on top of the
    universal phone/email/website/address patterns plan_redactor already
    checks unconditionally."""
    files_to_upload = []
    for unit in units:
        rid = unit.get('realestate_id', '')
        if not rid:
            continue
        # Only upload plan images (not gallery photos — those come from parent investment)
        for url, local in unit.get('plan_url_map', {}).items():
            if local and Path(local).exists():
                files_to_upload.append((rid, url, local))

    if not files_to_upload:
        return

    if redact_plan_image is not None:
        redacted_count = 0
        for _, _, local in files_to_upload:
            try:
                hits = redact_plan_image(local, extra_terms=extra_terms or [])
                if hits:
                    redacted_count += 1
            except Exception as e:
                log(f'  WARN: plan redaction failed for {local}: {e}')
        if redacted_count:
            log(f'  Redacted identifying text on {redacted_count}/{len(files_to_upload)} plan image(s)')
    else:
        log('  WARN: pytesseract not installed — skipping plan redaction (tesseract-ocr missing?)')

    sftp = ssh._client.open_sftp()
    try:
        try:
            sftp.mkdir(server_base)
        except IOError:
            pass
        dirs_created: set = set()
        for rid, url, local in files_to_upload:
            rid_dir = f"{server_base}/{rid}"
            if rid_dir not in dirs_created:
                try:
                    sftp.mkdir(rid_dir)
                except IOError:
                    pass
                dirs_created.add(rid_dir)
            remote_path = f"{rid_dir}/{Path(local).name}"
            sftp.put(local, remote_path)
            # Augment unit with server path for plan
            for unit in units:
                if unit.get('realestate_id') == rid:
                    unit.setdefault('plan_server_map', {})[url] = remote_path
    finally:
        sftp.close()
    log(f'  Uploaded {len(files_to_upload)} image files to server')


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

    # Upload unit images to server (authenticated download already done by scraper).
    # Developer/investment name passed through so plan_redactor can blur them
    # if they're baked into the plan image itself (on top of the universal
    # phone/email/website/address patterns it always checks).
    upload_unit_images(ssh, units, extra_terms=[inv.get('developer', ''), inv.get('name', '')])

    data_json = json.dumps(payload, ensure_ascii=False)
    ssh.write_remote_file(data_json, '/tmp/esm_units_data.json')
    ssh.write_remote_file(PHP_SYNC_UNITS, '/tmp/esm_sync_units.php')

    out = ssh.run_wp_cli('eval-file /tmp/esm_sync_units.php', timeout=600)
    out = out.strip()

    # Cleanup server image temp files
    ssh._client.exec_command('rm -rf /tmp/esm_imgs')

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


# ── Progress helpers ──────────────────────────────────────────────────────────

def _load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text('utf-8'))
        except Exception:
            pass
    return {'done': [], 'failed': []}

def _save_progress(p: dict) -> None:
    PROGRESS.write_text(json.dumps(p, ensure_ascii=False, indent=2), 'utf-8')

def _log(msg: str) -> None:
    log(msg)
    try:
        with LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass

# ── SSH reconnect wrapper ─────────────────────────────────────────────────────

def _ensure_ssh(ssh: SSHClient) -> SSHClient:
    """Reconnect if SSH dropped."""
    try:
        ssh._client.get_transport().is_active()  # type: ignore
        return ssh
    except Exception:
        _log('  SSH lost — reconnecting...')
        try:
            ssh.close()
        except Exception:
            pass
        new = SSHClient()
        new._connect()
        return new

# ── Single investment with full error guard ───────────────────────────────────

def sync_one(ssh: SSHClient, inv: dict, post_map: dict) -> tuple[SSHClient, str]:
    """
    Returns (ssh, status) where status is 'ok' | 'skip' | 'error'.
    Re-raises nothing — all exceptions are caught here.
    """
    expro_id = str(inv.get('expro_id') or inv.get('id', ''))
    name     = inv.get('name', expro_id)
    units    = inv.get('units', [])

    _log(f'\n{"="*60}')
    _log(f'[{expro_id}] {name} — {len(units)} units')

    # Guard: never touch pure-Dom investments
    if expro_id in _dom_only_ids():
        _log(f'  SKIP: Dom-only investment — not touching')
        return ssh, 'skip'

    parent_id = post_map.get(expro_id)
    if not parent_id:
        _log(f'  SKIP: no inwestycja post in WP for expro_id={expro_id}')
        return ssh, 'skip'

    _log(f'  Parent post: {parent_id}')

    try:
        ssh = _ensure_ssh(ssh)
        projekt_term_id = get_projekt_term_id(ssh, parent_id)
        if projekt_term_id:
            _log(f'  Projekt term ID: {projekt_term_id}')
        else:
            _log(f'  WARNING: no projekt term for post {parent_id}')

        ssh = _ensure_ssh(ssh)
        c, u = sync_units(ssh, inv, parent_id, projekt_term_id)
        _log(f'  ✓ {c} created, {u} updated')
        return ssh, 'ok'

    except Exception as e:
        _log(f'  ERROR: {e}')
        _log(f'  {traceback.format_exc().splitlines()[-1]}')
        return ssh, 'error'

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expro-ids', help='Comma-separated ExPro IDs to sync')
    parser.add_argument('--all',    action='store_true', help='Sync all investments with units')
    parser.add_argument('--resume', action='store_true', help='Skip already-done investments')
    parser.add_argument('--retry',  action='store_true', help='Retry only previously failed ones')
    parser.add_argument('--reset-progress', action='store_true', help='Clear progress file')
    args = parser.parse_args()

    if not DATA.exists():
        _log(f'ERROR: {DATA} not found — run scrape first')
        sys.exit(1)

    if args.reset_progress:
        PROGRESS.unlink(missing_ok=True)
        _log('Progress cleared.')
        return

    all_data = json.loads(DATA.read_text('utf-8'))

    # Merge Phase-2 amenity data (amenity_patch.py) into unit dicts if available
    if AMENITY_DATA.exists():
        amenity_cache = json.loads(AMENITY_DATA.read_text('utf-8'))
        merged = 0
        for inv in all_data:
            for unit in inv.get('units', []):
                uid = unit.get('realestate_id', '')
                if uid in amenity_cache and amenity_cache[uid]:
                    unit.update(amenity_cache[uid])
                    merged += 1
        _log(f'Amenity data merged for {merged} units from {AMENITY_DATA.name}')

    progress = _load_progress()

    if args.expro_ids:
        ids = set(args.expro_ids.split(','))
        investments = [d for d in all_data if str(d.get('expro_id') or d.get('id', '')) in ids]
    elif args.all:
        investments = [d for d in all_data if d.get('units')]
    elif args.retry:
        failed_ids = set(progress.get('failed', []))
        investments = [d for d in all_data if str(d.get('expro_id') or d.get('id', '')) in failed_ids]
        _log(f'Retrying {len(investments)} previously failed investments')
    else:
        parser.print_help()
        return

    # Filter already done if --resume
    if args.resume or args.all:
        done_ids = set(progress.get('done', []))
        before = len(investments)
        investments = [d for d in investments
                       if str(d.get('expro_id') or d.get('id', '')) not in done_ids]
        if before != len(investments):
            _log(f'Resume: skipping {before - len(investments)} already done, {len(investments)} remaining')

    _log(f'Investments to sync: {len(investments)}')

    ssh = SSHClient()
    ssh._connect()
    _log('SSH connected.')

    # Build expro_id → WP post_id map (with retry)
    for attempt in range(3):
        try:
            post_map = get_post_map(ssh)
            _log(f'Found {len(post_map)} inwestycja posts in WP.')
            break
        except Exception as e:
            _log(f'get_post_map failed (attempt {attempt+1}): {e}')
            time.sleep(5)
            ssh = _ensure_ssh(ssh)
    else:
        _log('FATAL: cannot load post map after 3 attempts')
        sys.exit(1)

    total_ok = total_skip = total_err = 0

    for i, inv in enumerate(investments, 1):
        expro_id = str(inv.get('expro_id') or inv.get('id', ''))
        _log(f'[{i}/{len(investments)}]')

        ssh, status = sync_one(ssh, inv, post_map)

        if status == 'ok':
            total_ok += 1
            progress['done'] = list(set(progress.get('done', [])) | {expro_id})
            # Remove from failed if it was there
            progress['failed'] = [x for x in progress.get('failed', []) if x != expro_id]
        elif status == 'skip':
            total_skip += 1
        else:
            total_err += 1
            progress['failed'] = list(set(progress.get('failed', [])) | {expro_id})

        _save_progress(progress)
        time.sleep(0.5)  # small pause to avoid hammering server

    try:
        ssh.close()
    except Exception:
        pass

    _log(f'\n{"="*60}')
    _log(f'Done: {total_ok} ok, {total_skip} skipped, {total_err} errors')
    if progress.get('failed'):
        _log(f'Failed IDs: {", ".join(progress["failed"])}')
        _log('Re-run with --retry to retry failed ones')


if __name__ == '__main__':
    main()
