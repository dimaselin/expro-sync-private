#!/usr/bin/env python3
"""
Realsy — Investment Enrichment Pipeline

Steps per investment:
  1. Contacts      — save ExPro phone/email to projekt_kontakty_json
  2. Website       — scrape developer website text + find PDF links
  3. PDF           — download + extract text from developer PDFs
  4. ExPro PDF     — download standard wykończenia from ExPro (authenticated)
  5. Claude        — parse combined text with Claude AI
  6. Save          — write results to WP meta

Usage:
  python3 pipeline.py --all                          # all posts without projekt_standard
  python3 pipeline.py --post-id 31734                # single post (looks up expro_id from WP)
  python3 pipeline.py --all --steps contacts,save    # only contacts step
  python3 pipeline.py --force                        # re-enrich even if already has data
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log, get_all_posts_expro

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from config import SSH, WP, EXPRO, ANTHROPIC_API_KEY as _CFG_KEY
    _SSH_HOST = SSH['host']
    _SSH_PORT = SSH['port']
    _SSH_USER = SSH['username']
    _SSH_PASS = SSH['password']
    _WP_PATH  = WP['path']
    _EXPRO_USER = EXPRO['username']
    _EXPRO_PASS = EXPRO['password']
    _DEFAULT_API_KEY = _CFG_KEY or os.environ.get('ANTHROPIC_API_KEY', '')
except (ImportError, KeyError):
    _SSH_HOST = '82.198.229.58'
    _SSH_PORT = 65002
    _SSH_USER = 'u525644354'
    _SSH_PASS = 'Strona2026!'
    _WP_PATH  = '/home/u525644354/domains/realsymanagement.pl/public_html'
    _EXPRO_USER = 'biuro@realsymanagement.pl'
    _EXPRO_PASS = 'Firmastart2026'
    _DEFAULT_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── Optional dependencies ──────────────────────────────────────────────────────
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    print('⚠  anthropic not installed. Run: pip3 install anthropic')

try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    try:
        import fitz as _fitz  # noqa: F401
        HAS_PDF = True
    except ImportError:
        HAS_PDF = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

EXPRO_DATA_FILE = Path(__file__).parent / 'data' / 'expro_data.json'
DOWNLOAD_DIR    = Path(__file__).parent / 'data' / 'pdfs'
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.8',
}

# ── WP SSH client ─────────────────────────────────────────────────────────────

class WPClient:
    def __init__(self):
        self._ssh  = None
        self._sftp = None

    def connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(_SSH_HOST, port=_SSH_PORT, username=_SSH_USER, password=_SSH_PASS, timeout=30)
        self._ssh  = c
        self._sftp = c.open_sftp()

    def close(self):
        if self._sftp: self._sftp.close()
        if self._ssh:  self._ssh.close()

    def wp(self, cmd: str) -> str:
        _, out, _ = self._ssh.exec_command(f'cd {_WP_PATH} && wp {cmd} 2>&1')
        return out.read().decode().strip()

    def get_meta(self, post_id: int, key: str) -> str:
        return self.wp(f'post meta get {post_id} {key}')

    def update_meta_plain(self, post_id: int, key: str, text: str):
        tmp = f'/tmp/rm_meta_{post_id}_{key}.txt'
        with self._sftp.open(tmp, 'w') as f:
            f.write(text)
        self.wp(f"eval \"update_post_meta({post_id}, '{key}', file_get_contents('{tmp}'));\"")
        self._ssh.exec_command(f'rm -f {tmp}')

    def update_meta_json(self, post_id: int, key: str, obj):
        val = json.dumps(obj, ensure_ascii=False)
        tmp = f'/tmp/rm_meta_{post_id}_{key}.json'
        with self._sftp.open(tmp, 'w') as f:
            f.write(val)
        self.wp(f"eval \"update_post_meta({post_id}, '{key}', file_get_contents('{tmp}'));\"")
        self._ssh.exec_command(f'rm -f {tmp}')

    def needs_enrichment(self, post_id: int, force: bool = False) -> bool:
        if force:
            return True
        standard = self.get_meta(post_id, 'projekt_standard').strip()
        return not standard


# ── ExPro data loader ──────────────────────────────────────────────────────────

def load_expro(expro_id: str) -> dict:
    if not EXPRO_DATA_FILE.exists():
        return {}
    with open(EXPRO_DATA_FILE, encoding='utf-8') as f:
        for item in json.load(f):
            if str(item.get('expro_id', '')) == str(expro_id):
                return item
    return {}


# ── Website scraping ───────────────────────────────────────────────────────────

def _fetch_simple(url: str) -> str:
    try:
        req  = urllib.request.Request(url, headers=HTTP_HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        log(f'  simple fetch failed: {e}')
        return ''

def _fetch_playwright(url: str) -> str:
    if not HAS_PLAYWRIGHT:
        return ''
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()
            page.set_extra_http_headers(HTTP_HEADERS)
            page.goto(url, wait_until='networkidle', timeout=25000)
            time.sleep(1)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log(f'  playwright fetch failed: {e}')
        return ''

def scrape_website(url: str) -> str:
    log(f'  Scraping: {url}')
    html = _fetch_playwright(url) if HAS_PLAYWRIGHT else ''
    if not html or len(html) < 5000:
        html = _fetch_simple(url)
    return html

def html_to_text(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.I)
    text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text,  flags=re.DOTALL | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    for ent, ch in [('&nbsp;',' '),('&amp;','&'),('&lt;','<'),('&gt;','>')]:
        text = text.replace(ent, ch)
    text = re.sub(r'&#\d+;', ' ', text)
    return re.sub(r'\s{3,}', '\n\n', text).strip()

def find_pdf_links(html: str, base_url: str) -> list:
    pdfs = []
    for m in re.finditer(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.I):
        href = m.group(1)
        if href.startswith('http'):
            pdfs.append(href)
        elif href.startswith('/'):
            p = urllib.parse.urlparse(base_url)
            pdfs.append(f'{p.scheme}://{p.netloc}{href}')
        else:
            pdfs.append(urllib.parse.urljoin(base_url, href))
    return list(dict.fromkeys(pdfs))


# ── PDF handling ───────────────────────────────────────────────────────────────

def download_pdf(url: str, post_id: int) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', url.split('/')[-1].split('?')[0])
    if not safe.lower().endswith('.pdf'):
        safe += '.pdf'
    dest = DOWNLOAD_DIR / f'{post_id}_{safe}'
    if dest.exists():
        log(f'  PDF cached: {dest.name}')
        return str(dest)
    try:
        req  = urllib.request.Request(url, headers=HTTP_HEADERS)
        resp = urllib.request.urlopen(req, timeout=20)
        data = resp.read()
        dest.write_bytes(data)
        log(f'  PDF: {dest.name} ({len(data)//1024}KB)')
        return str(dest)
    except Exception as e:
        log(f'  PDF download failed {url}: {e}')
        return ''

def extract_pdf_text(path: str) -> str:
    if not path or not Path(path).exists():
        return ''
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages[:20]:
                t = pg.extract_text()
                if t:
                    parts.append(t)
        return '\n'.join(parts)
    except Exception:
        pass
    try:
        import fitz
        doc   = fitz.open(path)
        parts = [pg.get_text() for pg in doc]
        return '\n'.join(parts)
    except Exception as e:
        log(f'  PDF text extraction failed: {e}')
        return ''

def download_expro_pdfs(expro_id: str, post_id: int) -> list:
    """Download standard wykończenia PDFs from ExPro (authenticated)."""
    inv   = load_expro(expro_id)
    docs  = [d for d in inv.get('documents', [])
             if re.search(r'standard|wykończ|wykonan', d.get('name', ''), re.I)]
    if not docs:
        return []
    log(f'  ExPro standard docs: {[d["name"] for d in docs]}')
    texts = []
    try:
        from scraper import login as expro_login
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_context(accept_downloads=True).new_page()
            expro_login(page)
            for doc in docs[:3]:
                url  = doc.get('url', '')
                safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc['name'])[:60]
                dest = DOWNLOAD_DIR / f'{post_id}_expro_{safe}.pdf'
                if dest.exists():
                    log(f'  ExPro PDF cached: {dest.name}')
                else:
                    try:
                        with page.expect_download(timeout=30000) as dl:
                            try:
                                page.goto(url, wait_until='commit', timeout=20000)
                            except Exception:
                                pass
                        dl.value.save_as(str(dest))
                        log(f'  ExPro PDF: {dest.name}')
                    except Exception as e:
                        log(f'  ExPro PDF failed {doc["name"]}: {e}')
                        continue
                t = extract_pdf_text(str(dest))
                if t:
                    texts.append(t)
            browser.close()
    except Exception as e:
        log(f'  ExPro PDF download error: {e}')
    return texts


# ── Claude parsing ─────────────────────────────────────────────────────────────

CLAUDE_PROMPT = """Jesteś asystentem analizującym teksty polskich inwestycji deweloperskich.

Na podstawie poniższego tekstu wyodrębnij dane w formacie JSON:

{{
  "standard_items": ["punkt 1", "punkt 2", ...],
  "opis_krotki": "...",
  "odleglosci": "...",
  "plan_platnosci": "...",
  "termin": "...",
  "cena_od": 0,
  "cechy": [
    {{"tytul": "...", "ikona": "dashicons-admin-home", "opis": "..."}},
    {{"tytul": "...", "ikona": "dashicons-shield",     "opis": "..."}},
    {{"tytul": "...", "ikona": "dashicons-car",        "opis": "..."}},
    {{"tytul": "...", "ikona": "dashicons-admin-home", "opis": "..."}}
  ]
}}

Zasady:
- standard_items: tylko konkretne punkty wykończenia (bez nagłówków, bez urwanych zdań, max 40)
- cechy: krótkie korzyści inwestycji dla kupującego (NIE nazwa dewelopera)
- Ikony: dashicons-admin-home, dashicons-shield, dashicons-car, dashicons-building, dashicons-heart
- Jeśli brak info → null
- Odpowiedz TYLKO czystym JSON

Tekst:
---
{text}
---"""

def parse_with_claude(text: str, api_key: str) -> dict:
    if not HAS_ANTHROPIC:
        log('  Claude: anthropic not installed')
        return {}
    if not api_key:
        log('  Claude: no API key (set ANTHROPIC_API_KEY in config.py or env)')
        return {}
    if len(text) < 100:
        log('  Claude: text too short')
        return {}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2048,
            messages=[{'role': 'user', 'content': CLAUDE_PROMPT.format(text=text[:30000])}],
        )
        raw = msg.content[0].text.strip()
        m   = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log(f'  Claude JSON error: {e}')
    except Exception as e:
        log(f'  Claude API error: {e}')
    return {}


# ── WP save helpers ────────────────────────────────────────────────────────────

def save_contacts(wp: WPClient, post_id: int, expro_data: dict) -> bool:
    contact = expro_data.get('contact', {})
    phone   = contact.get('phone', '').strip()
    email   = contact.get('email', '').strip()
    if not phone and not email:
        return False
    raw = wp.get_meta(post_id, 'projekt_kontakty_json')
    existing = {}
    try:
        existing = json.loads(raw) if raw else {}
    except Exception:
        pass
    existing.setdefault('sprzedaz', [])
    existing.setdefault('deweloper', [])
    if not existing['sprzedaz']:
        existing['sprzedaz'].append({
            'id':    f'c_expro_{post_id}',
            'imie':  contact.get('name', '') or 'Dział Sprzedaży',
            'rola':  'Handlowiec',
            'tel':   phone,
            'email': email,
            'uwagi': 'Źródło: ExPro',
        })
        wp.update_meta_json(post_id, 'projekt_kontakty_json', existing)
        log(f'  contacts saved: {phone} / {email}')
        return True
    log('  contacts already exist')
    return False


def save_documents(wp: WPClient, post_id: int, pdf_urls: list):
    if not pdf_urls:
        return
    raw = wp.get_meta(post_id, 'projekt_dokumenty_json')
    existing = []
    try:
        existing = json.loads(raw) if raw else []
    except Exception:
        pass
    known = {d.get('url') for d in existing}
    added = 0
    for url in pdf_urls:
        if url in known:
            continue
        name = url.split('/')[-1].split('?')[0]
        typ  = ('standard_wykonczenia' if re.search(r'standard', url, re.I) else
                'rzut'                 if re.search(r'rzut|plan', url, re.I) else 'inne')
        existing.append({'id': f'd_{post_id}_{len(existing)+1}', 'typ': typ,
                         'zrodlo': 'website', 'nazwa': name, 'url': url,
                         'data': datetime.now().strftime('%Y-%m-%d')})
        added += 1
    if added:
        wp.update_meta_json(post_id, 'projekt_dokumenty_json', existing)
        log(f'  documents saved: {added} new')


def save_parsed(wp: WPClient, post_id: int, parsed: dict, force: bool = False) -> list:
    saved = []

    def _set(key, value, is_json=False):
        if not value:
            return
        current = wp.get_meta(post_id, key).strip()
        if current and not force:
            return
        if is_json:
            wp.update_meta_json(post_id, key, value)
        else:
            wp.update_meta_plain(post_id, key, str(value))
        saved.append(key)

    items = parsed.get('standard_items')
    if items:
        _set('projekt_standard', '\n'.join(items))

    _set('projekt_dew_opis',     parsed.get('opis_krotki'))
    _set('projekt_odleglosci',   parsed.get('odleglosci'))
    _set('projekt_plan_platnosci', parsed.get('plan_platnosci'))
    _set('projekt_termin_oddania', parsed.get('termin'))

    cena = parsed.get('cena_od')
    if cena and int(cena) > 0:
        _set('projekt_cena_od', str(int(cena)))

    cechy = parsed.get('cechy')
    if cechy and isinstance(cechy, list):
        cechy_fmt = [{'title': c.get('tytul',''), 'desc': c.get('opis','')}
                     for c in cechy[:4] if isinstance(c, dict) and c.get('tytul')]
        if cechy_fmt:
            _set('projekt_cechy', cechy_fmt, is_json=True)

    if saved:
        log(f'  saved: {", ".join(saved)}')
    return saved


def append_log(wp: WPClient, post_id: int, entry: dict):
    raw = wp.get_meta(post_id, 'projekt_parse_log_json')
    lst = []
    try:
        lst = json.loads(raw) if raw else []
    except Exception:
        pass
    lst.append(entry)
    wp.update_meta_json(post_id, 'projekt_parse_log_json', lst[-20:])


# ── Enrichment flow ────────────────────────────────────────────────────────────

def enrich(post_id: int, expro_id: str, api_key: str = '', steps: list = None,
           force: bool = False) -> bool:
    steps    = steps or ['contacts', 'website', 'pdf', 'expro_pdf', 'claude', 'save']
    run_log  = {'data': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'ok', 'zrodla': {}, 'pola': [], 'uwagi': ''}

    log(f'\n{"="*60}')
    log(f'Enriching post {post_id}  (ExPro #{expro_id})')
    log(f'{"="*60}')

    wp = WPClient()
    wp.connect()

    try:
        expro_data   = load_expro(expro_id)
        subdomain    = wp.get_meta(post_id, 'projekt_subdomain_url').strip()
        website_text = ''
        pdf_urls     = []
        pdf_texts    = []

        # 1. Contacts
        if 'contacts' in steps and expro_data:
            log('→ contacts')
            if save_contacts(wp, post_id, expro_data):
                run_log['pola'].append('projekt_kontakty_json')
            run_log['zrodla']['expro'] = 'ok'

        # 2. Website scrape
        if 'website' in steps and subdomain:
            log('→ website')
            html = scrape_website(subdomain)
            if html and len(html) > 1000:
                website_text = html_to_text(html)
                pdf_urls     = find_pdf_links(html, subdomain)
                log(f'  {len(website_text)} chars, {len(pdf_urls)} PDFs found')
                run_log['zrodla']['website'] = 'ok'
            else:
                log('  no content')
                run_log['zrodla']['website'] = 'skip'

        # 3. Developer PDFs
        if 'pdf' in steps and pdf_urls:
            log(f'→ pdf ({len(pdf_urls)} links)')
            for url in pdf_urls[:5]:
                path = download_pdf(url, post_id)
                if path:
                    t = extract_pdf_text(path)
                    if t:
                        pdf_texts.append(t)
            run_log['zrodla']['pdf'] = 'ok' if pdf_texts else 'skip'

        # 4. ExPro PDFs (standard wykończenia)
        if 'expro_pdf' in steps and HAS_PLAYWRIGHT:
            log('→ expro_pdf')
            expro_texts = download_expro_pdfs(expro_id, post_id)
            if expro_texts:
                pdf_texts.extend(expro_texts)
                run_log['zrodla']['expro_pdf'] = 'ok'
            else:
                run_log['zrodla']['expro_pdf'] = 'skip'

        # 5. Claude parse
        parsed = {}
        if 'claude' in steps:
            all_text = '\n\n'.join(filter(None, [website_text] + pdf_texts))
            if all_text and len(all_text) > 200:
                log(f'→ claude ({len(all_text)} chars)')
                parsed = parse_with_claude(all_text, api_key)
                run_log['zrodla']['claude'] = 'ok' if parsed else 'skip'
            else:
                log('→ claude: no text')
                run_log['zrodla']['claude'] = 'skip'

        # 6. Save
        if 'save' in steps:
            log('→ save')
            save_documents(wp, post_id, pdf_urls)
            if parsed:
                saved = save_parsed(wp, post_id, parsed, force=force)
                run_log['pola'].extend(saved)
            wp.wp('litespeed-purge all')

        run_log['uwagi'] = (f'website={len(website_text)}c, '
                            f'pdfs={len(pdf_urls)}, '
                            f'claude_items={len(parsed.get("standard_items") or [])}')
        append_log(wp, post_id, run_log)
        log('✓ Done')
        return True

    except Exception as e:
        run_log['status'] = 'err'
        run_log['uwagi']  = str(e)
        try:
            append_log(wp, post_id, run_log)
        except Exception:
            pass
        log(f'✗ Pipeline failed: {e}')
        import traceback; traceback.print_exc()
        return False
    finally:
        wp.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Realsy investment enrichment pipeline')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all',     action='store_true', help='Enrich all posts missing projekt_standard')
    group.add_argument('--post-id', type=int,            help='Single WP post ID')
    parser.add_argument('--steps',   default='contacts,website,pdf,expro_pdf,claude,save')
    parser.add_argument('--api-key', default=_DEFAULT_API_KEY,
                        help='Anthropic API key (or set in config.py)')
    parser.add_argument('--force',   action='store_true',
                        help='Re-enrich even if data already exists')
    args   = parser.parse_args()
    steps  = [s.strip() for s in args.steps.split(',')]

    if not args.api_key and 'claude' in steps:
        log('WARNING: no ANTHROPIC_API_KEY — Claude step will be skipped')
        log('Set it in config.py: ANTHROPIC_API_KEY = "sk-ant-..."')

    ssh = SSHClient()
    ssh._connect()

    if args.all:
        pairs = get_all_posts_expro(ssh)
        log(f'Total posts: {len(pairs)}')
        if not args.force:
            # Filter to only posts that need enrichment
            needs = []
            for pid, eid in pairs:
                try:
                    standard = ssh.run_wp_cli(f'post meta get {pid} projekt_standard').strip()
                except Exception:
                    standard = ''
                if not standard:
                    needs.append((pid, eid))
            log(f'Needing enrichment: {len(needs)}')
            pairs = needs
    else:
        expro_id = ssh.run_wp_cli(f'post meta get {args.post_id} expro_id').strip()
        if not expro_id:
            log(f'ERROR: post {args.post_id} has no expro_id meta')
            sys.exit(1)
        pairs = [(args.post_id, expro_id)]

    ssh.close()

    ok = 0
    for i, (post_id, expro_id) in enumerate(pairs, 1):
        log(f'\n[{i}/{len(pairs)}]')
        if enrich(post_id, expro_id, api_key=args.api_key, steps=steps, force=args.force):
            ok += 1
        if len(pairs) > 1:
            time.sleep(2)

    log(f'\nDone: {ok}/{len(pairs)} enriched')


if __name__ == '__main__':
    main()
