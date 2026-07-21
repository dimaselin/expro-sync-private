"""
Detect and blur developer/investment-identifying text baked into ExPro floor
plan images (developer name, investment name/logo, sales-office contact:
phone, email, website, address). Different developers use different PDF
export templates (sidebar panel, top banner, bottom bar, etc.) — there is no
single fixed crop region that works across all of them — so this uses OCR to
find the actual text, then blurs only the matched regions, leaving the floor
plan drawing and legitimate labels (room names, areas, legend, disclaimer)
untouched.

Requires the `tesseract-ocr` binary (apt-get install tesseract-ocr
tesseract-ocr-pol on Debian/Ubuntu) and the `pytesseract` + `Pillow` Python
packages.

Usage as a library:
    from plan_redactor import redact_plan_image
    hits = redact_plan_image('/path/to/plan.jpg', extra_terms=[developer_name, investment_name])
    # image is modified in place; hits is a list of (text, confidence, box) that were blurred

Standalone CLI (for manual testing against a single file):
    python3 plan_redactor.py input.jpg output.jpg "Developer Name" "Investment Name"
"""
import re
import sys

try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageDraw
except ImportError:
    print("ERROR: pip install pytesseract Pillow  (and apt-get install tesseract-ocr tesseract-ocr-pol)")
    sys.exit(1)

# ── Blocklist patterns (universal — apply regardless of which investment) ───
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.\w+', re.I)
_DOMAIN_RE = re.compile(r'\b[\w-]+\.(pl|com|eu|info)\b', re.I)
_POSTAL_RE = re.compile(r'\b\d{2}-\d{3}\b')
_PHONE_KEYWORD_RE = re.compile(r'\btel\b\.?:?', re.I)
_PHONE_DIGITS_RE = re.compile(r'(\d[\d\s.\-oO]{6,}\d)')  # tolerate OCR 0/O confusion
_KEYWORDS = [
    'biuro sprzedaży', 'biuro sprzedazy', 'dział sprzedaży', 'dzial sprzedazy',
    'sprzedaż mieszkań', 'sprzedaz mieszkan', 'sprzedaz@', 'ul.', 'www.',
]


def is_blocked_text(text: str, extra_terms: list) -> bool:
    """True if this OCR'd text token should be redacted."""
    t = text.strip()
    if not t:
        return False
    tl = t.lower()
    if _EMAIL_RE.search(t):
        return True
    if _DOMAIN_RE.search(t):
        return True
    if _POSTAL_RE.search(t):
        return True
    if _PHONE_KEYWORD_RE.search(tl):
        return True
    # Phone-digit heuristic only for short, mostly-numeric tokens — a real
    # phone number OCRs as its own short token; long sentences that merely
    # contain a date or a regulation/article number (common in the legal
    # disclaimer every plan carries) must not match just because they have
    # 7+ digits somewhere inside.
    digits_only = re.sub(r'\D', '', t)
    if len(t) <= 20 and _PHONE_DIGITS_RE.search(t) and len(digits_only) >= 7:
        return True
    for kw in _KEYWORDS:
        if kw in tl:
            return True
    text_words = set(re.findall(r'\w+', tl))
    for term in extra_terms:
        term = str(term or '').strip().lower()
        if not term:
            continue
        # Either direction: a short OCR token can be a substring of a long
        # investment name, or (rarely) the whole term can appear in a longer
        # OCR'd line. A plain "term in tl" check alone misses this because
        # Tesseract detects text per-word/per-line, not per-phrase — the
        # investment name "Elewator - Mieszkania i Lofty" never appears as a
        # single OCR token, only fragments like "ELEWATOR" on their own line.
        if term in tl or tl in term:
            return True
        # Word-level overlap: any distinctive (4+ char) word shared between
        # the known term and the OCR'd text, e.g. "ELEWATOR" line matching
        # the "Elewator - Mieszkania i Lofty" investment name.
        term_words = [w for w in re.findall(r'\w+', term) if len(w) >= 4]
        if any(w in text_words for w in term_words):
            return True
    return False


def _ocr_lines(image_path: str, langs: str = 'pol+eng'):
    """Yield (text, confidence_0_100, (x0,y0,x1,y1)) per detected LINE.

    Tesseract's image_to_data() returns per-WORD boxes, but multi-word
    phrases (phone numbers with spaces, "Biuro sprzedaży", investment names
    split across words) need to be matched as a whole — a lone "536" or
    "Biuro" token doesn't look like a phone number or sales-office keyword by
    itself. Words are grouped by Tesseract's own (block, paragraph, line)
    indices and rejoined in reading order; the line's bounding box is the
    union of its words' boxes, so blurring still stays tight around only the
    text that was actually flagged.
    """
    data = pytesseract.image_to_data(Image.open(image_path), lang=langs,
                                      output_type=pytesseract.Output.DICT)
    n = len(data['text'])
    lines = {}
    for i in range(n):
        text = data['text'][i].strip()
        if not text:
            continue
        conf = int(data['conf'][i]) if str(data['conf'][i]).lstrip('-').isdigit() else -1
        x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        entry = lines.setdefault(key, {'words': [], 'confs': [], 'box': None})
        entry['words'].append((data['word_num'][i], text))
        entry['confs'].append(conf)
        box = (x, y, x + w, y + h)
        if entry['box'] is None:
            entry['box'] = box
        else:
            bx0, by0, bx1, by1 = entry['box']
            entry['box'] = (min(bx0, box[0]), min(by0, box[1]),
                             max(bx1, box[2]), max(by1, box[3]))

    for entry in lines.values():
        words = [w for _, w in sorted(entry['words'])]
        line_text = ' '.join(words)
        line_conf = max(entry['confs']) if entry['confs'] else -1
        yield line_text, line_conf, entry['box']


def redact_plan_image(image_path: str, extra_terms: list = None, pad: int = 6,
                       min_conf: int = 30, blur_radius: int = 18,
                       out_path: str = None) -> list:
    """
    Detects and blurs identifying text in-place (or to out_path if given).
    Returns the list of (text, confidence, box) regions that were blurred —
    empty list means nothing matched, image left untouched.
    """
    extra_terms = extra_terms or []
    img = Image.open(image_path).convert('RGB')
    blurred_full = img.filter(ImageFilter.GaussianBlur(blur_radius))
    mask = Image.new('L', img.size, 0)
    draw = ImageDraw.Draw(mask)

    hits = []
    for text, conf, box in _ocr_lines(image_path):
        if conf < min_conf:
            continue
        if is_blocked_text(text, extra_terms):
            x0, y0, x1, y1 = box
            padded = (max(0, x0 - pad), max(0, y0 - pad),
                      min(img.width, x1 + pad), min(img.height, y1 + pad))
            draw.rectangle(padded, fill=255)
            hits.append((text, conf, padded))

    if hits:
        out = Image.composite(blurred_full, img, mask)
        out.save(out_path or image_path, quality=90)
    return hits


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    extra = sys.argv[3:]
    found = redact_plan_image(in_path, extra_terms=extra, out_path=out_path)
    print(f"Blurred {len(found)} region(s):")
    for text, conf, box in found:
        print(f"  conf={conf} {text!r} -> {box}")
