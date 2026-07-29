#!/usr/bin/env python3
"""
Give property_type a real shape before the classifier starts writing into it.

The tree is half-built today: dom-szeregowy and dom-wolnostojacy hang directly
under `mieszkalne`, as siblings of `mieszkanie`, and there is no `dom` node at
all — so "houses" is not something the taxonomy can express. `blizniak` is an
orphan at parent 0. Four terms carry no Polylang language, which on this site
means they are invisible to the front-end filters no matter what is assigned
to them (that is how the "Dom" filter came to return 0 against 258 houses).

What this does, all of it additive or free:

  * create `dom` under `mieszkalne`
  * reparent dom-szeregowy (278 posts) and dom-wolnostojacy (0) under it —
    slugs untouched, so no URL changes
  * rename the orphan `blizniak` to `dom-blizniaczy` and file it under `dom`;
    free to do because it holds 0 posts
  * create `niesklasyfikowane` for units the classifier cannot place, which
    are never published
  * give every language-less term the site's default language

Usage:
    python3 taxonomy_migrate.py            # dry run — prints the tree and the plan
    python3 taxonomy_migrate.py --apply    # write, after backing the tree up
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

BACKUP_DIR = Path(__file__).parent / "data"


def run_php(ssh: SSHClient, php: str, tag: str, timeout: int = 300) -> str:
    remote = f"/tmp/esm_tax_{tag}.php"
    ssh.write_remote_file(php, remote)
    try:
        return ssh.run_wp_cli(f"eval-file {remote}", timeout=timeout)
    finally:
        try:
            ssh.run(f"rm -f {remote}")
        except Exception:
            pass


def parse_json(out: str, tag: str):
    """Parse the JSON a snippet echoed, ignoring anything printed before it.

    WordPress and its plugins emit deprecation notices to stdout under WP-CLI,
    and one of them landed in front of the payload — which turned a migration
    that had in fact succeeded into a traceback. The result matters more than
    the noise around it, so the JSON is picked out of the tail.
    """
    out = (out or "").strip()
    for opener in ("[", "{"):
        idx = out.find(opener)
        if idx >= 0:
            try:
                return json.loads(out[idx:])
            except Exception:
                continue
    if out:
        log(f"  WARN {tag}: не вдалось розібрати відповідь: {out[:300]}")
    return None


SCAN = r"""<?php
$out = ['terms' => [], 'default_lang' => '', 'has_pll' => function_exists('pll_set_term_language')];
if (function_exists('pll_default_language')) $out['default_lang'] = (string) pll_default_language();
foreach (get_terms(['taxonomy' => 'property_type', 'hide_empty' => false]) as $t) {
    $out['terms'][] = [
        'id'     => (int) $t->term_id,
        'slug'   => $t->slug,
        'name'   => $t->name,
        'parent' => (int) $t->parent,
        'count'  => (int) $t->count,
        'lang'   => function_exists('pll_get_term_language') ? (string) pll_get_term_language($t->term_id) : '',
    ];
}
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""


def print_tree(terms: list[dict]) -> None:
    by_parent: dict[int, list[dict]] = {}
    for t in terms:
        by_parent.setdefault(t["parent"], []).append(t)

    def walk(parent: int, depth: int) -> None:
        for t in sorted(by_parent.get(parent, []), key=lambda x: x["slug"]):
            lang = t["lang"] or "БЕЗ МОВИ"
            pad = "  " * depth
            log(f"    {pad}{t['slug']:<28} id={t['id']:<5} постів={t['count']:<5} {lang}")
            walk(t["id"], depth + 1)

    walk(0, 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    ssh = SSHClient()
    ssh._connect()
    try:
        data = parse_json(run_php(ssh, SCAN, "scan"), "scan") or {}
        terms = data.get("terms", [])
        default_lang = data.get("default_lang") or "pl"
        by_slug = {t["slug"]: t for t in terms}

        log("── property_type зараз ──────────────────────────────────")
        print_tree(terms)
        log(f"  мова за замовчуванням: {default_lang}   Polylang API: "
            f"{'є' if data.get('has_pll') else 'НЕМАЄ'}")

        plan: list[str] = []
        dom = by_slug.get("dom")
        mieszkalne = by_slug.get("mieszkalne")
        if not mieszkalne:
            log("  FATAL: немає кореневого терміна 'mieszkalne' — зупиняюсь")
            return
        if not dom:
            plan.append(f"створити 'dom' під '{mieszkalne['slug']}' (id={mieszkalne['id']})")
        for slug in ("dom-szeregowy", "dom-wolnostojacy"):
            t = by_slug.get(slug)
            if t and (dom is None or t["parent"] != dom["id"]):
                plan.append(f"перепідвісити '{slug}' (постів {t['count']}) під 'dom' — slug не змінюється")
        bliz = by_slug.get("blizniak")
        if bliz:
            if bliz["count"] == 0:
                plan.append("перейменувати 'blizniak' → 'dom-blizniaczy' і підвісити під 'dom' (0 постів, безпечно)")
            else:
                plan.append(f"УВАГА: 'blizniak' має {bliz['count']} постів — slug НЕ чіпаю, лише parent")
        if "niesklasyfikowane" not in by_slug:
            plan.append("створити 'niesklasyfikowane' (кореневий, ніколи не публікується)")
        langless = [t["slug"] for t in terms if not t["lang"]]
        if langless:
            plan.append(f"призначити мову '{default_lang}' термінам без мови: {', '.join(langless)}")

        log("── що буде зроблено ─────────────────────────────────────")
        for i, p in enumerate(plan, 1):
            log(f"  {i}. {p}")
        if not plan:
            log("  нічого — дерево вже у потрібному стані")
            return

        if not args.apply:
            log("  DRY RUN — нічого не записано; для застосування додай --apply")
            return

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"taxonomy_backup_{stamp}.json"
        backup.write_text(json.dumps(terms, ensure_ascii=False, indent=1), "utf-8")
        log(f"  backup дерева → {backup.name}")

        php = r"""<?php
$lang = '%s';
$log  = [];
function esm_lang($tid, $lang) {
    if (function_exists('pll_set_term_language') && function_exists('pll_get_term_language')) {
        if (!pll_get_term_language($tid)) { pll_set_term_language($tid, $lang); return 'мову призначено'; }
        return 'мова вже є';
    }
    return 'Polylang недоступний';
}
$mieszkalne = get_term_by('slug', 'mieszkalne', 'property_type');
$dom = get_term_by('slug', 'dom', 'property_type');
if (!$dom) {
    $r = wp_insert_term('Dom', 'property_type', ['slug' => 'dom', 'parent' => (int)$mieszkalne->term_id]);
    if (is_wp_error($r)) { $log[] = 'ПОМИЛКА створення dom: '.$r->get_error_message(); }
    else { $dom = get_term((int)$r['term_id'], 'property_type'); $log[] = 'створено dom id='.$dom->term_id; }
}
if ($dom && !is_wp_error($dom)) {
    $log[] = 'dom: '.esm_lang((int)$dom->term_id, $lang);
    foreach (['dom-szeregowy', 'dom-wolnostojacy'] as $slug) {
        $t = get_term_by('slug', $slug, 'property_type');
        if (!$t) continue;
        if ((int)$t->parent !== (int)$dom->term_id) {
            $r = wp_update_term((int)$t->term_id, 'property_type', ['parent' => (int)$dom->term_id]);
            $log[] = is_wp_error($r) ? "ПОМИЛКА $slug: ".$r->get_error_message() : "$slug перепідвішено під dom";
        }
        $log[] = "$slug: ".esm_lang((int)$t->term_id, $lang);
    }
    $b = get_term_by('slug', 'blizniak', 'property_type');
    if ($b) {
        $args = ['parent' => (int)$dom->term_id];
        if ((int)$b->count === 0) { $args['name'] = 'Dom bliźniaczy'; $args['slug'] = 'dom-blizniaczy'; }
        $r = wp_update_term((int)$b->term_id, 'property_type', $args);
        $log[] = is_wp_error($r) ? 'ПОМИЛКА blizniak: '.$r->get_error_message()
                                 : 'blizniak оновлено ('.implode(', ', array_keys($args)).')';
        $log[] = 'blizniak: '.esm_lang((int)$b->term_id, $lang);
    }
}
$ns = get_term_by('slug', 'niesklasyfikowane', 'property_type');
if (!$ns) {
    $r = wp_insert_term('Niesklasyfikowane', 'property_type', ['slug' => 'niesklasyfikowane']);
    if (is_wp_error($r)) { $log[] = 'ПОМИЛКА niesklasyfikowane: '.$r->get_error_message(); }
    else { $ns = get_term((int)$r['term_id'], 'property_type'); $log[] = 'створено niesklasyfikowane id='.$ns->term_id; }
}
if ($ns && !is_wp_error($ns)) $log[] = 'niesklasyfikowane: '.esm_lang((int)$ns->term_id, $lang);
foreach (get_terms(['taxonomy' => 'property_type', 'hide_empty' => false]) as $t) {
    $r = esm_lang((int)$t->term_id, $lang);
    if ($r === 'мову призначено') $log[] = $t->slug.': мову призначено';
}
echo json_encode($log, JSON_UNESCAPED_UNICODE);
""" % default_lang

        for line in (parse_json(run_php(ssh, php, "apply"), "apply") or []):
            log(f"  {line}")

        log("── property_type після міграції ─────────────────────────")
        after = parse_json(run_php(ssh, SCAN, "scan2"), "scan2") or {}
        print_tree(after.get("terms", []))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
