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
except (ImportError, SystemExit):
    # plan_redactor is a runnable script too, so it reports a missing OCR stack
    # with sys.exit(1) rather than by raising — and SystemExit is not an
    # ImportError, so catching only ImportError killed this whole sync outright
    # instead of skipping redaction. A missing optional dependency must never
    # stop units from syncing; upload_unit_images() then skips the plan upload
    # (see there for why they are not published unredacted).
    redact_plan_image = None

DATA         = Path(__file__).parent / 'data' / 'expro_data.json'
AMENITY_DATA = Path(__file__).parent / 'data' / 'amenity_data.json'
PROGRESS     = Path(__file__).parent / 'data' / 'mieszkania_sync_progress.json'
LOG_FILE     = Path(__file__).parent / 'data' / 'mieszkania_sync.log'

# A guard that skipped every all-houses investment used to live here, to stop
# this script from overwriting their projekt_typ. It never wrote projekt_typ —
# the only mention of that field in this file was the guard's own comment — so
# it protected nothing, while excluding 37 investments and 229 house units from
# the sync entirely. Their prices, statuses, coordinates and types sat
# untouched since 2026-07-09, and every one of them still carried the old
# forced "dom-szeregowy". Type is decided per unit now, so there is nothing
# left for such a guard to defend.

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

# Per-investment override of the resolved unit type. Deliberately empty: the
# classifier reads five signals and reports what it could not place, so an
# entry here means someone checked that investment by hand and ExPro is wrong.
_UNIT_TYPE_OVERRIDES: dict[str, str] = {}

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
// Axis B. Resolved by slug, not pinned to an ID, so a rebuilt term still lands
// and a missing one degrades to "no marker" instead of a fatal.
$_inw          = get_term_by('slug', 'inwestycyjne', 'property_label');
$label_inw_tid = $_inw ? (int) $_inw->term_id : 0;

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

// ── Meta writer ───────────────────────────────────────────────────────────
// Every meta below used to be written unconditionally, so a field ExPro
// didn't send this run became an empty string and erased whatever was
// already in WP. That is not a hypothetical: a dropped unit-detail response,
// an investment whose `extra` block is empty, or amenity data that only
// arrives from the weekly Playwright pass all produce "" here, and the value
// was wiped on every daily run. `fave_land_area`, `lokal_balkon_m2`,
// `lokal_taras_m2`, the whole `inw_*` family and both geolocation fields
// were being cleared this way.
//
// Empty now means "ExPro said nothing", which is not the same as "ExPro said
// there is nothing" — so the existing value stays. The only exceptions are
// the fields listed in $clearable, where a value disappearing is itself the
// news and a stale number would be worse than none.
if (!function_exists('esm_stat')) {
    function esm_stat($key = null) {
        static $s = ['cleared' => 0, 'kept' => 0];
        if ($key === null) return $s;
        $s[$key]++;
        return null;
    }
}
// ExPro answers the amenity questions with the words "Tak" and "Nie", so
// !empty() reads "Nie" as a yes. On the live site that put 'Smart Home' on
// 3634 units whose investment says Nie, and 'Wykończenie pod klucz' on 4523 —
// while all 163 investments say Nie, so not one of them has it. The tags are
// only refreshed when the extra block carries data, which it stopped doing
// after the REST migration, so this has to be correct BEFORE the HTML parser
// starts filling those fields again.
if (!function_exists('esm_is_yes')) {
    function esm_is_yes($v) {
        $v = trim(mb_strtolower((string) $v));
        if ($v === '') return false;
        // Explicit negatives. Anything else non-empty counts as a yes, so a
        // developer writing "2 windy" or "garaż podziemny" is not thrown away.
        $no = ['nie', 'no', 'brak', 'nie dotyczy', 'n/d', 'nd', '-', '—', '0', 'false'];
        return !in_array($v, $no, true);
    }
}
// ── Type resolution ───────────────────────────────────────────────────────
// The old rule was five strpos calls over a free-text label with "mieszkanie"
// as the fallback, so anything unrecognised became a flat: 137 units labelled
// "Nieruchomość inwestycyjna", 84 with no label at all, and 3 "Segment" — a
// row house — are flats on the site today purely because no pattern matched.
// Nothing is guessed here. A unit no signal can place goes to
// `niesklasyfikowane` and is not published, so it shows up in the run summary
// instead of quietly joining the flats.
if (!function_exists('esm_norm')) {
    function esm_norm($s) {
        $s = mb_strtolower(trim((string) $s));
        return strtr($s, ['ł'=>'l','ż'=>'z','ź'=>'z','ó'=>'o','ą'=>'a','ę'=>'e','ć'=>'c','ń'=>'n','ś'=>'s']);
    }
}
if (!function_exists('esm_type_from_label')) {
    // ExPro's own vocabulary (realestate_type_id: Mieszkanie, Dom, Lokal
    // użytkowy, Nieruchomość inwestycyjna, Wykończenie, Apartament
    // inwestycyjny, Segment, Firmy budowlane/Domy modułowe, Zarządzanie
    // najmem) plus the free-text values the feed still emits.
    function esm_type_from_label($raw) {
        $t = esm_norm($raw);
        if ($t === '') return '';
        // Not addressable real estate — these must never resolve to a type.
        if (str_contains($t, 'modulow') || str_contains($t, 'wykonczeni')
            || str_contains($t, 'zarzadzanie najmem') || str_contains($t, 'firmy budowlane')) return '';
        if (str_contains($t, 'lokal') || str_contains($t, 'biuro') || str_contains($t, 'uslug')) return 'lokal-uzytkowy';
        if (str_contains($t, 'segment') || str_contains($t, 'szereg'))  return 'dom-szeregowy';
        if (str_contains($t, 'blizniak'))                               return 'dom-blizniaczy';
        if (str_contains($t, 'wolnostoj'))                              return 'dom-wolnostojacy';
        if (str_contains($t, 'dom'))                                    return 'dom';
        // An investment product is still physically a flat; what makes it an
        // investment is carried on property_label, not on the type.
        if (str_contains($t, 'inwestycyjn'))                            return 'mieszkanie';
        if (str_contains($t, 'mieszkanie') || str_contains($t, 'apartament')) return 'mieszkanie';
        return '';
    }
}
if (!function_exists('esm_is_investment')) {
    // The other half of the sentence above. esm_type_from_label answers what a
    // unit physically is and deliberately forgets that it is sold as an
    // investment product, which for a year meant nothing recorded it at all:
    // 211 units across five investments were shelved next to ordinary flats.
    // ExPro's dictionary has two investment types — "Nieruchomość inwestycyjna"
    // and "Apartament inwestycyjny" — and both share this stem.
    function esm_is_investment($raw) {
        return str_contains(esm_norm($raw), 'inwestycyjn');
    }
}
if (!function_exists('esm_looks_like_house')) {
    // The investment name must never decide this. "Wille Biskupin" sells flats
    // — units on floors 0 to 2, 45 to 107 m², and ExPro correctly reports
    // Mieszkanie — but a rule that read "wille" as houses overruled that
    // correct answer and filed 72 flats as detached houses.
    //
    // ExPro's building_type_id is no substitute on its own either: bt=4 is
    // mostly house developments, yet Osiedle Ferrovia sits there with 100
    // units of 25-61 m², which are plainly flats.
    //
    // So the promotion is per unit and needs all three to agree: ExPro filed
    // the development as houses, this unit is on the ground floor, and it is
    // the size of a house. A unit that fails any of them stays what ExPro
    // called it. Applied per unit rather than per investment, which is what
    // makes genuinely mixed developments work: in Małe Wilczyce a 56 m²
    // ground-floor unit stays a flat while a 118 m² one does not.
    function esm_looks_like_house($u, $inv) {
        $bt = (string)($inv['building_type_id'] ?? '');
        if (!in_array($bt, ['4', '7'], true)) return false;
        $floor = trim((string)($u['floor'] ?? $u['Piętro'] ?? ''));
        if ($floor !== '' && $floor !== '0') return false;
        $area = (float) str_replace(',', '.', (string)($u['area_raw'] ?? $u['Powierzchnia'] ?? '0'));
        return $area >= 100.0;
    }
}
if (!function_exists('esm_name_says_commercial')) {
    // Kept only for premises, where the name is a statement of use rather than
    // marketing: "lokale usługowe" is what the thing is, not what it is called.
    function esm_name_says_commercial($name) {
        $n = esm_norm($name);
        foreach (['lokal uslug', 'lokale uslug', 'lokal uzytk', 'lokale uzytk',
                  'lokale biurowe', 'wykonczeni', 'zarzadzanie najmem'] as $p) {
            if (str_contains($n, $p)) return 'lokal-uzytkowy';
        }
        return '';
    }
}
if (!function_exists('esm_dom_subtype')) {
    // ExPro only ever says "Dom". Rather than invent a subtype — which is how
    // all 246 houses became "szeregowy" and left the "Dom wolnostojący" filter
    // permanently empty — the name is asked, and a bare `dom` is a perfectly
    // valid answer when it stays silent.
    function esm_dom_subtype($name) {
        $n = esm_norm($name);
        if (str_contains($n, 'szereg') || str_contains($n, 'segment')) return 'dom-szeregowy';
        if (str_contains($n, 'blizniak'))                              return 'dom-blizniaczy';
        // "wille"/"willa" is deliberately absent: it is a marketing word, not a
        // building type. Wille Biskupin sells flats.
        if (str_contains($n, 'wolnostoj'))                             return 'dom-wolnostojacy';
        return 'dom';
    }
}
if (!function_exists('esm_rodzaj')) {
    function esm_rodzaj($slug) {
        if ($slug === 'mieszkanie') return 'mieszkanie';
        if (str_starts_with($slug, 'dom')) return 'dom';
        if (str_starts_with($slug, 'lokal') || $slug === 'komercja') return 'lokal';
        return 'unknown';
    }
}
if (!function_exists('esm_resolve_type')) {
    function esm_resolve_type($u, $inv) {
        if (!empty($inv['type_override'])) return [$inv['type_override'], 'override'];

        $slug = esm_type_from_label($u['Typ'] ?? $u['type'] ?? '');
        $src  = $slug ? 'unit' : '';

        // The investment's filing in ExPro's dictionary — the fallback for a
        // unit that reports nothing. It is deliberately below the unit: Domy
        // pod Lasem is filed under Mieszkanie while every one of its units
        // reports Segment, and the unit is the thing being classified.
        if (!$slug) {
            foreach ((array)($inv['expro_types'] ?? []) as $t) {
                $slug = esm_type_from_label($t);
                if ($slug) { $src = 'expro_types'; break; }
            }
        }
        // Premises: the name states a use rather than a brand, so it may
        // correct a unit ExPro left as "Mieszkanie".
        if ($slug === 'mieszkanie') {
            $by_name = esm_name_says_commercial($inv['name'] ?? '');
            if ($by_name) { $slug = $by_name; $src = ($src ?: 'none') . '+name'; }
        }
        // Houses: decided by measurement, never by the name. All three of
        // building type, ground floor and house-sized area must agree.
        if ($slug === 'mieszkanie' && esm_looks_like_house($u, $inv)) {
            $slug = 'dom';
            $src  = ($src ?: 'none') . '+profil';
        }
        if ($slug === 'dom') $slug = esm_dom_subtype($inv['name'] ?? '');
        if (!$slug) return ['niesklasyfikowane', 'none'];
        return [$slug, $src];
    }
}
if (!function_exists('esm_put')) {
    function esm_put($post_id, $key, $val) {
        static $clearable = [
            // Price vanishing from the feed is a real state change; keeping the
            // old figure would advertise a price that no longer exists.
            'fave_property_price',
            'lokal_cena_m2',
            // Always present in the feed (the reader defaults it), listed so an
            // empty one is never silently kept.
            'lokal_status_expro',
            // Deliberately empty: ExPro has no bedroom/bathroom count and a
            // guess reads exactly like a measurement. Must stay clearable so an
            // invented value can never survive here again.
            'fave_property_bedrooms',
            'fave_property_bathrooms',
        ];
        $val      = (string) $val;
        $existing = (string) get_post_meta($post_id, $key, true);
        if ($val !== '') {
            update_post_meta($post_id, $key, $val);
            return;
        }
        // "None" is Python's str(None) that leaked through the old scraper, not
        // a value anyone stored on purpose. Without this the guard defends it:
        // the correct answer is empty, so the write is skipped and the garbage
        // survives every run. 832 rows came back this way after a cron ran the
        // old code over a database that had just been cleaned.
        if ($existing === 'None') {
            update_post_meta($post_id, $key, '');
            esm_stat('cleared');
            return;
        }
        if (in_array($key, $clearable, true)) {
            if ($existing !== '') esm_stat('cleared');
            update_post_meta($post_id, $key, '');
            return;
        }
        if ($existing !== '') esm_stat('kept');   // write suppressed — data survived
    }
}

$created = $updated = $skipped = 0;
$errors  = [];
$type_counts = [];
$src_counts  = [];
$unpublished = 0;

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
    [$type_slug, $type_src] = esm_resolve_type($u, $inv);
    $rodzaj = esm_rodzaj($type_slug);
    $typ_label = [
        'mieszkanie'        => 'Mieszkanie',
        'dom'               => 'Dom',
        'dom-szeregowy'     => 'Dom szeregowy',
        'dom-blizniaczy'    => 'Dom bliźniaczy',
        'dom-wolnostojacy'  => 'Dom wolnostojący',
        'lokal-uzytkowy'    => 'Lokal użytkowy',
        'niesklasyfikowane' => 'Nieruchomość',
    ][$type_slug] ?? 'Nieruchomość';
    $type_counts[$type_slug] = ($type_counts[$type_slug] ?? 0) + 1;
    $src_counts[$type_src]   = ($src_counts[$type_src] ?? 0) + 1;

    // Only real, addressable homes go live. Commercial premises have no
    // template of their own and would render through the houses layout, and an
    // unclassified unit has no business being advertised at all — better a
    // draft that shows up in the summary than a wrong page in the catalogue.
    $publishable   = in_array($rodzaj, ['mieszkanie', 'dom'], true);
    $target_status = $publishable ? 'publish' : 'draft';
    // A unit of an investment the developer forbids publishing must never be
    // public, whatever its own type says. The flag rides on the investment
    // ("Zakaz publikacji" in ExPro's detail header) and is carried in the
    // payload; the marker meta records the reason, which is not the same as
    // the draft the reconciler sets when a unit disappears from the feed.
    if (!empty($data['zakaz_publikacji'])) {
        $target_status = 'draft';
    }

    // ── Address ──────────────────────────────────────────────────────────
    $street  = $inv['street'] ?? '';
    $city    = $inv['city']   ?? '';
    $address = trim(($street ? $street . ', ' : '') . $city);
    $lat     = (string)($inv['latitude'] ?? $inv['lat'] ?? '');
    $lng     = (string)($inv['longitude'] ?? $inv['lng'] ?? '');
    // Houzez keeps the pair in ONE field, comma separated, and its own
    // save_property_post_type() hook re-derives houzez_geolocation_lat/long
    // from it after every meta write:
    //     $p = explode(',', get_post_meta($id,'fave_property_location',true));
    //     update_post_meta($id,'houzez_geolocation_long', $p[1]);
    // We were writing the bare latitude into that field, so $p[1] did not
    // exist and the hook overwrote the longitude with NULL a moment after we
    // set it — the maps stayed blank even once the coordinates arrived.
    $lat_lng = ($lat !== '' && $lng !== '') ? $lat . ',' . $lng : '';

    // ── Description (generated from available data) ───────────────────────
    $rooms_txt = $rooms ? $rooms . '-pokojowe' : '';
    $content = "{$typ_label}";
    if ($rooms_txt) $content .= " {$rooms_txt}";
    // Polish decimal comma, matching every other number on the page. The raw
    // value carries a dot ("99.88"), which the rendered page never uses.
    if ($area) { $area_txt = str_replace('.', ',', (string)$area); $content .= " o powierzchni {$area_txt} m²"; }
    if ($floor_disp && in_array($rodzaj, ['mieszkanie', 'lokal'], true)) {
        $content .= ", {$floor_disp}";
    }
    $content .= ". Inwestycja: <strong>{$inv['name']}</strong>";
    if ($city) $content .= ", {$city}";
    if ($street) $content .= ", ul. {$street}";
    $content .= ".";
    // A completion date already in the past reads as an abandoned listing.
    if ($delivery) {
        $d_ts = strtotime($delivery);
        $content .= ($d_ts && $d_ts < strtotime('today'))
            ? " Gotowe do odbioru."
            : " Termin oddania: {$delivery}.";
    }
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
    if (esm_is_yes($inv_extra['winda'] ?? ''))               $amenity_lines[] = "winda";
    if (esm_is_yes($inv_extra['smart_home'] ?? ''))          $amenity_lines[] = "Smart Home";
    if (esm_is_yes($inv_extra['stacja_ev'] ?? ''))           $amenity_lines[] = "stacja ładowania EV";
    if (esm_is_yes($inv_extra['miejsce_postojowe'] ?? ''))   $amenity_lines[] = "miejsce postojowe";
    if (esm_is_yes($inv_extra['komorki_lokatorskie'] ?? '')) $amenity_lines[] = "komórka lokatorska";
    if ($amenity_lines) {
        $content .= " Inwestycja wyposażona m.in. w: " . implode(', ', $amenity_lines) . ".";
    }
    $content .= " Skontaktuj się z naszym doradcą, aby uzyskać więcej informacji i umówić prezentację.";

    // ── Title ─────────────────────────────────────────────────────────────
    $title = $unit_name . ' — ' . $inv['name'];

    // ── Create or update post ────────────────────────────────────────────
    if (!empty($existing)) {
        $post_id   = (int)$existing[0];
        $upd_args  = ['ID' => $post_id, 'post_title' => $title, 'post_status' => $target_status];
        if (!empty($data['zakaz_publikacji'])) update_post_meta($post_id, '_expro_zakaz_publikacji', '1');
        elseif (get_post_meta($post_id, '_expro_zakaz_publikacji', true)) delete_post_meta($post_id, '_expro_zakaz_publikacji');
        if ($target_status === 'draft' && get_post_status($post_id) === 'publish') $unpublished++;
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
            'post_status'  => $target_status,
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
        'fave_property_location'      => $lat_lng,
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
        // Which signal placed this unit, so a wrong type can be traced to
        // its source instead of being re-guessed.
        'lokal_typ_slug'              => $type_slug,
        'lokal_typ_zrodlo'            => $type_src,
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
        esm_put($post_id, $k, $v);
    }

    // Unit amenities — only overwrite if scraper explicitly provided the key
    if (array_key_exists('has_balcony',  $u)) update_post_meta($post_id, 'lokal_balkon',  $u['has_balcony']  ? '1' : '0');
    if (array_key_exists('has_terrace',  $u)) update_post_meta($post_id, 'lokal_taras',   $u['has_terrace']  ? '1' : '0');
    if (array_key_exists('has_garden',   $u)) update_post_meta($post_id, 'lokal_ogrodek', $u['has_garden']   ? '1' : '0');
    if (array_key_exists('has_basement', $u)) update_post_meta($post_id, 'lokal_piwnica', $u['has_basement'] ? '1' : '0');
    if (array_key_exists('has_garage',   $u)) update_post_meta($post_id, 'lokal_garaz',   $u['has_garage']   ? '1' : '0');

    // ── Taxonomies ───────────────────────────────────────────────────────
    // A unit can now carry two labels. Order here means nothing —
    // wp_get_post_terms sorts by name, so "Inwestycyjne" sorts ahead of "Rynek
    // Pierwotny" wherever it is added. The catalogue used to take labels[0] as
    // the availability badge; that read is fixed in page-rynek-pierwotny.php
    // and page-katalog-mieszkan.php to select by slug instead of by position.
    $labels = [$label_tid];
    if ($label_inw_tid && esm_is_investment($u['Typ'] ?? $u['type'] ?? '')) {
        $labels[] = $label_inw_tid;
    }
    wp_set_object_terms($post_id, $labels, 'property_label');
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
    if (esm_is_yes($inv_extra['winda'] ?? ''))               $features[] = 'Winda';
    if (esm_is_yes($inv_extra['smart_home'] ?? ''))          $features[] = 'Smart Home';
    if (esm_is_yes($inv_extra['stacja_ev'] ?? ''))           $features[] = 'Stacja EV';
    if (esm_is_yes($inv_extra['miejsce_postojowe'] ?? ''))   $features[] = 'Miejsce postojowe';
    if (esm_is_yes($inv_extra['komorki_lokatorskie'] ?? '')) $features[] = 'Komórka lokatorska';
    if (esm_is_yes($inv_extra['pod_klucz'] ?? ''))           $features[] = 'Wykończenie pod klucz';

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
        }
        // There used to be a third branch here: media_sideload_image($plan_url),
        // which downloads the file straight from ExPro onto the WP server. It
        // runs whenever no redacted local copy is available — a plan whose
        // download failed during the scrape, or any run without the OCR stack —
        // and it bypasses the redactor completely, publishing plans with the
        // developer's name and the sales office's phone number still legible.
        // The rule is now absolute: nothing enters the media library without
        // passing plan_redactor. The URL is recorded below instead, and the
        // plan is picked up on a later run once a redacted copy exists.
        // Measured before removal: 0 of 7015 units currently reach this path
        // (5422 have a local file, 1593 are ExPro's logo placeholder), so no
        // plan is lost today.
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

$stats = esm_stat();
echo json_encode([
    'created' => $created,
    'updated' => $updated,
    'skipped' => $skipped,
    'errors'  => $errors,
    // How many writes the guard suppressed (values that would have been
    // erased) and how many it let through as deliberate clears. Reported so a
    // sudden jump in 'cleared' is visible instead of silent.
    'kept'    => $stats['kept'],
    'cleared' => $stats['cleared'],
    'types'       => $type_counts,
    'type_srcs'   => $src_counts,
    'unpublished' => $unpublished,
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
    """The `projekt` term for an investment, creating it if it is missing.

    This used to read the term off the inwestycja post, which could never work:
    the taxonomy is registered for `property` only, so an inwestycja post can
    hold no terms in it. The warning fired for all 167 investments, every unit
    was synced with term 0, and the catalogue templates — which look the term up
    by the investment's slug — fell through to the expro_lokale_json snapshot
    for the entire catalogue.

    Resolving by slug matches what those templates do, and creating on the spot
    keeps a new investment from silently landing on the snapshot path again.
    """
    php = f"""<?php
$p = get_post({post_id});
if (!$p) {{ echo '0'; return; }}
$term = get_term_by('slug', $p->post_name, 'projekt');
if (!$term) {{
    $r = wp_insert_term($p->post_title, 'projekt', ['slug' => $p->post_name]);
    if (is_wp_error($r)) {{ echo '0'; return; }}
    $tid = (int) $r['term_id'];
    // A language-less term is invisible on the front end.
    if (function_exists('pll_set_term_language') && function_exists('pll_default_language')) {{
        pll_set_term_language($tid, pll_default_language());
    }}
    echo $tid;
    return;
}}
echo (int) $term->term_id;
"""
    ssh.write_remote_file(php, '/tmp/esm_getterm.php')
    out = ssh.run_wp_cli('eval-file /tmp/esm_getterm.php')
    digits = "".join(ch for ch in out if ch.isdigit())
    return int(digits or '0')


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

    if redact_plan_image is None:
        # No OCR stack, so nothing can be blurred. Publishing the plans anyway
        # would put developer names and sales-office phone numbers straight
        # onto the site — the exact thing redaction exists to prevent. Skip the
        # upload entirely instead: every other field still syncs, and the plans
        # get imported on the next run from a machine that has tesseract.
        log(f'  WARN: pytesseract/tesseract missing — skipping upload of '
            f'{len(files_to_upload)} plan image(s) rather than publishing them unredacted')
        return

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

    # The scraper stores coordinates as latitude/longitude; this payload only
    # ever carried 'lat'/'lng', keys that exist nowhere in its output. The PHP
    # reads `$inv['latitude'] ?? $inv['lat']`, found neither, and wrote "" —
    # which is why fave_property_location and houzez_geolocation_lat/long are
    # empty on all 4917 property posts and no unit shows on a map. Both spellings
    # go through now, so the PHP finds them whichever key it reaches for.
    lat = str(inv.get('latitude') or inv.get('lat') or '').strip()
    lng = str(inv.get('longitude') or inv.get('lng') or '').strip()

    payload = {
        'units': units,
        'inv': {
            'name':      inv.get('name', ''),
            'expro_id':  str(inv.get('expro_id') or inv.get('id', '')),
            'city':      inv.get('city', ''),
            'street':    inv.get('street', ''),
            'district':  inv.get('district', ''),
            'latitude':  lat,
            'longitude': lng,
            'lat':       lat,
            'lng':       lng,
            'delivery':  inv.get('delivery', ''),
            # ExPro's own filing for this investment — the classifier's
            # fallback for units that report no type of their own.
            'expro_types': inv.get('expro_types', []),
            # How ExPro classifies the building itself. Needed by the
            # house-profile check; without it that check reads a missing key,
            # silently answers "not a house" for everything, and the promotion
            # never happens at all.
            'building_type_id': str(inv.get('building_type_id') or ''),
            # Escape hatch for an investment ExPro has plainly mislabelled.
            # Empty by design: an entry here is a claim we have checked.
            'type_override': _UNIT_TYPE_OVERRIDES.get(
                str(inv.get('expro_id') or inv.get('id', '')), ''),
        },
        'inv_extra':       inv.get('extra', {}),
        # Developer's refusal to have this investment published. Absent means
        # the dump predates the flag — treated as banned, because publishing
        # against a refusal is the failure that cost us a phone call.
        'zakaz_publikacji': bool(inv.get('zakaz_publikacji', True)),
        'parent_id':       parent_id,
        'projekt_term_id': projekt_term_id,
    }

    # Upload unit images to server (authenticated download already done by scraper).
    # Developer/investment name passed through so plan_redactor can blur them
    # if they're baked into the plan image itself (on top of the universal
    # phone/email/website/address patterns it always checks).
    upload_unit_images(ssh, units, extra_terms=[inv.get('developer', ''), inv.get('name', '')])

    if redact_plan_image is None:
        # Belt and braces. The PHP no longer has a branch that downloads the
        # plan straight from ExPro, so an unredacted file cannot reach the
        # media library either way — but sending plan URLs we have deliberately
        # not prepared only invites lokal_plan_url_expro to be filled with work
        # this run cannot do. Blank them; units that already have
        # lokal_plan_attachment_id keep the plan they have.
        payload['units'] = [
            {**u, 'plan_urls': [], 'plan_url_map': {}, 'plan_server_map': {}}
            for u in payload['units']
        ]

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
        kept, cleared = result.get('kept', 0), result.get('cleared', 0)
        if kept or cleared:
            log(f'  meta guard: {kept} write(s) suppressed (value survived), '
                f'{cleared} deliberate clear(s)')
        types = result.get('types', {})
        if types:
            log('  types: ' + ', '.join(f'{k}={v}' for k, v in sorted(types.items())))
            log('  signal: ' + ', '.join(f'{k or "none"}={v}'
                                        for k, v in sorted(result.get('type_srcs', {}).items())))
        if types.get('niesklasyfikowane'):
            log(f'  WARNING: {types["niesklasyfikowane"]} unit(s) no signal could place — left unpublished')
        if result.get('unpublished'):
            log(f'  {result["unpublished"]} previously published unit(s) moved to draft '
                f'(commercial premises or unclassified)')
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
