"""
ExPro sync runner
Usage:
  python run.py [all|scrape|sync|mieszkania]   -- run once and exit
  python run.py --daemon                        -- poll WP every 5 min, run on demand
"""
import sys
import os
import subprocess
import datetime
import time
import json
import urllib.request
import urllib.error

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts   = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.join(script_dir, 'logs'), exist_ok=True)
    with open(os.path.join(script_dir, 'logs', 'sync.log'), 'a', encoding='utf-8') as f:
        f.write(line + '\n')


# ── WP Plugin reporter ────────────────────────────────────────────────────────

def _wp_cfg() -> dict:
    try:
        from config import WP_PLUGIN
        return WP_PLUGIN
    except (ImportError, AttributeError):
        return {}


def _wp_post(path: str, payload: dict) -> None:
    cfg = _wp_cfg()
    url = cfg.get('url', '').rstrip('/')
    key = cfg.get('api_key', '')
    if not url or not key:
        return
    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            f"{url}/wp-json/expro-sync/v1/{path}",
            data=data,
            headers={'Content-Type': 'application/json', 'X-ExPro-Key': key},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def wp_start(run_type: str) -> None:
    _wp_post('report', {'action': 'start', 'type': run_type})


def wp_log(lines: list) -> None:
    if lines:
        _wp_post('report', {'action': 'log', 'lines': lines})


def wp_finish(success: bool, stats: dict, error: str = '') -> None:
    _wp_post('report', {'action': 'finish', 'success': success, 'stats': stats, 'error': error})


def wp_check_pending():
    cfg = _wp_cfg()
    url = cfg.get('url', '').rstrip('/')
    key = cfg.get('api_key', '')
    if not url or not key:
        return None
    try:
        req = urllib.request.Request(
            f"{url}/wp-json/expro-sync/v1/pending",
            headers={'X-ExPro-Key': key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get('run_type')
    except Exception:
        return None


# ── Subprocess runner with live log capture ───────────────────────────────────

def _run_script(name: str, extra_args=None) -> bool:
    """Run a Python script, stream its output to log() and WP, return success."""
    cmd  = [sys.executable, os.path.join(script_dir, name)] + (extra_args or [])
    proc = subprocess.Popen(
        cmd, cwd=script_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    pending    = []
    last_flush = time.time()

    for raw in proc.stdout:
        line = raw.rstrip('\n')
        log(line)
        pending.append(line)
        if len(pending) >= 10 or (time.time() - last_flush) >= 15:
            wp_log(pending)
            pending    = []
            last_flush = time.time()

    proc.wait()
    if pending:
        wp_log(pending)

    return proc.returncode == 0


# ── Run mode ──────────────────────────────────────────────────────────────────

def run_mode(mode: str) -> bool:
    """Execute sync in given mode. Reports to WP plugin throughout. Returns success."""
    os.chdir(script_dir)
    wp_start(mode)

    # Test mode: single investment (env var set by workflow input or manually)
    test_inv = os.environ.get("EXPRO_TEST_INV_ID", "").strip()
    if test_inv:
        log(f"TEST MODE: limiting to investment(s): {test_inv}")
    mieszkania_args = ['--expro-ids', test_inv] if test_inv else ['--all']
    # import_media and pipeline work on WP post IDs — --all is safe (few posts in test)
    media_args    = ['--all']
    pipeline_args = ['--all']

    if mode in ('all', 'scrape'):
        log('=== Starting ExPro scrape (API) ===')
        if not _run_script('api_scraper.py'):
            log('=== Scrape FAILED ===')
            wp_finish(False, {}, error='api_scraper.py exited with error')
            return False
        log('=== Scrape complete ===')

    if mode in ('all', 'sync'):
        log('=== Starting WP sync (inwestycje) ===')
        if not _run_script('wp_sync.py'):
            log('=== WP sync FAILED ===')
            wp_finish(False, {}, error='wp_sync.py exited with error')
            return False
        log('=== WP sync complete ===')

    if mode in ('all', 'mieszkania'):
        log('=== Starting mieszkania sync ===')
        if not _run_script('mieszkania_sync.py', mieszkania_args):
            log('=== Mieszkania sync FAILED ===')
            wp_finish(False, {}, error='mieszkania_sync.py exited with error')
            return False
        log('=== Mieszkania sync complete ===')

    if mode in ('all', 'media'):
        log('=== Starting media import ===')
        if not _run_script('import_media.py', media_args):
            log('=== Media import FAILED ===')
            wp_finish(False, {}, error='import_media.py exited with error')
            return False
        log('=== Media import complete ===')

    if mode in ('all', 'pipeline'):
        log('=== Starting enrichment pipeline ===')
        if not _run_script('pipeline.py', pipeline_args):
            log('=== Pipeline FAILED ===')
            wp_finish(False, {}, error='pipeline.py exited with error')
            return False
        log('=== Pipeline complete ===')

    if mode == 'amenity':
        log('=== Starting amenity patch (Phase 2) ===')
        amenity_args = ['--expro-ids', test_inv] if test_inv else ['--all']
        if not _run_script('amenity_patch.py', amenity_args):
            log('=== Amenity patch FAILED ===')
            wp_finish(False, {}, error='amenity_patch.py exited with error')
            return False
        log('=== Amenity patch complete ===')

    if mode == 'redact':
        # Not part of 'all' — a deliberate, manually-triggered one-off batch
        # cleanup of already-imported plan images, separate from the
        # per-sync redaction new plans already get automatically.
        # EXPRO_TEST_INV_ID is reused here to mean property POST IDs (not
        # ExPro investment IDs) for a small-sample test run.
        log('=== Starting floor plan redaction (existing images) ===')
        # --resume is always safe here: the workflow now caches
        # redact_plans_progress.json across dispatches (a single ~3800-image
        # pass takes longer than a GitHub-hosted runner's 6h ceiling, and a
        # dropped SSH connection can also end a run early), and 'error'
        # entries are retried rather than skipped (see main()'s resume check).
        redact_args = ['--post-ids', test_inv] if test_inv else ['--all', '--resume']
        if os.environ.get('EXPRO_REDACT_DRY_RUN', '').strip().lower() == 'true':
            redact_args.append('--dry-run')
            log('DRY RUN: scanning only, no server writes')
        if not _run_script('redact_existing_plans.py', redact_args):
            log('=== Plan redaction FAILED ===')
            wp_finish(False, {}, error='redact_existing_plans.py exited with error')
            return False
        log('=== Plan redaction complete ===')

    wp_finish(True, {})
    return True


# ── Daemon ────────────────────────────────────────────────────────────────────

DAEMON_POLL = 300  # seconds between WP polls


def daemon_loop() -> None:
    cfg = _wp_cfg()
    log(f"ExPro Sync Daemon started — polling every {DAEMON_POLL}s")
    log(f"WP: {cfg.get('url', '(not configured — set WP_PLUGIN in config.py)')}")

    while True:
        try:
            pending = wp_check_pending()
        except Exception as e:
            log(f"Daemon: poll error: {e}")
            pending = None

        if pending:
            log(f"=== Daemon: run requested: {pending!r} ===")
            ok = run_mode(pending)
            log(f"=== Daemon: run {'OK' if ok else 'FAILED'} ===")
        else:
            log(f"Daemon: idle — next check in {DAEMON_POLL}s")

        time.sleep(DAEMON_POLL)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if '--daemon' in sys.argv:
        daemon_loop()
        return

    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if mode not in ('all', 'scrape', 'sync', 'mieszkania', 'media', 'pipeline', 'amenity', 'redact'):
        print('Usage: python run.py [all|scrape|sync|mieszkania|media|pipeline|amenity|redact|--daemon]')
        sys.exit(1)

    ok = run_mode(mode)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
