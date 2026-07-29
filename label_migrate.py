#!/usr/bin/env python3
"""
Give property_label the term that carries axis B.

Type segmentation split the catalogue on two axes: property_type says what a
unit physically is and picks the template, property_label says what kind of
product it is and drives the catalogue. Only axis A was ever built — every unit
carries "Rynek Pierwotny" and nothing else, so the 211 units ExPro files under
"Nieruchomość inwestycyjna" are indistinguishable from ordinary flats. They are
physically flats, correctly, but nothing records that they are sold as an
investment product.

Creates one term: `inwestycyjne`. Not `condohotel` — the plan named it, but
ExPro's own dictionary has no such type and nothing in the feed implies one, so
there is nothing to put in it.

A term with no Polylang language is invisible on the front end (a filter over
one silently returned nothing for 25 terms once), so the language is assigned
here rather than left to whoever notices.

Usage:
    python3 label_migrate.py            # report what it would do
    python3 label_migrate.py --apply    # create the term
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

# name → slug. Only what the feed can actually justify.
NEW_LABELS = {
    "Inwestycyjne": "inwestycyjne",
}


def run_php(ssh: SSHClient, php: str, tag: str, timeout: int = 300):
    remote = f"/tmp/esm_label_{tag}.php"
    ssh.write_remote_file(php, remote)
    try:
        out = ssh.run_wp_cli(f"eval-file {remote}", timeout=timeout)
    finally:
        ssh.remove_remote_file(remote)
    start = min([x for x in (out.find("["), out.find("{")) if x >= 0], default=-1)
    if start < 0:
        log(f"  WARN {tag}: unexpected output: {out[:200]}")
        return None
    try:
        return json.loads(out[start:])
    except Exception:
        log(f"  WARN {tag}: could not parse: {out[:200]}")
        return None


def survey(ssh: SSHClient):
    php = """<?php
$out = ['terms' => [], 'default_lang' => '',
        'has_pll' => function_exists('pll_set_term_language')];
if (function_exists('pll_default_language')) $out['default_lang'] = (string) pll_default_language();
foreach (get_terms(['taxonomy' => 'property_label', 'hide_empty' => false]) as $t) {
    $out['terms'][] = [
        'id'    => (int) $t->term_id,
        'slug'  => $t->slug,
        'name'  => $t->name,
        'count' => (int) $t->count,
        'lang'  => function_exists('pll_get_term_language') ? (string) pll_get_term_language($t->term_id) : '',
    ];
}
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""
    return run_php(ssh, php, "survey") or {}


def create(ssh: SSHClient, wanted: dict, lang: str):
    pairs = ", ".join(f"'{slug}' => '{name}'" for name, slug in wanted.items())
    php = f"""<?php
$lang = '{lang}';
$log  = [];
foreach ([{pairs}] as $slug => $name) {{
    $t = get_term_by('slug', $slug, 'property_label');
    if ($t) {{
        $tid = (int) $t->term_id;
        $log[] = "$slug: already exists (id $tid)";
    }} else {{
        $r = wp_insert_term($name, 'property_label', ['slug' => $slug]);
        if (is_wp_error($r)) {{ $log[] = "$slug: ERROR " . $r->get_error_message(); continue; }}
        $tid = (int) $r['term_id'];
        $log[] = "$slug: created (id $tid)";
    }}
    // A language-less term is invisible on the front end.
    if (function_exists('pll_set_term_language') && function_exists('pll_get_term_language')) {{
        if (!pll_get_term_language($tid)) {{
            pll_set_term_language($tid, $lang);
            $log[] = "$slug: language '$lang' assigned";
        }} else {{
            $log[] = "$slug: language already '" . pll_get_term_language($tid) . "'";
        }}
    }} else {{
        $log[] = "$slug: Polylang unavailable — TERM WILL BE INVISIBLE";
    }}
}}
echo json_encode($log, JSON_UNESCAPED_UNICODE);
"""
    return run_php(ssh, php, "create") or []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="create the term (without it, report only)")
    args = ap.parse_args()

    ssh = SSHClient()
    ssh._connect()
    try:
        data = survey(ssh)
        terms = data.get("terms", [])
        lang = data.get("default_lang") or "pl"
        log(f"── property_label now ({len(terms)} terms, default language '{lang}', "
            f"Polylang API: {'yes' if data.get('has_pll') else 'NO'}) ──")
        for t in sorted(terms, key=lambda x: -x["count"]):
            log(f"  {t['slug']:<22} id={t['id']:<5} posts={t['count']:<6} {t['lang'] or 'NO LANGUAGE'}")

        have = {t["slug"] for t in terms}
        missing = {n: s for n, s in NEW_LABELS.items() if s not in have}
        if not missing:
            log("Nothing to create — every label already exists.")
        else:
            log(f"── to create: {', '.join(missing.values())} ──")

        if not args.apply:
            log("REPORT ONLY — nothing written. Add --apply to create.")
            return

        for line in create(ssh, NEW_LABELS, lang):
            log(f"  {line}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
