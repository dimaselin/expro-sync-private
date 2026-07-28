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
# Anchored to the WHOLE token (not just a substring) so a floor-plan scale
# bar like "0 100 200cm" doesn't match — the trailing unit letters would
# otherwise be ignored by a plain .search() that only needs *some* digit run
# inside the token.
_PHONE_DIGITS_RE = re.compile(r'^\d[\d\s.\-oO]{6,}\d$')  # tolerate OCR 0/O confusion
# A bare revision-date stamp ("15.06.2026") structurally looks just like a
# phone number to _PHONE_DIGITS_RE (digits separated by dots) but isn't
# developer/investment-identifying info at all — exclude it explicitly.
_DATE_RE = re.compile(r'^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$')
_KEYWORDS = [
    'biuro sprzedaży', 'biuro sprzedazy', 'dział sprzedaży', 'dzial sprzedazy',
    # Lokum's plans label the block "Salon Sprzedaży", which none of the
    # variants above matched — its address and phone stayed in the clear.
    'salon sprzedaży', 'salon sprzedazy',
    'sprzedaż mieszkań', 'sprzedaz mieszkan', 'sprzedaz@', 'ul.', 'www.',
]

# Keywords that make a line a contact/sales-office line in its own right, as
# opposed to a legal sentence that merely ends with a developer's domain. Lines
# carrying one of these are blurred whole, however long they are.
_CONTACT_KEYWORDS = [
    'biuro sprzedaży', 'biuro sprzedazy', 'dział sprzedaży', 'dzial sprzedazy',
    'salon sprzedaży', 'salon sprzedazy', 'sprzedaż mieszkań', 'sprzedaz mieszkan',
]

# Generic Polish real-estate/floor-plan vocabulary that legitimately recurs in
# room labels, dimensions, and the legal disclaimer on every plan. These words
# are excluded from extra_terms word-overlap matching even when they happen to
# be part of an investment's name (e.g. "Elewator - Mieszkania i Lofty")
# — otherwise every plan's "Wejście do mieszkania" / "Wysokość mieszkania"
# label and the disclaimer paragraph (which mentions "aranżacji mieszkania")
# get wrongly blurred just because they share the word "mieszkania".
_GENERIC_WORDS = {
    'mieszkanie', 'mieszkania', 'mieszkań', 'mieszkaniu', 'mieszkaniem',
    # "lokum" was here as a common noun for "dwelling", but it is also a
    # developer's brand — and floor plans do not label rooms with it, so the
    # only thing the exclusion protected was the developer's own name.
    'dom', 'domu', 'domy', 'domów', 'lokal', 'lokalu',
    'budynek', 'budynku', 'budynki', 'inwestycja', 'inwestycji',
    'parking', 'piwnica', 'piwnicy', 'balkon', 'balkonu', 'taras', 'tarasu',
    'strefa', 'strefy', 'klatka', 'klatki', 'schodowa', 'winda', 'windy',
    'korytarz', 'korytarza', 'wysokość', 'wysokości', 'powierzchnia',
    'powierzchni', 'wejście', 'wejscie', 'rzut', 'plan', 'piętro', 'pietro',
    'osiedle', 'osiedla', 'osiedlu',
}


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
    if has_phone_number(t):
        return True
    # Phone-digit heuristic only for short, mostly-numeric tokens — a real
    # phone number OCRs as its own short token; long sentences that merely
    # contain a date or a regulation/article number (common in the legal
    # disclaimer every plan carries) must not match just because they have
    # 7+ digits somewhere inside. _PHONE_DIGITS_RE is now whole-token
    # anchored (see definition) to exclude a scale-bar label like
    # "0 100 200cm", and a plain revision-date stamp ("15.06.2026") is
    # excluded explicitly — it looks identical in shape to a phone number
    # but isn't developer/investment-identifying info.
    # A dimension chain along a wall ("463 662 816 123") OCRs as one line and
    # satisfies this shape exactly — same digits, same separators, same length
    # bracket as a phone. Requiring a context marker is what separates the two;
    # without it this rule blurred part of the drawing.
    digits_only = re.sub(r'\D', '', t)
    if (len(t) <= 20 and not _DATE_RE.match(t)
            and _PHONE_DIGITS_RE.match(t) and len(digits_only) >= 7
            and _PHONE_CONTEXT_RE.search(t)):
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
        # The "tl in term" direction needs a length floor: without it, a
        # single short conjunction like "i" (or "w", "z", "do") is trivially
        # a substring of almost any long investment name and wrongly matches.
        if term in tl or (len(tl) >= 4 and tl in term):
            return True
        # Word-level overlap: any distinctive (4+ char, non-generic) word
        # shared between the known term and the OCR'd text, e.g. "ELEWATOR"
        # line matching the "Elewator - Mieszkania i Lofty" investment name.
        # Generic real-estate words (see _GENERIC_WORDS) are excluded even
        # when part of the term name, since they recur constantly in
        # legitimate room labels and the plan's legal disclaimer.
        term_words = [w for w in re.findall(r'\w+', term)
                       if len(w) >= 4 and w not in _GENERIC_WORDS]
        if any(w in text_words for w in term_words):
            return True
    return False


# Signals that are fully self-contained within a single OCR token — an email
# address, a domain, or a postal code never needs neighboring words to be
# recognized as identifying info. These are the only signals eligible for
# the tight-box fallback below; _KEYWORDS phrases like "ul." and extra_terms
# name fragments are deliberately excluded because they're prefixes/partial
# matches whose whole point is to catch the surrounding words too (e.g. "ul."
# alone is meaningless — the actual street address is what needs hiding).
def _self_complete_hit(word_text: str) -> bool:
    t = word_text.strip()
    return bool(t) and bool(_EMAIL_RE.search(t) or _DOMAIN_RE.search(t) or _POSTAL_RE.search(t))


# A phone is only recognised next to something that announces one. Floor plans
# are covered in bare numbers — a wall's dimension chain OCRs as a single line
# like "463 662 816 123", which joins to 12 digits and is indistinguishable
# from a phone by shape alone. Blurring that would eat the drawing, so a
# context marker is required and the digit count is capped.
_PHONE_CONTEXT_RE = re.compile(r'\b(tel|telefon|kom|fax|faks|mob)\b\.?:?|\+\s?48', re.I)


def has_phone_number(text: str) -> bool:
    """True if the line contains something shaped like a phone number.

    _PHONE_DIGITS_RE only ever sees one token at a time, and Tesseract splits
    "tel. 71 796 66 66" into four short tokens, none of which reaches the
    seven-digit floor — so a printed sales-office phone looked like four
    harmless numbers and survived redaction. Adjacent numeric tokens are
    joined here before the length test, which is how a human reads them.
    """
    if not _PHONE_CONTEXT_RE.search(text):
        return False
    tokens = text.split()
    run = []
    for tok in tokens + ['']:
        cleaned = tok.strip('.,;:()')
        is_num = bool(cleaned) and bool(re.fullmatch(r'[+()\d\s.\-oO]+', cleaned)) \
            and any(ch.isdigit() for ch in cleaned)
        if is_num and not _DATE_RE.match(cleaned):
            run.append(cleaned)
            continue
        if run:
            digits = re.sub(r'\D', '', ''.join(run))
            # 9 digits is a full Polish number, 11 with the country code. Longer
            # runs are dimension chains, not numbers anyone can dial.
            if 9 <= len(digits) <= 12:
                return True
            run = []
    return False


# Signals that make a line a contact/identity line on its own, regardless of
# how long it is. A long legal disclaimer that merely ends in a developer
# domain is NOT one of these — that distinction is the whole point of
# _LONG_LINE_CHARS, and it stays intact for the weak signals.
def contact_signal(text: str) -> bool:
    tl = text.lower()
    if _EMAIL_RE.search(text):
        return True
    if _POSTAL_RE.search(text):
        return True
    if _PHONE_KEYWORD_RE.search(tl) and has_phone_number(text):
        return True
    if has_phone_number(text):
        return True
    return any(kw in tl for kw in _CONTACT_KEYWORDS)


# Above this line length, a match is assumed to be a long legal-disclaimer
# or legend sentence that merely *contains* a legitimate short signal (most
# often a developer's domain tacked on as a footer) rather than being an
# identity/contact line in its own right — verified against a live batch
# run where a 179-char disclaimer sentence ending in "dekpoldeweloper.pl"
# got fully blurred, while every real contact/address line observed stayed
# under 65 chars.
_LONG_LINE_CHARS = 100


def _ocr_lines(image_path: str, langs: str = 'pol+eng'):
    """Yield (text, confidence_0_100, (x0,y0,x1,y1), words) per detected LINE.

    Tesseract's image_to_data() returns per-WORD boxes, but multi-word
    phrases (phone numbers with spaces, "Biuro sprzedaży", investment names
    split across words) need to be matched as a whole — a lone "536" or
    "Biuro" token doesn't look like a phone number or sales-office keyword by
    itself. Words are grouped by Tesseract's own (block, paragraph, line)
    indices and rejoined in reading order; the line's bounding box is the
    union of its words' boxes, so blurring still stays tight around only the
    text that was actually flagged. `words` (a list of (text, box) pairs) is
    also yielded so callers can fall back to a tighter per-word box on long
    lines — see _LONG_LINE_CHARS.
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
        box = (x, y, x + w, y + h)
        entry['words'].append((data['word_num'][i], text, box))
        entry['confs'].append(conf)
        if entry['box'] is None:
            entry['box'] = box
        else:
            bx0, by0, bx1, by1 = entry['box']
            entry['box'] = (min(bx0, box[0]), min(by0, box[1]),
                             max(bx1, box[2]), max(by1, box[3]))

    for entry in lines.values():
        ordered = sorted(entry['words'])
        words = [(w, b) for _, w, b in ordered]
        line_text = ' '.join(w for w, _ in words)
        line_conf = max(entry['confs']) if entry['confs'] else -1
        yield line_text, line_conf, entry['box'], words


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
    for text, conf, box, words in _ocr_lines(image_path):
        if conf < min_conf:
            continue
        if not is_blocked_text(text, extra_terms):
            continue
        target_box = box
        target_text = text
        # A line carrying a real contact signal is a contact line no matter how
        # long it is. Without this, a wrapped sales-office block ("Salon
        # Sprzedaży: <address>, tel. 71 796 66 66, e-mail: ...") fell into the
        # long-line branch below, which blurred only the e-mail token and left
        # the address and phone readable — exactly what was found live.
        if len(text) > _LONG_LINE_CHARS and not contact_signal(text):
            self_hits = [(w, b) for w, b in words if _self_complete_hit(w)]
            if self_hits:
                xs0 = min(b[0] for _, b in self_hits)
                ys0 = min(b[1] for _, b in self_hits)
                xs1 = max(b[2] for _, b in self_hits)
                ys1 = max(b[3] for _, b in self_hits)
                target_box = (xs0, ys0, xs1, ys1)
                target_text = ' '.join(w for w, _ in self_hits)
            else:
                # Long line matched only via a multi-word signal (phone
                # digits split across words, a keyword phrase, extra_terms)
                # with no single self-contained word to fall back to —
                # skip rather than risk blurring an entire long sentence.
                continue
        x0, y0, x1, y1 = target_box
        padded = (max(0, x0 - pad), max(0, y0 - pad),
                  min(img.width, x1 + pad), min(img.height, y1 + pad))
        draw.rectangle(padded, fill=255)
        hits.append((target_text, conf, padded))

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
