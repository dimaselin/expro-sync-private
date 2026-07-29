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
    "SELECT p.ID, p.post_title, p.post_status, pm.meta_value AS expro_id,
            uid.meta_value  AS unit_uuid,
            typ.meta_value  AS typ_slug,
            zrd.meta_value  AS typ_zrodlo,
            pln.meta_value  AS plan_att
     FROM {{$wpdb->posts}} p
     JOIN {{$wpdb->postmeta}} pm ON pm.post_id = p.ID AND pm.meta_key = 'expro_id'
     LEFT JOIN {{$wpdb->postmeta}} uid ON uid.post_id = p.ID AND uid.meta_key = 'expro_unit_id'
     LEFT JOIN {{$wpdb->postmeta}} typ ON typ.post_id = p.ID AND typ.meta_key = 'lokal_typ_slug'
     LEFT JOIN {{$wpdb->postmeta}} zrd ON zrd.post_id = p.ID AND zrd.meta_key = 'lokal_typ_zrodlo'
     LEFT JOIN {{$wpdb->postmeta}} pln ON pln.post_id = p.ID AND pln.meta_key = 'lokal_plan_attachment_id'
     WHERE p.post_type = 'property' AND p.post_status IN ('publish','draft')
       AND pm.meta_value IN ($placeholders)",
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
    # Two indexes: the UUID is exact and is what ExPro itself uses, the title
    # prefix is the fallback for posts that predate the UUID migration.
    prop_by_uuid = {}
    prop_by_key = {}
    for p in properties:
        if p.get("unit_uuid"):
            prop_by_uuid[p["unit_uuid"]] = p
        title = p["post_title"] or ""
        prefix = title.split(" — ")[0].strip() if " — " in title else title.strip()
        prop_by_key[(p["expro_id"], prefix)] = p

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
            uuid = u.get("realestate_id") or ""
            prop = prop_by_uuid.get(uuid) or prop_by_key.get((eid, name))
            prop_id = int(prop["ID"]) if prop else None
            if prop_id is None:
                unmatched += 1
            rows.append({
                "investment_post_id": pid,
                "property_post_id": prop_id,
                "expro_investment_id": eid,
                "unit_name": name,
                "expro_unit_uuid": uuid or None,
                "currency": u.get("currency") or None,
                "typ_slug": (prop or {}).get("typ_slug") or None,
                "typ_zrodlo": (prop or {}).get("typ_zrodlo") or None,
                "plan_attachment_id": int(prop["plan_att"]) if prop and (prop.get("plan_att") or "").isdigit() else None,
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


# Columns added after the table was first built. Kept as an explicit list so a
# fresh install and an existing one converge on the same shape, and so this can
# run on every sync without a separate migration step.
_NEW_COLUMNS = {
    # ExPro's own unit identity. Matching property posts by the prefix of their
    # title was the only option when the table was written; the UUID is exact,
    # and every property post has carried it since the API migration.
    "expro_unit_uuid":    "varchar(64) DEFAULT NULL",
    "currency":           "varchar(8) DEFAULT NULL",
    "plan_attachment_id": "bigint(20) unsigned DEFAULT NULL",
    # What the classifier decided and which signal decided it, so a wrong type
    # can be traced without re-deriving it.
    "typ_slug":           "varchar(32) DEFAULT NULL",
    "typ_zrodlo":         "varchar(32) DEFAULT NULL",
    # Lifecycle. ExPro simply stops listing a unit when it sells — there is no
    # "sold" status in the feed — so disappearance is the only signal there is,
    # and it has to be recorded rather than inferred later.
    "first_seen_at":      "datetime DEFAULT NULL",
    "last_seen_at":       "datetime DEFAULT NULL",
    "gone_at":            "datetime DEFAULT NULL",
}


def ensure_schema(ssh: SSHClient) -> None:
    """Add any missing column. Safe to run every time."""
    php = """<?php
global $wpdb; $t = $wpdb->prefix . 'expro_units';
$have = [];
foreach ($wpdb->get_results("SHOW COLUMNS FROM {$t}") as $c) $have[] = $c->Field;
echo json_encode($have);
"""
    ssh.write_remote_file(php, "/tmp/esm_norm_cols.php")
    have = set(json.loads(ssh.run_wp_cli("eval-file /tmp/esm_norm_cols.php", timeout=60)))
    missing = {c: d for c, d in _NEW_COLUMNS.items() if c not in have}
    if not missing:
        return
    adds = ", ".join(f"ADD COLUMN `{c}` {d}" for c, d in missing.items())
    php_alter = (
        "<?php\nglobal $wpdb; $t = $wpdb->prefix . 'expro_units';\n"
        f"$wpdb->query(\"ALTER TABLE {{$t}} {adds}\");\n"
        "$wpdb->query(\"ALTER TABLE {$t} ADD INDEX idx_unit_uuid (expro_unit_uuid)\");\n"
        "$wpdb->query(\"ALTER TABLE {$t} ADD INDEX idx_gone (gone_at)\");\n"
        "echo $wpdb->last_error ?: 'OK';\n"
    )
    ssh.write_remote_file(php_alter, "/tmp/esm_norm_alter.php")
    log(f"  schema: adding {len(missing)} column(s) — {', '.join(missing)}")
    log(f"  {ssh.run_wp_cli('eval-file /tmp/esm_norm_alter.php', timeout=120).strip()}")


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
$numeric_cols = ['investment_post_id', 'property_post_id', 'price', 'price_per_m2', 'area', 'rooms',
                 'plan_attachment_id'];
$str_cols = ['expro_investment_id', 'unit_name', 'status', 'unit_type', 'price_raw',
             'price_per_m2_raw', 'area_raw', 'floor', 'delivery_date', 'stage', 'raw_json', 'synced_at',
             'expro_unit_uuid', 'currency', 'typ_slug', 'typ_zrodlo', 'first_seen_at', 'last_seen_at'];
$all_cols = array_merge($numeric_cols, $str_cols);
$col_list = implode(', ', $all_cols);
$update_list = implode(', ', array_map(function ($c) { return "$c=VALUES($c)"; },
    array_diff($all_cols, ['expro_investment_id', 'unit_name', 'first_seen_at'])));
// first_seen_at is written once, on insert, and never touched again — it is
// the day the unit appeared, not the day we last looked at it.

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


def push_rows(ssh: SSHClient, rows: list, chunk_size: int = 200) -> str:
    # UTC, because that is what the database runs on. Stamping these with the
    # local clock put every row two hours into the future as far as MySQL was
    # concerned, so "gone_at < NOW()" was false for all 744 marked units and
    # the reconciliation found nothing to do while looking like it worked.
    synced_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    total = len(rows)
    ssh.write_remote_file(_PUSH_ROWS_PHP, "/tmp/esm_norm_push.php")
    for i in range(0, total, chunk_size):
        chunk = rows[i:i + chunk_size]
        for r in chunk:
            r["synced_at"] = synced_at
            r["first_seen_at"] = synced_at
            r["last_seen_at"] = synced_at
        ssh.write_remote_file(json.dumps(chunk, ensure_ascii=False), "/tmp/esm_norm_rows.json")
        out = ssh.run_wp_cli("eval-file /tmp/esm_norm_push.php", timeout=120)
        log(f"  chunk {i}-{i+len(chunk)}/{total}: {out.strip()}")
    return synced_at


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


def mark_gone(ssh: SSHClient, synced_at: str, full_run: bool) -> dict:
    """Flag units this run did not see, and un-flag any that came back.

    ExPro has no "sold" status — a unit that sells simply stops being listed,
    which is why 467 units sit published on the site with data frozen at the
    day they vanished. Disappearance is the only signal available, so it is
    recorded here rather than guessed at later. Nothing is deleted: gone_at is
    a date, and a unit that reappears has it cleared.

    Only a full run may mark anything. A partial run sees a handful of
    investments by design, and would otherwise declare the entire rest of the
    catalogue gone.
    """
    if not full_run:
        return {"skipped": "partial run — gone_at only updated on --all"}
    php = f"""<?php
global $wpdb; $t = $wpdb->prefix . 'expro_units';
$seen = '{synced_at}';
$newly = $wpdb->query($wpdb->prepare(
    "UPDATE {{$t}} SET gone_at = %s WHERE gone_at IS NULL AND (last_seen_at IS NULL OR last_seen_at < %s)",
    $seen, $seen));
$back = $wpdb->query($wpdb->prepare(
    "UPDATE {{$t}} SET gone_at = NULL WHERE gone_at IS NOT NULL AND last_seen_at >= %s", $seen));
$total_gone = (int)$wpdb->get_var("SELECT COUNT(*) FROM {{$t}} WHERE gone_at IS NOT NULL");
echo json_encode(['newly_gone'=>(int)$newly, 'returned'=>(int)$back, 'gone_total'=>$total_gone]);
"""
    ssh.write_remote_file(php, "/tmp/esm_norm_gone.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_norm_gone.php", timeout=180)
    try:
        return json.loads(out[out.find("{"):])
    except Exception:
        return {"error": out[:200]}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--investment-ids", help="Comma-separated WP post IDs (small sample run)")
    g.add_argument("--all", action="store_true", help="Backfill all investments")
    ap.add_argument("--dry-run", action="store_true", help="Parse and match only, no DB writes")
    args = ap.parse_args()

    ssh = SSHClient()

    log("Ensuring table schema is current...")
    ensure_schema(ssh)

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
    synced_at = push_rows(ssh, rows)

    log("Marking units no longer in the feed...")
    log(f"  {mark_gone(ssh, synced_at, full_run=bool(args.all))}")

    log("Reconciliation:")
    report = reconcile(ssh)
    log(f"  {report}")


if __name__ == "__main__":
    main()
