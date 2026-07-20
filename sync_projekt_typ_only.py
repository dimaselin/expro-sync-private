"""
Fast, narrow sync: writes ONLY projekt_typ for every live investment, using
resolve_projekt_typ() from wp_sync.py. Does not touch images, prices, or any
other field — one bulk SSH round-trip instead of one per investment, so it
runs in seconds instead of the hours a full wp_sync.py run takes when photo
imports are failing.

Use this to get a classifier fix live quickly; run the full wp_sync.py /
run.py pipeline separately (already migrated to GitHub-hosted CI) to pick
up everything else on its normal schedule.

Usage:
    python3 sync_projekt_typ_only.py --dry-run   # compute + diff only, no writes
    python3 sync_projekt_typ_only.py             # write for real
"""
import argparse
import json

from wp_sync import SSHClient, resolve_projekt_typ, log


def fetch_live_posts(ssh: SSHClient) -> list:
    php = """<?php
$posts = get_posts(['post_type'=>'inwestycja','posts_per_page'=>-1,'post_status'=>'publish','fields'=>'ids']);
$out = [];
foreach ($posts as $id) {
    $out[] = [
        'post_id'  => $id,
        'expro_id' => get_post_meta($id, 'expro_id', true),
        'current'  => get_post_meta($id, 'projekt_typ', true),
    ];
}
echo json_encode($out);
"""
    ssh.write_remote_file(php, "/tmp/esm_typ_fetch.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_typ_fetch.php")
    return json.loads(out)


def push_updates(ssh: SSHClient, updates: list):
    php = f"""<?php
$updates = json_decode('{json.dumps(updates)}', true);
$n = 0;
foreach ($updates as $u) {{
    update_post_meta((int)$u['post_id'], 'projekt_typ', $u['projekt_typ']);
    $n++;
}}
echo "Updated $n posts\\n";
"""
    ssh.write_remote_file(php, "/tmp/esm_typ_push.php")
    out = ssh.run_wp_cli("eval-file /tmp/esm_typ_push.php", timeout=60)
    log(out.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/expro_data.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    by_expro = {str(inv["expro_id"]): inv for inv in data}

    ssh = SSHClient()
    log("Fetching live posts (post_id, expro_id, current projekt_typ)...")
    live_posts = fetch_live_posts(ssh)
    log(f"  {len(live_posts)} live investments.")

    updates = []
    unchanged = 0
    no_data = 0
    for p in live_posts:
        eid = p["expro_id"]
        inv = by_expro.get(eid)
        if not inv:
            no_data += 1
            continue
        new_typ = resolve_projekt_typ(eid, inv)
        if not new_typ:
            continue  # empty result — never overwrite (matches wp_sync.py's `if value:` guard)
        if new_typ != p["current"]:
            updates.append({"post_id": p["post_id"], "expro_id": eid,
                             "old": p["current"], "projekt_typ": new_typ})
        else:
            unchanged += 1

    log(f"Unchanged: {unchanged}. No scrape data (not in local cache): {no_data}. To update: {len(updates)}.")
    for u in updates:
        log(f"  {u['expro_id']:6} post={u['post_id']}: {u['old']!r} -> {u['projekt_typ']!r}")

    if args.dry_run:
        log("DRY RUN — nothing written.")
        return

    if not updates:
        log("Nothing to write.")
        return

    push_data = [{"post_id": u["post_id"], "projekt_typ": u["projekt_typ"]} for u in updates]
    push_updates(ssh, push_data)


if __name__ == "__main__":
    main()
