#!/usr/bin/env python3
"""
One-off cleanup of what the pre-step-1 code left in the database.

The code defects themselves are fixed (see the "Five fields that were wrong
rather than missing" commit); this removes the rows those defects wrote. It is
deliberately a separate, resumable, dry-run-by-default script rather than part
of the sync, because it deletes and rewrites data the sync would never touch
again on its own.

Three independent parts:

  none      fave_property_rooms / lokal_pietro etc. holding the literal string
            "None" — Python's str(None) reaching the database through the
            scraper. 531 + 515 rows measured on 2026-07-28.

  features  property_feature terms applied because !empty("Nie") is true in
            PHP. A term is removed only where the investment's own expro_*
            meta says an explicit no; where the meta is empty there is no
            evidence either way, so the term is left alone and reported.

  gallery   investments whose featured image is pictures[0] instead of ExPro's
            designated `picture`. Reorders projekt_galeria and re-points
            _thumbnail_id at the intended attachment; imports nothing.

Usage:
    python3 cleanup_after_step1.py                     # dry run, all parts
    python3 cleanup_after_step1.py --part features     # dry run, one part
    python3 cleanup_after_step1.py --part none --apply # write
    python3 cleanup_after_step1.py --apply             # write, all parts

Every --apply writes data/cleanup_backup_<part>_<timestamp>.json first and
refuses to continue if that file cannot be written.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

try:
    from config import EXPRO
    BASE_URL = EXPRO["base_url"]
except Exception:
    BASE_URL = "https://expro.expander.pl"

BACKUP_DIR = Path(__file__).parent / "data"

# property_feature term name -> the inwestycja meta that justifies it.
# These six are investment-level: mieszkania_sync applies them to every unit of
# the investment, which is why a single "Nie" mislabels thousands of units.
FEATURE_SOURCES = {
    "Winda":                 "expro_winda",
    "Smart Home":            "expro_smart_home",
    "Stacja EV":             "expro_stacja_ev",
    "Miejsce postojowe":     "expro_parking",
    "Komórka Lokatorska":    "expro_komorki",
    "Komórka lokatorska":    "expro_komorki",
    "Wykończenie pod klucz": "expro_pod_klucz",
}

# Same rule as esm_is_yes() in mieszkania_sync.py — keep the two in step.
NEGATIVE = {"nie", "no", "brak", "nie dotyczy", "n/d", "nd", "-", "—", "0", "false"}


def is_yes(v: str) -> bool:
    v = (v or "").strip().lower()
    return bool(v) and v not in NEGATIVE


def run_php(ssh: SSHClient, php: str, tag: str, timeout: int = 600) -> str:
    remote = f"/tmp/esm_cleanup_{tag}.php"
    ssh.write_remote_file(php, remote)
    try:
        return ssh.run_wp_cli(f"eval-file {remote}", timeout=timeout)
    finally:
        try:
            ssh.run(f"rm -f {remote}")
        except Exception:
            pass


def save_backup(part: str, payload) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"cleanup_backup_{part}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    log(f"  backup → {path.name} ({path.stat().st_size} bytes)")
    return path


# ---------------------------------------------------------------------------
# Part 1 — the literal string "None"
# ---------------------------------------------------------------------------

def part_none(ssh: SSHClient, apply: bool) -> None:
    log("── part: none ─────────────────────────────────────────────")
    php = r"""<?php
global $wpdb;
$rows = $wpdb->get_results("
  SELECT pm.meta_id, pm.post_id, pm.meta_key
  FROM {$wpdb->postmeta} pm
  JOIN {$wpdb->posts} p ON p.ID = pm.post_id
  WHERE p.post_type IN ('property','inwestycja') AND pm.meta_value = 'None'
  ORDER BY pm.meta_key, pm.post_id", ARRAY_A);
echo json_encode($rows, JSON_UNESCAPED_UNICODE);
"""
    rows = json.loads(run_php(ssh, php, "none_scan") or "[]")
    by_key: dict[str, int] = {}
    for r in rows:
        by_key[r["meta_key"]] = by_key.get(r["meta_key"], 0) + 1
    if not rows:
        log("  нічого не знайдено")
        return
    for k, n in sorted(by_key.items(), key=lambda x: -x[1]):
        log(f"  {k:<34} {n}")
    log(f"  разом: {len(rows)} рядків")

    if not apply:
        log("  DRY RUN — нічого не записано")
        return

    save_backup("none", rows)
    ids = [int(r["meta_id"]) for r in rows]
    total = 0
    for i in range(0, len(ids), 500):
        chunk = ",".join(str(x) for x in ids[i:i + 500])
        php_w = (
            "<?php\nglobal $wpdb;\n"
            f"$n=$wpdb->query(\"UPDATE {{$wpdb->postmeta}} SET meta_value='' WHERE meta_id IN ({chunk})\");\n"
            "echo (int)$n;\n"
        )
        total += int(run_php(ssh, php_w, f"none_w{i}") or 0)
    log(f"  ✓ очищено {total} рядків (значення -> '')")


# ---------------------------------------------------------------------------
# Part 2 — property_feature terms that "Nie" produced
# ---------------------------------------------------------------------------

def part_features(ssh: SSHClient, apply: bool) -> None:
    log("── part: features ─────────────────────────────────────────")
    php = r"""<?php
global $wpdb;
// investment expro_id -> every answer any post carrying that expro_id holds.
// Five expro_ids have two inwestycja posts (a stale draft beside the published
// one) and the two disagree: Legnicka Vita's published post has an empty
// expro_smart_home while its draft still holds 'Nie'. Keying by expro_id and
// letting the last post win read that as "no data" and left 79 units tagged.
// Collect all of them and let the caller resolve.
$out = ['inv' => [], 'units' => []];
foreach (get_posts(['post_type'=>'inwestycja','post_status'=>'any','posts_per_page'=>-1,'fields'=>'ids']) as $iid) {
    $eid = get_post_meta($iid, 'expro_id', true);
    if (!$eid) continue;
    foreach (['expro_winda','expro_smart_home','expro_stacja_ev',
              'expro_parking','expro_komorki','expro_pod_klucz'] as $k) {
        $out['inv'][$eid][$k][] = (string) get_post_meta($iid, $k, true);
    }
}
// every published property with its expro_id and its property_feature terms
$rows = $wpdb->get_results("
  SELECT p.ID, pm.meta_value AS eid, t.name AS feature, tt.term_taxonomy_id AS ttid
  FROM {$wpdb->posts} p
  JOIN {$wpdb->postmeta} pm ON pm.post_id = p.ID AND pm.meta_key = 'expro_id'
  JOIN {$wpdb->term_relationships} tr ON tr.object_id = p.ID
  JOIN {$wpdb->term_taxonomy} tt ON tt.term_taxonomy_id = tr.term_taxonomy_id AND tt.taxonomy = 'property_feature'
  JOIN {$wpdb->terms} t ON t.term_id = tt.term_id
  WHERE p.post_type = 'property' AND p.post_status = 'publish'", ARRAY_A);
$out['units'] = $rows;
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""
    data = json.loads(run_php(ssh, php, "feat_scan") or "{}")
    inv = data.get("inv", {})
    rows = data.get("units", [])
    log(f"  інвестицій з expro_id: {len(inv)}   звʼязків property↔feature: {len(rows)}")

    to_remove: dict[str, list[dict]] = {}
    no_evidence: dict[str, int] = {}
    kept_yes: dict[str, int] = {}
    for r in rows:
        feat = r["feature"]
        src = FEATURE_SOURCES.get(feat)
        if not src:
            continue                       # unit-level amenity or manual term — never touched
        vals = [v for v in (inv.get(r["eid"], {}) or {}).get(src, []) if v != ""]
        if not vals:
            no_evidence[feat] = no_evidence.get(feat, 0) + 1
            continue                       # no data on any post — leave it alone
        # A single yes anywhere wins: the feature exists and one of the posts
        # has simply lost the value. Only remove when every answer we have is
        # a no.
        if any(is_yes(v) for v in vals):
            kept_yes[feat] = kept_yes.get(feat, 0) + 1
            continue
        to_remove.setdefault(feat, []).append({"post_id": int(r["ID"]), "ttid": int(r["ttid"])})

    names = sorted(set(list(to_remove) + list(no_evidence) + list(kept_yes)))
    log(f"  {'фіча':<24}{'зняти':>8}{'лишити (Tak)':>14}{'без доказів':>14}")
    for n in names:
        log(f"  {n:<24}{len(to_remove.get(n,[])):>8}{kept_yes.get(n,0):>14}{no_evidence.get(n,0):>14}")
    total = sum(len(v) for v in to_remove.values())
    log(f"  разом до зняття: {total}")

    if not apply:
        log("  DRY RUN — нічого не записано")
        return
    if not total:
        return

    save_backup("features", to_remove)
    done = 0
    for feat, items in to_remove.items():
        for i in range(0, len(items), 400):
            chunk = items[i:i + 400]
            pairs = ",".join(f"[{c['post_id']},{c['ttid']}]" for c in chunk)
            php_w = (
                "<?php\n"
                f"$pairs = [{pairs}];\n"
                "$n=0;\n"
                "foreach ($pairs as $p) {\n"
                "    $tt = get_term_by('term_taxonomy_id', $p[1]);\n"
                "    if (!$tt || is_wp_error($tt)) continue;\n"
                "    $r = wp_remove_object_terms($p[0], [(int)$tt->term_id], 'property_feature');\n"
                "    if ($r === true) $n++;\n"
                "}\n"
                "echo $n;\n"
            )
            done += int(run_php(ssh, php_w, "feat_w") or 0)
            log(f"    {feat}: {done} знято…")
    log(f"  ✓ знято {done} звʼязків")


# ---------------------------------------------------------------------------
# Part 3 — featured image pointing at the wrong photo
# ---------------------------------------------------------------------------

def part_gallery(ssh: SSHClient, apply: bool) -> None:
    log("── part: gallery ──────────────────────────────────────────")
    try:
        import requests
        from config import EXPRO
    except Exception as e:
        log(f"  пропущено — немає requests/config: {e}")
        return

    tok = requests.post(f"{BASE_URL}/api/auth",
                        data={"login": EXPRO["username"], "password": EXPRO["password"]},
                        timeout=20).json()["token"]
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer: {tok}"})
    invs = []
    for page in range(1, 14):
        r = sess.get(f"{BASE_URL}/api/investment/", params={"page": page}, timeout=30).json()
        if not r.get("payload"):
            break
        invs += r["payload"]
        if len(invs) >= int(r["paginator"]["totalItems"] or 0):
            break
    main_by_eid = {
        i["id"]: (i.get("picture") or "").strip()
        for i in invs if (i.get("picture") or "").strip()
    }
    log(f"  ExPro віддав головне фото для {len(main_by_eid)} інвестицій")

    php = r"""<?php
global $wpdb;
$out = [];
foreach (get_posts(['post_type'=>'inwestycja','post_status'=>'publish','posts_per_page'=>-1,'fields'=>'ids']) as $iid) {
    $eid = get_post_meta($iid, 'expro_id', true);
    if (!$eid) continue;
    $gal = get_post_meta($iid, 'projekt_galeria', true);
    $ids = $gal ? array_values(array_filter(array_map('intval', explode(',', $gal)))) : [];
    // _source_url is empty on 1259 of 1283 gallery attachments (they were not
    // imported through media_sideload_image), so the uploaded file name is the
    // only reliable link back to ExPro — it keeps the original hash, e.g.
    // esm_gallery_53927_gal_53927_<hash>.jpeg
    $src = [];
    foreach ($ids as $a) {
        $src[$a] = [
            'url'  => (string) get_post_meta($a, '_source_url', true),
            'file' => basename((string) get_attached_file($a)),
        ];
    }
    $out[] = ['post_id'=>$iid, 'eid'=>$eid, 'gallery'=>$ids,
              'thumb'=>(int) get_post_thumbnail_id($iid), 'src'=>$src];
}
echo json_encode($out, JSON_UNESCAPED_UNICODE);
"""
    posts = json.loads(run_php(ssh, php, "gal_scan") or "[]")
    planned, missing = [], 0
    for p in posts:
        main = main_by_eid.get(p["eid"], "")
        if not main or not p["gallery"]:
            continue
        # Two ways to tie an attachment back to ExPro's file: _source_url when
        # it is set at all (24 of 1283), and otherwise the uploaded file name,
        # which keeps the original hash.
        stem = main.rsplit(".", 1)[0]
        att = 0
        for a, meta in p["src"].items():
            url, fname = meta.get("url", ""), meta.get("file", "")
            if (url and url.rsplit("/", 1)[-1] == main) or (fname and stem in fname):
                att = int(a)
                break
        if not att:
            missing += 1
            continue
        if p["gallery"][0] == att and p["thumb"] == att:
            continue
        new_gal = [att] + [a for a in p["gallery"] if a != att]
        planned.append({"post_id": p["post_id"], "eid": p["eid"],
                        "old_gallery": p["gallery"], "new_gallery": new_gal,
                        "old_thumb": p["thumb"], "new_thumb": att})

    log(f"  переставити: {len(planned)}   головне фото не імпортоване: {missing}")
    for p in planned[:8]:
        log(f"    post {p['post_id']} (expro {p['eid']}): thumb {p['old_thumb']} → {p['new_thumb']}")

    if not apply:
        log("  DRY RUN — нічого не записано")
        return
    if not planned:
        return

    save_backup("gallery", planned)
    done = 0
    for i in range(0, len(planned), 40):
        chunk = planned[i:i + 40]
        js = json.dumps(chunk, ensure_ascii=False)
        ssh.write_remote_file(js, "/tmp/esm_gal_chunk.json")
        php_w = r"""<?php
$items = json_decode(file_get_contents('/tmp/esm_gal_chunk.json'), true);
$n = 0;
foreach ($items as $it) {
    update_post_meta($it['post_id'], 'projekt_galeria', implode(',', $it['new_gallery']));
    set_post_thumbnail($it['post_id'], (int) $it['new_thumb']);
    $n++;
}
echo $n;
"""
        done += int(run_php(ssh, php_w, "gal_w") or 0)
    ssh.run("rm -f /tmp/esm_gal_chunk.json")
    log(f"  ✓ переставлено {done} галерей")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["none", "features", "gallery", "all"], default="all")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    args = ap.parse_args()

    if args.apply:
        log("РЕЖИМ ЗАПИСУ — бекап пишеться перед кожною частиною")
    else:
        log("DRY RUN — жодного запису; для застосування додай --apply")

    ssh = SSHClient()
    ssh._connect()
    try:
        if args.part in ("none", "all"):
            part_none(ssh, args.apply)
        if args.part in ("features", "all"):
            part_features(ssh, args.apply)
        if args.part in ("gallery", "all"):
            part_gallery(ssh, args.apply)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
