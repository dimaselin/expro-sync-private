#!/usr/bin/env python3
"""
Give every investment its `projekt` term and put its units in it.

The catalogue pages look their units up with
get_term_by('slug', <investment slug>, 'projekt') and, finding nothing, fall
back to the expro_lokale_json snapshot stored on the investment post. There is
exactly one `projekt` term in the database — `raclawice-wielkie`, left over from
a project that was sold — so 0 of 167 investments take the live path. Every
catalogue card is drawn from a JSON blob, which is why none of the work built on
the posts (type segmentation, labels, reconciliation) shows up there.

The sync half of it was broken from the start in a way worth naming: the
taxonomy is registered for `property` only, but get_projekt_term_id() reads it
off the `inwestycja` post, where it can never exist. That is why the warning
fired for all 167 rather than for a handful.

Creates one term per investment (slug = the investment's post_name, which is
what the templates look up), assigns the investment's published units to it, and
sets the Polylang language — a language-less term is invisible on the front end.

Nothing is deleted and no post changes status. Re-running is safe: an existing
term is reused, and unit assignment is idempotent.

Usage:
    python3 projekt_terms.py                    # report only
    python3 projekt_terms.py --limit 1 --apply  # try one investment first
    python3 projekt_terms.py --apply            # all of them
    python3 projekt_terms.py --only kyriad-karkonosze --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log


def run_php(ssh: SSHClient, php: str, tag: str, timeout: int = 600):
    remote = f"/tmp/esm_projekt_{tag}.php"
    ssh.write_remote_file(php, remote)
    try:
        out = ssh.run_wp_cli(f"eval-file {remote}", timeout=timeout)
    finally:
        ssh.remove_remote_file(remote)
    start = min([x for x in (out.find("["), out.find("{")) if x >= 0], default=-1)
    if start < 0:
        log(f"  WARN {tag}: unexpected output: {out[:300]}")
        return None
    try:
        return json.loads(out[start:])
    except Exception:
        log(f"  WARN {tag}: could not parse: {out[:300]}")
        return None


def survey(ssh: SSHClient):
    """Every published investment, its slug, its unit count, and whether a term exists."""
    php = """<?php
global $wpdb; $t = $wpdb->prefix . 'expro_units';
$out = ['default_lang' => function_exists('pll_default_language') ? (string) pll_default_language() : '',
        'rows' => []];
$posts = $wpdb->get_results("SELECT ID, post_name, post_title FROM {$wpdb->posts}
    WHERE post_type='inwestycja' AND post_status='publish' ORDER BY post_title", ARRAY_A);
foreach ($posts as $p) {
    $term  = get_term_by('slug', $p['post_name'], 'projekt');
    $units = (int) $wpdb->get_var($wpdb->prepare(
        "SELECT COUNT(*) FROM {$t} u JOIN {$wpdb->posts} q ON q.ID = u.property_post_id
         WHERE u.investment_post_id = %d AND q.post_status = 'publish'", $p['ID']));
    $out['rows'][] = [
        'id'    => (int) $p['ID'],
        'slug'  => $p['post_name'],
        'title' => $p['post_title'],
        'units' => $units,
        'term'  => $term ? (int) $term->term_id : 0,
    ];
}
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""
    return run_php(ssh, php, "survey") or {}


def apply_one(ssh: SSHClient, inv_id: int, slug: str, title: str, lang: str):
    php = f"""<?php
global $wpdb; $t = $wpdb->prefix . 'expro_units';
$slug = '{slug}';
$out  = ['slug' => $slug];

$term = get_term_by('slug', $slug, 'projekt');
if ($term) {{
    $tid = (int) $term->term_id;
    $out['term'] = 'reused';
}} else {{
    $r = wp_insert_term({json.dumps(title, ensure_ascii=False)}, 'projekt', ['slug' => $slug]);
    if (is_wp_error($r)) {{ echo json_encode(['error' => $r->get_error_message(), 'slug' => $slug]); return; }}
    $tid = (int) $r['term_id'];
    $out['term'] = 'created';
}}
$out['term_id'] = $tid;

// Invisible on the front end without one.
if (function_exists('pll_set_term_language') && function_exists('pll_get_term_language')) {{
    if (!pll_get_term_language($tid)) {{ pll_set_term_language($tid, '{lang}'); $out['lang'] = 'set'; }}
    else $out['lang'] = (string) pll_get_term_language($tid);
}} else {{
    $out['lang'] = 'POLYLANG MISSING';
}}

$ids = $wpdb->get_col($wpdb->prepare(
    "SELECT u.property_post_id FROM {{$t}} u JOIN {{$wpdb->posts}} q ON q.ID = u.property_post_id
     WHERE u.investment_post_id = %d AND q.post_status = 'publish'", {int(inv_id)}));
$n = 0;
foreach ($ids as $id) {{
    // append=false would be the same set; this keeps a unit in exactly one project.
    $res = wp_set_object_terms((int) $id, [$tid], 'projekt');
    if (!is_wp_error($res)) $n++;
}}
$out['units_tagged'] = $n;
$out['units_found']  = count($ids);
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""
    return run_php(ssh, php, f"apply{inv_id}") or {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (without it, report only)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N investments")
    ap.add_argument("--only", default="", help="a single investment slug")
    args = ap.parse_args()

    ssh = SSHClient()
    ssh._connect()
    try:
        data = survey(ssh)
        rows = data.get("rows", [])
        lang = data.get("default_lang") or "pl"
        have = [r for r in rows if r["term"]]
        need = [r for r in rows if not r["term"] and r["units"] > 0]
        empty = [r for r in rows if not r["term"] and r["units"] == 0]

        log(f"── {len(rows)} published investments, default language '{lang}' ──")
        log(f"  already have a projekt term : {len(have)}")
        log(f"  need one (have units)       : {len(need)}   "
            f"({sum(r['units'] for r in need)} units)")
        log(f"  skipped (no published units): {len(empty)}")

        if args.only:
            need = [r for r in need if r["slug"] == args.only] or \
                   [r for r in rows if r["slug"] == args.only]
            if not need:
                log(f"No investment with slug '{args.only}'.")
                return
        if args.limit:
            need = sorted(need, key=lambda r: r["units"])[:args.limit]

        log(f"── to process now: {len(need)} ──")
        for r in need[:12]:
            log(f"  {r['slug']:<44} {r['units']:>5} units")
        if len(need) > 12:
            log(f"  … and {len(need) - 12} more")

        if not args.apply:
            log("REPORT ONLY — nothing written. Add --apply.")
            return

        done = tagged = 0
        for r in need:
            res = apply_one(ssh, r["id"], r["slug"], r["title"], lang)
            if res.get("error"):
                log(f"  ERROR {r['slug']}: {res['error']}")
                continue
            done += 1
            tagged += int(res.get("units_tagged", 0))
            log(f"  {r['slug']:<44} term {res.get('term')}, "
                f"{res.get('units_tagged')}/{res.get('units_found')} units, lang {res.get('lang')}")
        log(f"── {done} investment(s), {tagged} unit(s) tagged ──")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
