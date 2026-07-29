#!/usr/bin/env python3
"""
Take units ExPro no longer lists off the site.

ExPro has no "sold" status: a unit that sells simply stops appearing in the
feed. Nothing ever acted on that, so property posts for units that vanished
stayed published — advertised as free, with prices and availability frozen at
the day they disappeared. Some had not moved since 2026-07-09.

normalize_units_table.py records the disappearance as gone_at. This turns that
record into an action, and deliberately keeps the two apart: marking is
automatic and reversible, unpublishing is not, so it asks first.

Reports by default; --apply moves the posts to draft. Nothing is deleted, and
a unit that returns to the feed has its gone_at cleared by the next normalize
run, after which --restore republishes it.

--apply is the write switch for both directions; without it either mode only
reports. --restore additionally republishes homes only, never the commercial
premises the classifier drafts on purpose.

Usage:
    python3 reconcile_gone_units.py                   # report what would be unpublished
    python3 reconcile_gone_units.py --apply           # publish -> draft
    python3 reconcile_gone_units.py --restore         # report what came back
    python3 reconcile_gone_units.py --restore --apply # draft -> publish
    python3 reconcile_gone_units.py --min-age-days 3
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


def run_php(ssh: SSHClient, php: str, tag: str, timeout: int = 300):
    remote = f"/tmp/esm_recon_{tag}.php"
    ssh.write_remote_file(php, remote)
    try:
        out = ssh.run_wp_cli(f"eval-file {remote}", timeout=timeout)
    finally:
        ssh.remove_remote_file(remote)
    idx = out.find("[")
    idx2 = out.find("{")
    start = min(x for x in (idx, idx2) if x >= 0) if (idx >= 0 or idx2 >= 0) else -1
    if start < 0:
        log(f"  WARN {tag}: unexpected output: {out[:200]}")
        return None
    try:
        return json.loads(out[start:])
    except Exception:
        log(f"  WARN {tag}: could not parse: {out[:200]}")
        return None


def scan(ssh: SSHClient, min_age_days: int, restore: bool):
    """Published posts whose unit is gone, or drafted posts whose unit returned."""
    if restore:
        cond = "u.gone_at IS NULL AND p.post_status = 'draft'"
        age = ""
        # Being in the feed is not enough to deserve publishing. The classifier
        # in mieszkania_sync.py drafts lokal-uzytkowy (no template of its own)
        # and niesklasyfikowane (no signal placed it) on purpose, and those stay
        # in the feed, so an unfiltered restore would put every one of them back
        # on the site. Same rule as esm_rodzaj() there: homes only.
        filter_php = """
$rows = array_values(array_filter($rows, function ($r) {
    foreach (wp_get_post_terms($r['ID'], 'property_type', ['fields' => 'slugs']) as $s) {
        if ($s === 'mieszkanie' || str_starts_with($s, 'dom')) return true;
    }
    return false;
}));"""
    else:
        cond = "u.gone_at IS NOT NULL AND p.post_status = 'publish'"
        # A unit missing from one run may be a blip in ExPro rather than a sale.
        age = f"AND u.gone_at < DATE_SUB(NOW(), INTERVAL {int(min_age_days)} DAY)"
        filter_php = ""
    php = f"""<?php
global $wpdb; $t = $wpdb->prefix . 'expro_units';
$rows = $wpdb->get_results("
    SELECT p.ID, p.post_title, p.post_status, u.gone_at, u.unit_name, u.price,
           u.expro_investment_id, inv.post_title AS investment
    FROM {{$t}} u
    JOIN {{$wpdb->posts}} p ON p.ID = u.property_post_id
    LEFT JOIN {{$wpdb->posts}} inv ON inv.ID = u.investment_post_id
    WHERE {cond} {age}
    ORDER BY u.expro_investment_id, u.unit_name", ARRAY_A);
{filter_php}
echo json_encode($rows, JSON_UNESCAPED_UNICODE);
"""
    return run_php(ssh, php, "scan") or []


def apply_status(ssh: SSHClient, ids: list, status: str) -> int:
    done = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        csv = ",".join(str(int(x)) for x in chunk)
        php = f"""<?php
$n = 0;
foreach ([{csv}] as $id) {{
    $r = wp_update_post(['ID' => $id, 'post_status' => '{status}'], true);
    if (!is_wp_error($r)) $n++;
}}
echo json_encode(['done' => $n]);
"""
        res = run_php(ssh, php, f"apply{i}") or {}
        done += int(res.get("done", 0))
        log(f"    {done}/{len(ids)}")
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the change (without it, report only)")
    ap.add_argument("--restore", action="store_true", help="look at units that returned to the feed instead")
    ap.add_argument("--min-age-days", type=int, default=1,
                    help="how long a unit must have been missing before acting (default 1)")
    args = ap.parse_args()

    ssh = SSHClient()
    ssh._connect()
    try:
        rows = scan(ssh, args.min_age_days, args.restore)
        verb = "republish" if args.restore else "unpublish"
        if not rows:
            log(f"Nothing to {verb}.")
            return

        by_inv: dict = {}
        for r in rows:
            key = f"{r['expro_investment_id']} {r.get('investment') or '?'}"
            by_inv.setdefault(key, []).append(r)

        log(f"── {len(rows)} post(s) to {verb}, across {len(by_inv)} investment(s) ──")
        for key, items in sorted(by_inv.items(), key=lambda x: -len(x[1]))[:20]:
            gone = items[0].get("gone_at") or ""
            log(f"  {key[:46]:<48} {len(items):>4}   {('зникли ' + gone[:10]) if gone else ''}")
        if len(by_inv) > 20:
            log(f"  … and {len(by_inv) - 20} more investments")

        if not args.apply:
            log(f"REPORT ONLY — nothing written. Add --apply to {verb}.")
            return

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"reconcile_backup_{stamp}.json"
        backup.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
        log(f"  backup → {backup.name}")

        target = "publish" if args.restore else "draft"
        log(f"  setting post_status={target} …")
        log(f"  ✓ {apply_status(ssh, [r['ID'] for r in rows], target)} post(s) updated")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
