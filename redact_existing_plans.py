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
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, WP_PATH, log

try:
    from plan_redactor import redact_plan_image
except ImportError:
    print("ERROR: pip install pytesseract Pillow (and apt-get install tesseract-ocr tesseract-ocr-pol)")
    sys.exit(1)

# Outside public_html so backups are never web-served. A false positive in
# the OCR blocklist (e.g. a generic word from an investment name matching a
# legitimate floor-plan label) overwrites the live file in place with no
# other copy anywhere — this happened for real during testing (see git log)
# and required manually re-downloading the original from ExPro to recover.
# Every file this script touches gets backed up here first so a bad batch
# run can be rolled back with a plain `cp` instead of a source re-fetch.
# Derived from WP_PATH (.../domains/<site>/public_html) rather than "~" —
# shlex.quote() wraps "~" in single quotes, which disables shell tilde
# expansion, so a literal home-dir path is needed instead.
_HOME_DIR = WP_PATH.split('/domains/')[0]
BACKUP_DIR = f'{_HOME_DIR}/esm_plan_backups'

# Separate files (and separate cache keys in the workflow) for dry-run vs.
# real writes — a dry-run marks every scanned item 'redacted'/'clean' in its
# progress file same as a real run does, so sharing one file/cache prefix
# would make a real run silently restore the dry-run's "everything already
# done" progress and skip writing anything at all.
def progress_file(dry_run: bool) -> Path:
    name = 'redact_plans_progress_dryrun.json' if dry_run else 'redact_plans_progress.json'
    return Path(__file__).parent / 'data' / name


def _load_progress(dry_run: bool) -> dict:
    f = progress_file(dry_run)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _save_progress(p: dict, dry_run: bool):
    f = progress_file(dry_run)
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(p, indent=2, ensure_ascii=False))


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


def process_one(ssh: SSHClient, target: dict, dry_run: bool) -> str:
    """Returns 'redacted' | 'clean' | 'error'.

    Opens its own short-lived SFTP session rather than reusing one held for
    the whole batch — a multi-hour run over a shared-hosting SSH connection
    WILL drop at some point (confirmed live: it dropped after ~1h, killing
    ~3100 of 3812 items with "Socket is closed" since the single sftp handle
    captured before the loop started pointed at a transport that was gone,
    even though wp_sync's own _ensure_connected() reconnected `ssh._client`
    for the wp-cli calls). A fresh sftp handle per item is always bound to
    whatever connection is currently live.
    """
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
        ssh._ensure_connected()
        sftp = ssh._client.open_sftp()
        try:
            sftp.get(remote_file_path, local_path)
        finally:
            sftp.close()
        extra_terms = [target.get('developer') or '', target.get('inv_name') or '']
        hits = redact_plan_image(local_path, extra_terms=extra_terms)
        if not hits:
            return 'clean'
        log(f"    post {target['post_id']}: blurred {len(hits)} region(s): "
            f"{[h[0] for h in hits]}")
        if not dry_run:
            # Back up the still-pristine remote file before overwriting it —
            # a bad OCR match overwrites the only copy of the live image
            # with no other trace of the original anywhere.
            backup_path = f"{BACKUP_DIR}/{att_id}{Path(remote_file_path).suffix}"
            ssh.run(f"mkdir -p {shlex.quote(BACKUP_DIR)} && "
                    f"cp {shlex.quote(remote_file_path)} {shlex.quote(backup_path)}",
                    timeout=30)
            ssh._ensure_connected()
            sftp = ssh._client.open_sftp()
            try:
                sftp.put(local_path, remote_file_path)
            finally:
                sftp.close()
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

    if args.reset_progress and progress_file(args.dry_run).exists():
        progress_file(args.dry_run).unlink()

    post_ids = [int(x) for x in args.post_ids.split(",")] if args.post_ids else None
    if not args.all and not post_ids:
        print("Specify --all or --post-ids")
        sys.exit(1)

    ssh = SSHClient()
    # A one-off socket timeout on the very first connect attempt (observed
    # live: run 29927231184 failed here, before touching a single image)
    # otherwise kills the whole run instantly with zero progress made —
    # retry a couple of times before giving up.
    for attempt in range(3):
        try:
            ssh._connect()
            break
        except Exception as e:
            if attempt == 2:
                raise
            log(f"Initial SSH connect failed ({e}), retrying...")
            time.sleep(5)

    log("Fetching plan targets...")
    targets = fetch_plan_targets(ssh, post_ids)
    log(f"  {len(targets)} property posts with a floor plan.")

    progress = _load_progress(args.dry_run) if args.resume else {}
    stats = {'redacted': 0, 'clean': 0, 'error': 0, 'skipped': 0}

    for i, t in enumerate(targets, 1):
        pid = str(t['post_id'])
        # Only skip items that actually finished — an 'error' entry from a
        # prior interrupted run (e.g. the connection drop this fix addresses)
        # must be retried, not treated as done forever.
        if args.resume and progress.get(pid) in ('redacted', 'clean'):
            stats['skipped'] += 1
            continue
        log(f"[{i}/{len(targets)}] post {pid} ({t.get('inv_name') or '?'})")
        try:
            result = process_one(ssh, t, args.dry_run)
        except Exception as e:
            # One retry after forcing a reconnect — covers a connection that
            # dies mid-transfer on this specific item, not just between items.
            log(f"  WARN: {e} — retrying once after reconnect")
            try:
                ssh._client = None
                result = process_one(ssh, t, args.dry_run)
            except Exception as e2:
                log(f"  ERROR: {e2}")
                result = 'error'
        stats[result] += 1
        progress[pid] = result
        if i % 20 == 0:
            _save_progress(progress, args.dry_run)

    _save_progress(progress, args.dry_run)
    ssh.close()
    log(f"\nDone. {stats}")


if __name__ == '__main__':
    main()
