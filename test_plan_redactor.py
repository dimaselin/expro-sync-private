"""Text-logic tests for plan_redactor — no tesseract needed."""
import sys, types, importlib.util, re
for name in ('pytesseract','PIL','PIL.Image','PIL.ImageFilter','PIL.ImageDraw'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['PIL'].Image = object; sys.modules['PIL'].ImageFilter = object; sys.modules['PIL'].ImageDraw = object
spec = importlib.util.spec_from_file_location('pr', sys.argv[1] if len(sys.argv)>1 else 'plan_redactor.py')
pr = importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)
T = ['Lokum Deweloper', 'Lokum PORTO']
LONG = pr._LONG_LINE_CHARS

def whole(text, terms=T):
    return pr.is_blocked_text(text, terms) and (len(text) <= LONG or pr.contact_signal(text))

MUST_BLUR = [
  "Salon Sprzedaży: Justin Center, ul. Krawiecka 1, pok. 101, I piętro, Wrocław tel. 71 796 66 66, e-mail: biuro@lokum.pl",
  "tel. 71 796 66 66",
  "Salon Sprzedaży:",
  "LOKUM DEWELOPER",
  "LOKUM",
  "Biuro sprzedaży: ul. Kwiatowa 3, 50-001 Wrocław, tel. 501 234 567",
  "kom. +48 501 234 567",
  "e-mail: sprzedaz@developer.pl",
  "www.developer.pl",
]
MUST_KEEP = [
  "POKÓJ 10,68 m2", "ŁAZIENKA 4,30 m2", "WC 2,32 m2",
  "POKÓJ DZIENNY Z ANEKSEM KUCHENNYM 43,00 m2",
  "0 100 200cm", "15.06.2026", "Powierzchnia 99,86 m2",
  "463 662 816 123",                      # wall dimension chain
  "283 8 323 8 323 543",                  # dimension chain
  "LOGGIA ~22,07 m2", "PRZEDPOKÓJ 11,78 m2",
  "Projektant:", "Inwestor:",
]
fails = []
for t in MUST_BLUR:
    if not whole(t): fails.append(("SHOULD BLUR", t))
for t in MUST_KEEP:
    if pr.is_blocked_text(t, T): fails.append(("SHOULD KEEP", t))
for kind, t in fails:
    print(f"FAIL {kind}: {t[:80]}")
print(f"{len(MUST_BLUR)} blur cases, {len(MUST_KEEP)} keep cases, {len(fails)} failures")
sys.exit(1 if fails else 0)
