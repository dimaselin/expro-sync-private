"""
Batch-reprocess floor plan images ALREADY on the WP site: download each
plan attachment, check it for developer/investment name and sales-office
contact info baked into the pixels (see plan_redactor.py), and re-upload
the blurred version in place (same attachment ID/URL — nothing else on the
site needs to change).

Resumable: progress is tracked in data/redact_plans_progress.json so a
killed/crashed run can continue with --resume instead of starting over.

Usage:
    python3 redact_existing_plans.py --all --dry-run   # scan + report only, no writes
    python3 redact_existing_plans.py --all              # process everything
    python3 redact_existing_plans.py --all --resume      # continue after a crash
    python3 redact_existing_plans.py --post-ids 41225,41226  # specific property posts
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

try:
    from plan_redactor import redact_plan_image
except ImportError:
    print("ERROR: pip install pytesseract Pillow (and apt-get install tesseract-ocr tesseract-ocr-pol)")
    sys.exit(1)

PROGRESS_FILE = Path(__file__).parent / 'data' / 'redact_plans_progress.json'


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_progress(p: dict):
    PROGRESS_FILE.parent.mkdir(exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False))


def fetch_plan_targets(ssh: SSHClient, post_ids: list = None) -> list:
    """Returns [{post_id, plan_attachment_id, developer, investment_name}] for
    property posts that have a floor plan, joined to their parent investment's
    developer/name for redaction context."""
    filter_clause = ""
    if post_ids:
        ids_csv = ",".join(str(i) for i in post_ids)
        filter_clause = f"$ids_filter = [{ids_csv}];"
    php = f"""<?php
{filter_clause}
global $wpdb;
$sql = "
    SELECT p.ID as post_id, pm.meta_value as plan_att_id,
           pd.meta_value as developer, inv.post_title as inv_name
    FROM {{$wpdb->posts}} p
    JOIN {{$wpdb->postmeta}} pm ON pm.post_id = p.ID AND pm.meta_key = 'lokal_plan_attachment_id' AND pm.meta_value != ''
    LEFT JOIN {{$wpdb->postmeta}} pp ON pp.post_id = p.ID AND pp.meta_key = 'fave_property_project_id'
    LEFT JOIN {{$wpdb->posts}} inv ON inv.ID = pp.meta_value
    LEFT JOIN {{$wpdb->postmeta}} pd ON pd.post_id = inv.ID AND pd.meta_key = 'projekt_developer'
    WHERE p.post_type = 'property' AND p.post_status = 'publish'
";
if (isset($ids_filter)) {{
    $sql .= " AND p.ID IN (" . implode(',', array_map('intval', $ids_filter)) . ")";
}}
$rows = $wpdb->get_results($sql, ARRAY_A);
echo json_encode($rows);
"""
    ssh.write_remote_file(php, "/tmp/esm_redact_targets.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_redact_targets.php", timeout=60)
    return json.loads(out)


def process_one(ssh: SSHClient, sftp, target: dict, dry_run: bool) -> str:
    """Returns 'redacted' | 'clean' | 'error'."""
    att_id = int(target['plan_att_id'])
    php = f"""<?php echo wp_get_attachment_url({att_id}) ?: ''; echo "\\n"; echo get_attached_file({att_id}) ?: '';"""
    ssh.write_remote_file(php, "/tmp/esm_redact_geturl.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_redact_geturl.php", timeout=30)
    lines = out.strip().splitlines()
    if len(lines) < 2 or not lines[1]:
        return 'error'
    remote_file_path = lines[1].strip()

    with tempfile.NamedTemporaryFile(suffix=Path(remote_file_path).suffix, delete=False) as tmp:
        local_path = tmp.name
    try:
        sftp.get(remote_file_path, local_path)
        extra_terms = [target.get('developer') or '', target.get('inv_name') or '']
        hits = redact_plan_image(local_path, extra_terms=extra_terms)
        if not hits:
            return 'clean'
        log(f"    post {target['post_id']}: blurred {len(hits)} region(s): "
            f"{[h[0] for h in hits]}")
        if not dry_run:
            sftp.put(local_path, remote_file_path)
        return 'redacted'
    finally:
        try:
            Path(local_path).unlink()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--post-ids", help="Comma-separated property post IDs")
    ap.add_argument("--dry-run", action="store_true", help="Detect only, never write back")
    ap.add_argument("--resume", action="store_true", help="Skip post IDs already processed per progress file")
    ap.add_argument("--reset-progress", action="store_true")
    args = ap.parse_args()

    if args.reset_progress and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    post_ids = [int(x) for x in args.post_ids.split(",")] if args.post_ids else None
    if not args.all and not post_ids:
        print("Specify --all or --post-ids")
        sys.exit(1)

    ssh = SSHClient()
    ssh._connect()
    sftp = ssh._client.open_sftp()

    log("Fetching plan targets...")
    targets = fetch_plan_targets(ssh, post_ids)
    log(f"  {len(targets)} property posts with a floor plan.")

    progress = _load_progress() if args.resume else {}
    stats = {'redacted': 0, 'clean': 0, 'error': 0, 'skipped': 0}

    for i, t in enumerate(targets, 1):
        pid = str(t['post_id'])
        if args.resume and progress.get(pid):
            stats['skipped'] += 1
            continue
        log(f"[{i}/{len(targets)}] post {pid} ({t.get('inv_name') or '?'})")
        try:
            result = process_one(ssh, sftp, t, args.dry_run)
        except Exception as e:
            log(f"  ERROR: {e}")
            result = 'error'
        stats[result] += 1
        progress[pid] = result
        if i % 20 == 0:
            _save_progress(progress)

    _save_progress(progress)
    sftp.close()
    ssh.close()
    log(f"\nDone. {stats}")


if __name__ == '__main__':
    main()
