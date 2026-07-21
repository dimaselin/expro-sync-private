#!/usr/bin/env python3
"""
Import ExPro media to WordPress:
  1. Gallery images (from expro.expander.pl) → projekt_galeria + _thumbnail_id
  2. Rzut PDFs (floor plans) from ExPro documents → JPEG → projekt_plan_parter / projekt_plan_pietro

Usage:
  python3 import_media.py --all                   # all inwestycja posts
  python3 import_media.py --post-id 31734          # single post
  python3 import_media.py --all --skip-existing    # skip posts that already have gallery
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, update_meta, import_image_to_wp, log, get_all_posts_expro

DATA          = Path(__file__).parent / 'data' / 'expro_data.json'
PDFS          = Path(__file__).parent / 'data' / 'pdfs'
MEDIA_PROGRESS = Path(__file__).parent / 'data' / 'media_progress.json'
PDFS.mkdir(exist_ok=True)


def _load_media_progress() -> dict:
    if MEDIA_PROGRESS.exists():
        try:
            return json.loads(MEDIA_PROGRESS.read_text())
        except Exception:
            pass
    return {}

def _save_media_progress(p: dict):
    MEDIA_PROGRESS.write_text(json.dumps(p, indent=2))


# ── PDF → JPEG conversion ─────────────────────────────────────────────────────

def pdf_page_to_jpeg(pdf_path: str, page_idx: int = 0) -> str:
    """Convert a PDF page to JPEG. Returns path to JPEG or ''."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if page_idx >= len(doc):
            page_idx = 0
        pg  = doc[page_idx]
        mat = fitz.Matrix(2.0, 2.0)
        pix = pg.get_pixmap(matrix=mat)
        out = pdf_path.replace('.pdf', f'_p{page_idx + 1}.jpg')
        pix.save(out)
        doc.close()
        return out
    except Exception as e:
        log(f'  PDF→JPEG failed: {e}')
        return ''


# ── Authenticated ExPro PDF download ──────────────────────────────────────────

_pw_page = None

def _get_expro_page():
    global _pw_page
    if _pw_page is None:
        import os
        from playwright.sync_api import sync_playwright
        from config import EXPRO
        _pw = sync_playwright().__enter__()
        browser = _pw.chromium.launch(headless=True)
        page = browser.new_context(accept_downloads=True).new_page()
        page.goto(f"{EXPRO['base_url']}/user/login", wait_until='domcontentloaded', timeout=20_000)
        page.fill('input[name="username"]', EXPRO['username'])
        page.fill('input[name="password"]', EXPRO['password'])
        page.click('button[type="submit"]')
        page.wait_for_load_state('domcontentloaded', timeout=15_000)
        _pw_page = page
    return _pw_page


def download_rzut_pdf(doc_url: str, dest: Path) -> bool:
    if dest.exists():
        log(f'  cached: {dest.name}')
        return True
    page = _get_expro_page()
    try:
        with page.expect_download(timeout=30000) as dl:
            try:
                page.goto(doc_url, wait_until='commit', timeout=20000)
            except Exception:
                pass
        dl.value.save_as(str(dest))
        log(f'  downloaded: {dest.name} ({dest.stat().st_size // 1024}KB)')
        return True
    except Exception as e:
        log(f'  rzut download failed: {e}')
        return False


# ── Per-investment media import ───────────────────────────────────────────────

def import_media_for(ssh: SSHClient, post_id: int, inv: dict, skip_existing: bool = True) -> dict:
    """Import gallery + rzut PDFs for one investment. Returns stats dict."""
    name  = inv.get('name', str(post_id))
    stats = {'gallery': 0, 'rzuty': 0, 'skipped': []}
    log(f'\n[{post_id}] {name}')

    # ── 1. Gallery images ──────────────────────────────────────────────────────
    try:
        existing_gallery = ssh.run_wp_cli(f'post meta get {post_id} projekt_galeria').strip()
    except Exception:
        existing_gallery = ''

    # Same ExPro-own-logo-as-placeholder issue as unit plan_urls (see
    # mieszkania_sync.py) can show up in the investment-level gallery too —
    # filter it out here so it never becomes projekt_galeria/_thumbnail_id,
    # which individual units fall back to when they have no gallery of
    # their own (mieszkania_sync.py's "reuse parent thumbnail" path).
    images = [u for u in inv.get('images', []) if 'expander-logo' not in u]
    if images and (not existing_gallery or not skip_existing):
        log(f'  Importing {len(images)} gallery images…')
        img_ids = []
        for i, url in enumerate(images):
            aid = import_image_to_wp(ssh, url, post_id)
            if aid:
                img_ids.append(str(aid))
                log(f'  [{i + 1}/{len(images)}] → ID {aid}')
        if img_ids:
            try:
                existing_thumb = ssh.run_wp_cli(f'post meta get {post_id} _thumbnail_id').strip()
            except Exception:
                existing_thumb = ''
            if not existing_thumb or not existing_thumb.isdigit():
                update_meta(ssh, post_id, '_thumbnail_id', img_ids[0])
            update_meta(ssh, post_id, 'projekt_galeria', ','.join(img_ids))
            stats['gallery'] = len(img_ids)
            log(f'  Gallery: {len(img_ids)} images, featured={img_ids[0]}')
    elif existing_gallery and skip_existing:
        log(f'  Gallery already set — skipping')
        stats['skipped'].append('gallery')
    else:
        log(f'  No images in ExPro')

    # ── 2. Rzut PDFs → JPEG ───────────────────────────────────────────────────
    try:
        existing_parter = ssh.run_wp_cli(f'post meta get {post_id} projekt_plan_parter').strip()
    except Exception:
        existing_parter = ''

    if existing_parter and skip_existing:
        log(f'  Rzuty already set — skipping')
        stats['skipped'].append('rzuty')
        return stats

    documents = inv.get('documents', [])
    rzut_docs = [
        d for d in documents
        if re.search(r'rzut|parter|pi.tro|pietro|plan|kondygnacj', d.get('name', ''), re.I)
        and not re.search(r'standard|wykończ', d.get('name', ''), re.I)
    ]

    if not rzut_docs:
        log(f'  No rzut docs in ExPro')
        return stats

    log(f'  Processing {len(rzut_docs)} rzut docs…')
    plan_parter_id = ''
    plan_pietro_id = ''

    for doc in rzut_docs[:4]:
        safe    = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc['name'])[:60]
        pdf_dest = PDFS / f'{post_id}_rzut_{safe}.pdf'

        if not download_rzut_pdf(doc['url'], pdf_dest):
            continue

        jpg_path = pdf_page_to_jpeg(str(pdf_dest))
        if not jpg_path:
            continue

        attach_id = import_image_to_wp(ssh, jpg_path, post_id)
        if not attach_id:
            continue

        log(f'  Rzut imported: [{doc["name"]}] → ID {attach_id}')
        stats['rzuty'] += 1

        is_pietro = bool(re.search(r'pi.tro|pietro|poddasze|kondygnacj', doc['name'], re.I))
        if is_pietro and not plan_pietro_id:
            plan_pietro_id = str(attach_id)
        elif not plan_parter_id:
            plan_parter_id = str(attach_id)
        elif not plan_pietro_id:
            plan_pietro_id = str(attach_id)

    if plan_parter_id:
        update_meta(ssh, post_id, 'projekt_plan_parter', plan_parter_id)
        log(f'  projekt_plan_parter = {plan_parter_id}')
    if plan_pietro_id:
        update_meta(ssh, post_id, 'projekt_plan_pietro', plan_pietro_id)
        log(f'  projekt_plan_pietro = {plan_pietro_id}')

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Import ExPro media to WordPress')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all',            action='store_true', help='Process all inwestycja posts')
    group.add_argument('--post-id',        type=int,            help='Single WP post ID')
    parser.add_argument('--force',         action='store_true', help='Re-import even if already set')
    parser.add_argument('--resume',        action='store_true', help='Skip posts already marked ok in progress file')
    parser.add_argument('--retry',         action='store_true', help='Resume + retry error posts')
    parser.add_argument('--reset-progress',action='store_true', help='Clear progress file and start fresh')
    args = parser.parse_args()

    if args.reset_progress:
        MEDIA_PROGRESS.unlink(missing_ok=True)
        log('Progress file cleared.')

    progress = _load_media_progress()

    with open(DATA, encoding='utf-8') as f:
        expro_index = {str(d['expro_id']): d for d in json.load(f)}

    ssh = SSHClient()
    ssh._connect()

    if args.all:
        pairs = get_all_posts_expro(ssh)
        log(f'Found {len(pairs)} posts in WP')
        test_inv = os.environ.get("EXPRO_TEST_INV_ID", "").strip()
        if test_inv:
            test_ids = set(test_inv.split(","))
            pairs = [(pid, eid) for pid, eid in pairs if eid in test_ids]
            log(f'TEST MODE: filtered to {len(pairs)} investment(s): {test_inv}')
    else:
        raw = ssh.run_wp_cli(f'post meta get {args.post_id} expro_id').strip()
        if not raw:
            log(f'ERROR: post {args.post_id} has no expro_id meta')
            sys.exit(1)
        pairs = [(args.post_id, raw)]

    skip = not args.force
    total_gallery = total_rzuty = 0

    for post_id, expro_id in pairs:
        key = str(post_id)
        status = progress.get(key)
        if args.resume and status == 'ok':
            log(f'[{post_id}] SKIP (already ok)')
            continue
        if (args.resume or args.retry) and status == 'error' and not args.retry:
            log(f'[{post_id}] SKIP (error — use --retry to reprocess)')
            continue

        inv = expro_index.get(expro_id)
        if not inv:
            log(f'  SKIP post {post_id} — expro_id {expro_id} not in expro_data.json')
            progress[key] = 'skip'
            _save_media_progress(progress)
            continue
        try:
            s = import_media_for(ssh, post_id, inv, skip_existing=skip)
            total_gallery += s['gallery']
            total_rzuty   += s['rzuty']
            progress[key] = 'ok'
        except Exception as e:
            log(f'  ERROR post {post_id}: {e}')
            progress[key] = 'error'
        _save_media_progress(progress)

    ssh.run_wp_cli('litespeed-purge all')
    ssh.close()
    log(f'\nDone: gallery={total_gallery} images, rzuty={total_rzuty} plans')


if __name__ == '__main__':
    main()
