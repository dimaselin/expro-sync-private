"""
Phase 1 (additive only) — populate k5ew_expro_units from the LIVE expro_lokale_json
meta already on each `inwestycja` post. Read-only against existing meta: never
writes to expro_lokale_json/expro_investment_json, never touches any template.

Reads the unit list format written by wp_sync.py's normalize_units() (Polish
keys: Nazwa/Status/Typ/Powierzchnia/Cena/Cena m2/Pokoje/Piętro/Termin oddania/Etap).
Matches property posts the same way mieszkania_sync.py's post-title convention
works: property.post_title == "{unit_name} — {investment_name}", property.expro_id
meta == investment's expro_id.

Usage:
    python3 normalize_units_table.py --investment-ids 32027,32359   # small sample
    python3 normalize_units_table.py --all                          # full backfill
    python3 normalize_units_table.py --all --dry-run                # parse+match only, no DB writes
"""
import argparse
import json
import re
import sys
import datetime

from wp_sync import SSHClient, log


def parse_price(raw: str):
    if not raw:
        return None
    digits = re.sub(r"[^\d,\.]", "", raw).replace(",", ".")
    # keep only the first decimal point if multiple dots slipped through
    parts = digits.split(".")
    if len(parts) > 2:
        digits = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return round(float(digits), 2) if digits else None
    except ValueError:
        return None


def parse_area(raw: str):
    return parse_price(raw)  # same "1 234,56 m2" style numeric format


def parse_rooms(raw: str):
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    return int(m.group()) if m else None


def fetch_investments(ssh: SSHClient, investment_ids=None) -> list:
    where = ""
    if investment_ids:
        ids_csv = ",".join(str(i) for i in investment_ids)
        where = f"'post__in' => [{ids_csv}],"
    php = f"""<?php
$args = ['post_type'=>'inwestycja','posts_per_page'=>-1,'post_status'=>'publish','fields'=>'ids', {where}];
$ids = get_posts($args);
$out = [];
foreach ($ids as $id) {{
    $out[] = [
        'post_id'   => $id,
        'name'      => get_the_title($id),
        'expro_id'  => get_post_meta($id, 'expro_id', true),
        'lokale'    => get_post_meta($id, 'expro_lokale_json', true),
    ];
}}
echo json_encode($out);
"""
    ssh.write_remote_file(php, "/tmp/esm_norm_fetch_inv.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_norm_fetch_inv.php")
    return json.loads(out)


def fetch_properties(ssh: SSHClient, expro_ids: list) -> list:
    """Bulk-fetch property posts (id, title, expro_id) for the given investment expro_ids."""
    ids_csv = ",".join(f"'{e}'" for e in expro_ids if e)
    if not ids_csv:
        return []
    php = f"""<?php
global $wpdb;
$expro_ids = [{ids_csv}];
$placeholders = implode(',', array_fill(0, count($expro_ids), '%s'));
$sql = $wpdb->prepare(
    "SELECT p.ID, p.post_title, pm.meta_value AS expro_id
     FROM {{$wpdb->posts}} p
     JOIN {{$wpdb->postmeta}} pm ON pm.post_id = p.ID AND pm.meta_key = 'expro_id'
     WHERE p.post_type = 'property' AND p.post_status = 'publish' AND pm.meta_value IN ($placeholders)",
    $expro_ids
);
$rows = $wpdb->get_results($sql, ARRAY_A);
echo json_encode($rows);
"""
    ssh.write_remote_file(php, "/tmp/esm_norm_fetch_prop.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_norm_fetch_prop.php", timeout=120)
    return json.loads(out)


def build_rows(investments: list, properties: list) -> list:
    # property_post_id lookup: (expro_id, unit_name_prefix) -> post_id
    prop_by_key = {}
    for p in properties:
        title = p["post_title"] or ""
        prefix = title.split(" — ")[0].strip() if " — " in title else title.strip()
        prop_by_key[(p["expro_id"], prefix)] = int(p["ID"])

    rows = []
    unmatched = 0
    for inv in investments:
        eid = inv.get("expro_id") or ""
        pid = inv["post_id"]
        raw = inv.get("lokale") or ""
        try:
            units = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            log(f"  WARN: bad expro_lokale_json for post {pid} ({inv.get('name')}) — skipping")
            continue
        if not isinstance(units, list):
            continue

        for u in units:
            name = u.get("Nazwa") or ""
            if not name:
                continue
            prop_id = prop_by_key.get((eid, name))
            if prop_id is None:
                unmatched += 1
            rows.append({
                "investment_post_id": pid,
                "property_post_id": prop_id,
                "expro_investment_id": eid,
                "unit_name": name,
                "status": u.get("Status") or None,
                "unit_type": u.get("Typ") or None,
                "price_raw": u.get("Cena") or None,
                "price": parse_price(u.get("Cena")),
                "price_per_m2_raw": u.get("Cena m2") or None,
                "price_per_m2": parse_price(u.get("Cena m2")),
                "area_raw": u.get("Powierzchnia") or None,
                "area": parse_area(u.get("Powierzchnia")),
                "rooms": parse_rooms(u.get("Pokoje")),
                "floor": u.get("Piętro") or None,
                "delivery_date": u.get("Termin oddania") or None,
                "stage": u.get("Etap") or None,
                "raw_json": json.dumps(u, ensure_ascii=False),
            })
    return rows, unmatched


# Static PHP script (data-independent — no string templating of row content,
# so JSON/quote characters in raw_json etc. can never break PHP syntax).
# Rows are passed in as a JSON file and inserted via $wpdb->prepare().
_PUSH_ROWS_PHP = r"""<?php
global $wpdb;
$table = $wpdb->prefix . 'expro_units';
$rows = json_decode(file_get_contents('/tmp/esm_norm_rows.json'), true);
if (!is_array($rows)) {
    echo "ERROR: could not decode rows JSON\n";
    exit(1);
}

// Numeric columns are emitted as literal NULL or a cast-validated number
// directly in the query, bypassing %s placeholders. $wpdb->prepare's %s
// coerces a PHP null argument to '' (empty string), which MySQL then
// silently casts to 0 in a numeric column — turning "no data" into a fake
// zero price/area/rooms. That distinction matters here (a real 0 could be
// misread as "free apartment"), so these columns never go through %s.
$numeric_cols = ['investment_post_id', 'property_post_id', 'price', 'price_per_m2', 'area', 'rooms'];
$str_cols = ['expro_investment_id', 'unit_name', 'status', 'unit_type', 'price_raw',
             'price_per_m2_raw', 'area_raw', 'floor', 'delivery_date', 'stage', 'raw_json', 'synced_at'];
$all_cols = array_merge($numeric_cols, $str_cols);
$col_list = implode(', ', $all_cols);
$update_list = implode(', ', array_map(function ($c) { return "$c=VALUES($c)"; },
    array_diff($all_cols, ['expro_investment_id', 'unit_name'])));

function esm_numeric_literal($v) {
    if ($v === null) return 'NULL';
    return is_int($v) || (is_float($v) && $v == (int)$v) ? (string)(int)$v : (string)(float)$v;
}

$affected = 0;
foreach ($rows as $r) {
    $numeric_literals = array_map(function ($c) use ($r) {
        return esm_numeric_literal(array_key_exists($c, $r) ? $r[$c] : null);
    }, $numeric_cols);
    $placeholders = '(' . implode(',', $numeric_literals) . ',' .
        implode(',', array_fill(0, count($str_cols), '%s')) . ')';
    $str_values = [];
    foreach ($str_cols as $c) {
        $str_values[] = array_key_exists($c, $r) ? $r[$c] : null;
    }
    $sql = $wpdb->prepare(
        "INSERT INTO {$table} ({$col_list}) VALUES {$placeholders} ON DUPLICATE KEY UPDATE {$update_list}",
        $str_values
    );
    $result = $wpdb->query($sql);
    if ($result === false) {
        echo "ERROR: " . $wpdb->last_error . " (unit_name=" . $r['unit_name'] . ")\n";
        exit(1);
    }
    $affected++;
}
echo "OK rows_affected={$affected}\n";
"""


def push_rows(ssh: SSHClient, rows: list, chunk_size: int = 200):
    synced_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(rows)
    ssh.write_remote_file(_PUSH_ROWS_PHP, "/tmp/esm_norm_push.php")
    for i in range(0, total, chunk_size):
        chunk = rows[i:i + chunk_size]
        for r in chunk:
            r["synced_at"] = synced_at
        ssh.write_remote_file(json.dumps(chunk, ensure_ascii=False), "/tmp/esm_norm_rows.json")
        out = ssh.run_wp_cli("eval-file /tmp/esm_norm_push.php", timeout=120)
        log(f"  chunk {i}-{i+len(chunk)}/{total}: {out.strip()}")


def reconcile(ssh: SSHClient) -> dict:
    php = """<?php
global $wpdb;
$table = $wpdb->prefix . 'expro_units';
$total = (int)$wpdb->get_var("SELECT COUNT(*) FROM {$table}");
$matched = (int)$wpdb->get_var("SELECT COUNT(*) FROM {$table} WHERE property_post_id IS NOT NULL");
$investments = (int)$wpdb->get_var("SELECT COUNT(DISTINCT investment_post_id) FROM {$table}");
$null_price = (int)$wpdb->get_var("SELECT COUNT(*) FROM {$table} WHERE price IS NULL");
$null_area = (int)$wpdb->get_var("SELECT COUNT(*) FROM {$table} WHERE area IS NULL");
echo json_encode(compact('total','matched','investments','null_price','null_area'));
"""
    ssh.write_remote_file(php, "/tmp/esm_norm_reconcile.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_norm_reconcile.php")
    return json.loads(out)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--investment-ids", help="Comma-separated WP post IDs (small sample run)")
    g.add_argument("--all", action="store_true", help="Backfill all investments")
    ap.add_argument("--dry-run", action="store_true", help="Parse and match only, no DB writes")
    args = ap.parse_args()

    ssh = SSHClient()

    investment_ids = None
    if args.investment_ids:
        investment_ids = [int(x) for x in args.investment_ids.split(",")]

    log("Fetching investments (live expro_lokale_json)...")
    investments = fetch_investments(ssh, investment_ids)
    log(f"  {len(investments)} investments fetched.")

    expro_ids = list({inv.get("expro_id") for inv in investments if inv.get("expro_id")})
    log(f"Fetching property posts for {len(expro_ids)} distinct expro_id(s)...")
    properties = fetch_properties(ssh, expro_ids)
    log(f"  {len(properties)} property posts fetched.")

    rows, unmatched = build_rows(investments, properties)
    total_units = sum(1 for _ in rows)
    log(f"Parsed {total_units} units across {len(investments)} investments. Unmatched property_post_id: {unmatched}")

    if args.dry_run:
        log("DRY RUN — not writing to DB. Sample rows:")
        for r in rows[:5]:
            log(f"  {r}")
        return

    if not rows:
        log("Nothing to push.")
        return

    log("Pushing rows (INSERT ... ON DUPLICATE KEY UPDATE)...")
    push_rows(ssh, rows)

    log("Reconciliation:")
    report = reconcile(ssh)
    log(f"  {report}")


if __name__ == "__main__":
    main()
