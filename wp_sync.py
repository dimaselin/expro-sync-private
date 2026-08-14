from typing import Optional, Tuple
"""
WordPress sync — reads data/expro_data.json, creates/updates inwestycja posts via WP-CLI over SSH
"""

import json
import re
import shlex
import os
import sys
import datetime
import tempfile
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

try:
    from config import SSH, WP, DATA_FILE, STATUS_MAP
except ImportError:
    SSH = {
        "host": "82.198.229.58",
        "port": 65002,
        "username": "u525644354",
        "password": "Strona2026!",
    }
    WP = {
        "path": "/home/u525644354/domains/realsymanagement.pl/public_html",
        "wp_cli": "/home/u525644354/domains/realsymanagement.pl/public_html/wp",
        "post_type": "inwestycja",
        "post_status": "publish",
        "post_author": 1,
    }
    DATA_FILE = "data/expro_data.json"
    STATUS_MAP = {
        "Dostępne": "dostepne",
        "Zarezerwowane": "zarezerwowane",
        "Sprzedane": "sprzedane",
    }

WP_PATH = WP["path"]
WP_CLI = WP.get("wp_cli", "wp")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------

class SSHClient:
    def __init__(self):
        self._client: Optional[paramiko.SSHClient] = None

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=SSH["host"],
            port=SSH["port"],
            username=SSH["username"],
            password=SSH["password"],
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        client.get_transport().set_keepalive(30)
        self._client = client
        log("SSH connected.")

    def _ensure_connected(self):
        if self._client is None:
            self._connect()
            return
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                log("SSH connection lost — reconnecting …")
                self._connect()
        except Exception:
            self._connect()

    def run(self, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
        """Run a shell command. Returns (stdout, stderr, exit_code)."""
        self._ensure_connected()
        try:
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            code = stdout.channel.recv_exit_status()
            return out, err, code
        except Exception as e:
            log(f"SSH exec error: {e}")
            # force reconnect on next call
            self._client = None
            raise

    def run_wp_cli(self, wp_args: str, timeout: int = 60) -> str:
        """Run a WP-CLI command. Returns stdout. Raises on failure."""
        # Try configured path first, then fallback
        for cli in [WP_CLI, "wp", "/usr/local/bin/wp"]:
            cmd = f"{cli} {wp_args} --path={shlex.quote(WP_PATH)} --allow-root"
            out, err, code = self.run(cmd, timeout=timeout)
            if code == 127:
                # command not found — try next
                continue
            if code != 0:
                raise RuntimeError(f"WP-CLI error (exit {code}): {err or out}")
            return out
        raise RuntimeError("WP-CLI binary not found. Tried: " + ", ".join([WP_CLI, "wp", "/usr/local/bin/wp"]))

    def write_remote_file(self, content: str, remote_path: str):
        """Write content to a remote temp file via SFTP."""
        self._ensure_connected()
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def remove_remote_file(self, remote_path: str):
        self._ensure_connected()
        try:
            sftp = self._client.open_sftp()
            sftp.remove(remote_path)
            sftp.close()
        except Exception:
            pass

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# Image import
# ---------------------------------------------------------------------------

# ExPro names every file with a 32-character hash, and that hash survives into
# the uploaded file name whatever route the import took.
_EXPRO_HASH_RE = re.compile(r"[0-9a-f]{32}")

# hash → attachment ID, built once per run.
_ATTACHMENT_INDEX: Optional[dict] = None


def _load_attachment_index(ssh: SSHClient) -> dict:
    """Index every attachment by the ExPro hash in its source URL or file name.

    Matching on _source_url alone is not enough: it is empty on 6030 of the
    10509 attachments in the library, because imports done from a local file
    never set it. Nine of fourteen sampled gallery images went unrecognised
    that way, so a run that touches every investment — which any change to
    scrape_hash causes — would have re-imported most of the gallery as
    duplicates.
    """
    global _ATTACHMENT_INDEX
    if _ATTACHMENT_INDEX is not None:
        return _ATTACHMENT_INDEX
    php = (
        "<?php\nglobal $wpdb;\n"
        "$rows=$wpdb->get_results(\"SELECT post_id, meta_value FROM {$wpdb->postmeta} "
        "WHERE meta_key IN ('_source_url','_wp_attached_file') AND meta_value<>''\");\n"
        "foreach($rows as $r) echo $r->post_id.'|'.$r->meta_value.\"\\n\";\n"
    )
    index: dict = {}
    try:
        ssh.write_remote_file(php, "/tmp/esm_att_index.php")
        out = ssh.run_wp_cli("eval-file /tmp/esm_att_index.php", timeout=180)
        for line in out.splitlines():
            pid, _, value = line.partition("|")
            if not pid.strip().isdigit():
                continue
            for h in _EXPRO_HASH_RE.findall(value.lower()):
                index.setdefault(h, int(pid))
    except Exception as e:
        log(f"  WARN attachment index unavailable ({e}) — dedup falls back to _source_url")
    finally:
        ssh.remove_remote_file("/tmp/esm_att_index.php")
    log(f"  attachment index: {len(index)} known images")
    _ATTACHMENT_INDEX = index
    return index


def check_image_exists(ssh: SSHClient, image_url: str) -> Optional[int]:
    """Return attachment ID if this ExPro image was already imported."""
    found = _EXPRO_HASH_RE.findall(image_url.lower())
    if found:
        hit = _load_attachment_index(ssh).get(found[-1])
        if hit:
            return hit
    try:
        wp_args = (
            f"post list --post_type=attachment "
            f"--meta_key=_source_url --meta_value={shlex.quote(image_url)} "
            f"--fields=ID --format=csv"
        )
        out = ssh.run_wp_cli(wp_args)
        lines = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "ID"]
        if lines and lines[0].isdigit():
            return int(lines[0])
    except Exception:
        pass
    return None


def import_image_to_wp(ssh: SSHClient, image_url: str, post_id: int,
                        local_path: Optional[str] = None) -> Optional[int]:
    """Import an image into WP media library. Returns attachment ID or None.

    If `local_path` is given (a file already downloaded with proper auth —
    see api_scraper.py's image_url_map, needed for /files/photos/ URLs which
    require an authenticated ExPro session), it's SFTP-uploaded to the
    server and imported from that local server path instead of `image_url`
    directly. `wp media import <remote_url>` runs ON the WP server with no
    ExPro session at all — for an auth-gated URL it downloads the
    /user/login redirect's HTML instead of the image, and WordPress
    correctly refuses it as the wrong file type. Confirmed live 2026-07-23
    against 3 investments (Aleja Platanowa 2, Osiedle Nasza Symfonia etap
    II, Lokum Porto Finale) that all had 0 gallery images despite real URLs
    in the ExPro data.
    """
    # dedup check
    existing = check_image_exists(ssh, image_url)
    if existing:
        return existing

    try:
        if local_path and os.path.exists(local_path):
            remote_tmp = f"/tmp/esm_gallery_{post_id}_{os.path.basename(local_path)}"
            sftp = ssh._client.open_sftp()
            try:
                sftp.put(local_path, remote_tmp)
            finally:
                sftp.close()
            wp_args = (
                f"media import {shlex.quote(remote_tmp)} "
                f"--post_id={post_id} --porcelain"
            )
        else:
            wp_args = (
                f"media import {shlex.quote(image_url)} "
                f"--post_id={post_id} --porcelain"
            )
        out = ssh.run_wp_cli(wp_args, timeout=120)
        att_id = out.strip()
        if att_id.isdigit():
            if local_path:
                # wp media import from a local path doesn't set _source_url
                # (that's normally set by media_sideload_image() when
                # importing from a URL) — set it explicitly so
                # check_image_exists() can still dedup on future runs.
                ssh.run_wp_cli(f"post meta update {att_id} _source_url {shlex.quote(image_url)}")
            return int(att_id)
        log(f"    Image import returned non-ID: {att_id!r}")
    except Exception as e:
        log(f"    Image import failed for {image_url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Meta update (handles long values via remote temp file)
# ---------------------------------------------------------------------------

def update_meta(ssh: SSHClient, post_id: int, key: str, value: str):
    """Update a single post meta field. Uses eval+file for long values."""
    if len(value) > 1000 or "\n" in value or '"' in value:
        remote_tmp = f"/tmp/wp_meta_{post_id}_{key}.txt"
        try:
            ssh.write_remote_file(value, remote_tmp)
            php = (
                f"update_post_meta({post_id}, "
                f"'{key}', "
                f"file_get_contents('{remote_tmp}'));"
            )
            ssh.run_wp_cli(f"eval {shlex.quote(php)}")
        finally:
            ssh.remove_remote_file(remote_tmp)
    else:
        ssh.run_wp_cli(f"post meta update {post_id} {shlex.quote(key)} {shlex.quote(value)}")


def update_meta_bulk(ssh: SSHClient, post_id: int, pairs: dict) -> int:
    """Write many meta fields for one post in a single WP-CLI call.

    One call per field meant roughly forty WordPress bootstraps per
    investment. Measured: about 90 seconds each, and — the giveaway — the same
    90 seconds for a 3-unit investment as for a 161-unit one, so the cost was
    round trips rather than payload. Batched, the whole run drops from hours to
    minutes, and the daily cron benefits identically.
    """
    if not pairs:
        return 0
    data_path = f"/tmp/esm_meta_{post_id}.json"
    php_path  = f"/tmp/esm_meta_{post_id}.php"
    php = (
        "<?php\n"
        f"$d = json_decode(file_get_contents('{data_path}'), true);\n"
        "if (!is_array($d)) { echo 0; return; }\n"
        f"foreach ($d as $k => $v) update_post_meta({post_id}, $k, (string) $v);\n"
        "echo count($d);\n"
    )
    try:
        ssh.write_remote_file(json.dumps(pairs, ensure_ascii=False), data_path)
        ssh.write_remote_file(php, php_path)
        ssh.run_wp_cli(f"eval-file {php_path}", timeout=180)
        return len(pairs)
    except Exception as e:
        log(f"  bulk meta update failed ({len(pairs)} fields): {e}")
        return 0
    finally:
        ssh.remove_remote_file(data_path)
        ssh.remove_remote_file(php_path)


# ---------------------------------------------------------------------------
# Content builder
# ---------------------------------------------------------------------------

def build_post_content(inv: dict) -> str:
    desc = inv.get("description", {})
    parts = []

    # The developer's name is deliberately absent. It is kept in
    # projekt_developer for internal use, but naming them in the body text hands
    # a visitor the party to call instead of us — and the standing rule on this
    # site is that no page names the developer. It was printed in this opening
    # line on 101 of the 102 published investments; the meta stays, the sentence
    # does not.
    city = inv.get("city", "")
    if city:
        parts.append(f"<p>{city}</p>")

    for key, label in [
        ("udogodnienia", "Udogodnienia"),
        ("przynaleznosci", "Przynależności"),
        ("bezpieczenstwo", "Bezpieczeństwo"),
        ("garaz", "Garaż / Parking"),
        ("komunikacja", "Komunikacja"),
        ("odleglosc_centrum", "Odległość od centrum"),
    ]:
        val = desc.get(key, "").strip()
        if val:
            parts.append(f"<p><strong>{label}:</strong> {val}</p>")

    units_available = inv.get("units_available", 0)
    units_count = inv.get("units_count", 0)
    parts.append("<h3>Lokale</h3>")
    parts.append(f"<p>Liczba dostępnych lokali: {units_available} / {units_count}</p>")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Unit field accessor — handles both Polish keys (top5.json) and English keys (scraper.py)
# ---------------------------------------------------------------------------

def _uval(unit: dict, *keys: str) -> str:
    """Get first non-empty value from a unit dict trying multiple key names."""
    for k in keys:
        v = unit.get(k, "")
        if v and str(v).strip():
            return str(v).strip()
    return ""


# ---------------------------------------------------------------------------
# Price / area / rooms formatting
# ---------------------------------------------------------------------------

def format_price(inv: dict) -> str:
    """Extract minimum price from raw string like '965 000,00 PLN do 1 199 000,00 PLN'."""
    raw = inv.get("price_from_raw", "")
    if not raw:
        return ""
    # Take only the first price (before " do ")
    first = raw.split(" do ")[0].strip()
    # Remove ",00" decimal suffix
    first = re.sub(r",00\b", "", first).strip()
    first = re.sub(r"\s+", " ", first)
    if "PLN" not in first.upper():
        first = first + " PLN"
    return first


def format_area(inv: dict) -> str:
    """Derive area range from units table (avoids area_from/area_to parsing bugs)."""
    units = inv.get("units", [])
    areas: list[float] = []
    for u in units:
        raw = _uval(u, "Powierzchnia", "area_raw")
        m = re.search(r"(\d+[,.]\d+)", raw)
        if m:
            try:
                areas.append(float(m.group(1).replace(",", ".")))
            except ValueError:
                pass
    if not areas:
        # fallback to stored area_from/area_to only if they look valid (> 10 m²)
        af = inv.get("area_from")
        at = inv.get("area_to")
        if af and float(af) > 10:
            areas = [float(af)]
            if at and float(at) > 10 and float(at) != float(af):
                areas.append(float(at))
    if not areas:
        return ""
    mn, mx = min(areas), max(areas)
    fmt = lambda v: str(int(v)) if v == int(v) else str(round(v, 2))
    return f"od {fmt(mn)} do {fmt(mx)} m²" if mx != mn else f"{fmt(mn)} m²"


def derive_rooms(inv: dict) -> str:
    """Derive room range string from units list (handles Polish + English keys)."""
    units = inv.get("units", [])
    room_nums: list[int] = []
    for u in units:
        raw = _uval(u, "Pokoje", "rooms")
        m = re.search(r"\d+", raw)
        if m:
            room_nums.append(int(m.group(0)))
    if not room_nums:
        return ""
    mn, mx = min(room_nums), max(room_nums)
    if mn == mx:
        return f"{mn} pokoje"
    return f"{mn}–{mx} pokoje"


def derive_price_m2(inv: dict) -> str:
    """Average price per m² across the units (Cena m2 / price_m2_raw).

    The pattern used to require a trailing "PLN", which the Playwright scraper
    emitted ("15 008,00 PLN") but the API scraper does not — it writes the bare
    integer "15008". So this matched 0 of 7015 units and returned "" every run;
    because the caller only writes truthy values, projekt_cena_za_m2 kept
    whatever the old scraper had left behind and has been frozen since the
    migration. The number is now read with or without the currency, and ExPro's
    own investment-level figure is the fallback when no unit carries one.
    """
    units = inv.get("units", [])
    vals: list[float] = []
    for u in units:
        raw = _uval(u, "Cena m2", "price_m2_raw")
        m = re.search(r"\d[\d\s]*(?:[,.]\d+)?", raw)
        if m:
            try:
                num = re.sub(r"\s", "", m.group(0)).replace(",", ".")
                val = float(num)
                if val > 0:
                    vals.append(val)
            except ValueError:
                pass
    if vals:
        return str(round(sum(vals) / len(vals)))

    fallback = inv.get("price_m2_from")
    try:
        if fallback and float(fallback) > 0:
            return str(round(float(fallback)))
    except (TypeError, ValueError):
        pass
    return ""


def komisja_rate(inv: dict, key: str) -> str:
    """Pull one numeric value out of zasady_wspolpracy as a bare decimal string.

    ExPro stores rates as "2.10" / "1,4" and terms as "14" or "14 dni";
    the flat meta fields have to be plain numbers to be sortable in WP.
    """
    raw = (inv.get("zasady_wspolpracy") or {}).get(key, "")
    m = re.search(r"\d+(?:[.,]\d+)?", str(raw).replace(",", "."))
    if not m:
        return ""
    return str(float(m.group())).rstrip("0").rstrip(".")


def map_status(inv: dict, delivery: str = "") -> str:
    """How far along the building is — which is what projekt_status means.

    This used to answer a different question: "dostepne" whenever any unit was
    free, "sprzedane" otherwise. But projekt_status is an admin select whose
    vocabulary is przedsprzedaz / w_budowie / gotowe / sprzedane, and every
    template reads it as a construction state through a map with no "dostepne"
    key — so it fell through to the default. Measured on the live site: 160 of
    167 investments held "dostepne" and therefore displayed "W budowie" as a
    constant, including the five ExPro reports as already delivered.

    Sold out still wins: an investment with nothing left to sell is not
    described by how far along the building is. Otherwise the answer comes from
    the delivery date, parsed exactly the way _km_termin_bucket() in the
    templates parses it, so the badge and the filters can never disagree.

    An absent or unparseable date returns "", which drops the pair from the
    bulk write — whatever a human set in the admin survives rather than being
    overwritten with a guess.
    """
    # "We saw no available units" and "we failed to read the unit list" arrive
    # here as the same zero: fetch_units_list raising leaves raw_units empty and
    # units_available at 0. count_realestates comes from the investment record
    # itself and is not affected by that failure, so a feed that declares units
    # while we collected none is a failed read, not a sold-out development —
    # and claiming "Sprzedane" on a live development is the more expensive of
    # the two mistakes. Say nothing instead.
    declared  = int(inv.get("units_count") or 0)
    collected = len(inv.get("units") or [])
    if declared > 0 and collected == 0:
        return ""
    if not inv.get("units_available", 0):
        return "sprzedane"

    raw = str(delivery or inv.get("delivery", "") or "").strip()
    low = raw.lower()
    if not raw or "brak danych" in low:
        return ""
    if "oddane" in low:
        return "gotowe"

    day = None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            day = None
    else:
        m = re.search(r"\b(IV|III|II|I)\s*kw(?:arta[łl]|\.)?\s*(\d{4})", raw, re.IGNORECASE)
        if m:
            # The quarter is only over at its end, so a Q4 date is not late
            # until December is.
            month = {"I": 3, "II": 6, "III": 9, "IV": 12}[m.group(1).upper()]
            day = datetime.date(int(m.group(2)), month, 28)
    if not day:
        return ""

    return "gotowe" if day <= datetime.date.today() else "w_budowie"


_TYP_PATTERNS = [
    (["bliźniak", "blizniak"],                          "blizniak"),
    (["szeregówka", "szeregowka", "szereg"],             "szeregowka"),
    (["wolnostojący", "wolnostojacy", "wolnostoj"],      "wolnostojacy"),
    (["mieszkanie", "mieszkania", "apartament"],         "mieszkanie"),
    (["dom"],                                            "dom"),
]

# ExPro sends "Mieszkanie" as unit type even for villas/row-houses/commercial.
# These overrides prevent sync from reverting manually-corrected projekt_typ.
# Kept permanently as a safety net even though detect_projekt_typ_from_name()
# below now also catches most of these cases generically — overrides always
# win first, so there's no conflict in leaving them here.
_PROJEKT_TYP_OVERRIDES: dict[str, str] = {
    # Corrected 2026-07-29. These two were pinned to "dom" on the assumption
    # that ExPro mislabels villas as "Mieszkanie". It does not here: both
    # investments report Mieszkanie for every unit (46 and 26), building_type_id
    # 1, and the per-unit classifier in mieszkania_sync.py independently resolves
    # all 92 live units to `mieszkanie` — its own comments say "Wille Biskupin
    # sells flats". The investment was the only thing still calling them houses,
    # so they sat in /rynek-pierwotny/domy/ while listing flats.
    "7557": "mieszkanie",  # Wille Biskupin etap II — 49 live units, all mieszkanie
    "7293": "mieszkanie",  # Wille Biskupin etap I  — 31 live units, all mieszkanie
    "6703": "dom",      # Nowa Winnica etap I - szeregi 9-12
    "6001": "usluga",   # Przystań Królewiecka III - lokale usługowe
    # Found 2026-07-20 via full audit against live WP data: unit-type heuristic
    # gives a WRONG *non-empty* result for these (so the `if value:` write guard
    # doesn't protect them) and they were corrected in WP outside this pipeline —
    # without this entry the next scheduled sync silently reverts them.
    # Six investments where ExPro's raw unit type says "Mieszkanie" but the
    # per-unit classifier — which also reads building_type_id and the unit's own
    # shape — resolves every live unit to a house. resolve_projekt_typ() works
    # from the scrape dump and cannot see that verdict, so it would keep
    # answering "mieszkanie" and revert the investment on every run. Verified
    # 2026-07-29 against the live units, unanimous in each case.
    #
    # 7108 was previously pinned to "mieszkanie" here for the opposite reason;
    # its 9 live units are all houses, so the entry was inverted.
    "7108": "dom",      # Zaciszny Ołtaszyn IV      —  9 live units, all dom
    "7336": "dom",      # Zakątek Bliż              —  8 live units, all dom
    "5928": "dom",      # Rubinowa Park II          —  4 live units, all dom
    "7169": "dom",      # Osiedle pod Lasem etap IV —  4 live units, all dom
    "5264": "dom",      # Przystań Mędłów           —  3 live units, all dom
    "6743": "dom",      # Inwestycja Malinowa       —  2 live units, all dom
}

# Secondary signal from investment NAME, used only to correct the specific
# failure mode where ExPro mislabels every unit as "Mieszkanie" even though
# the investment is actually houses/commercial (see _PROJEKT_TYP_OVERRIDES
# above for known cases). Verified 2026-07-20 against all 180 live investment
# names: zero collisions — no investment currently classified "mieszkanie"
# contains any of these keywords, so this can only ever move a *future*
# investment away from an already-known-wrong "mieszkanie" guess.
_NAME_TYP_PATTERNS = [
    (["wille", "willa", " domy ", "domy ", "szereg", "bliźniak", "blizniak", "wolnostoj"], "dom"),
    # "lokal"/"lokale" stays as the required prefix on purpose. Bare stems like
    # "handlow" or "biurow" match Polish street names — Handlowa, Biurowa —
    # and would reclassify a residential development on its address alone.
    (["lokal usług", "lokale usług", "lokal użytk", "lokale użytk",
      "lokal biurow", "lokale biurow", "lokal handlow", "lokale handlow",
      "firma wykończ", "wykończeni", "zarządzanie najmem", "zarzadzanie najmem"], "usluga"),
]

def detect_projekt_typ_from_name(inv: dict) -> str:
    """Secondary signal: infer projekt_typ from investment name/description keywords."""
    name = (inv.get("name") or "").lower()
    if not name:
        return ""
    for patterns, val in _NAME_TYP_PATTERNS:
        if any(p in name for p in patterns):
            return val
    return ""


# ExPro also lists things that are not real, addressable real estate: pure
# service companies (firma wykończeniowa, zarządzanie najmem — 0 units, just
# a company profile) and manufacturer product catalogs (generic model/size
# names instead of real units at a real address). These should never get a
# published post on a real-estate site.
_EXCLUDED_NON_REALESTATE_IDS: set[str] = {
    "5520",  # Domy Modułowe - Wrocław — modular-home manufacturer catalog (unit
             # names are model codes like "HAV"/"DAL_A", not real addressed lots)
    "4844",  # Domy Modułowe — same manufacturer, same issue
}

def is_excluded_from_site(expro_id, inv: dict) -> bool:
    """True if this investment must never get a published inwestycja post —
    it isn't real, sellable/rentable real estate."""
    if str(expro_id) in _EXCLUDED_NON_REALESTATE_IDS:
        return True
    no_units = not inv.get("units_count") and not inv.get("units")
    no_building_type = not inv.get("building_type_id")
    if no_units and no_building_type:
        # Zero units AND no building_type_id = a service-company profile, not
        # a property listing. units_count==0 alone is NOT enough — real
        # pre-launch developments (units not listed yet) also have 0 units,
        # but ExPro still tags them with a real building_type_id (verified:
        # "Bulwar Północny", a genuine 145-flat pre-launch by Archicom, has
        # units_count=0 but building_type_id="1" — every confirmed pure
        # service-company profile has building_type_id=None).
        return True
    return False


def detect_projekt_typ(inv: dict) -> str:
    """Detect projekt_typ from unit types in ExPro data."""
    units = inv.get("units", [])
    counts: dict[str, int] = {}
    for u in units:
        t = (u.get("type") or u.get("Typ") or "").lower().strip()
        if t:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return ""
    dominant = max(counts, key=counts.get)
    for patterns, val in _TYP_PATTERNS:
        if any(p in dominant for p in patterns):
            return val
    return ""


def resolve_projekt_typ(expro_id, inv: dict) -> str:
    """Final projekt_typ resolution: override > unit-heuristic (corrected by name
    signal only in the known "everything mislabeled Mieszkanie" failure mode)."""
    override = _PROJEKT_TYP_OVERRIDES.get(str(expro_id))
    if override:
        return override
    heuristic = detect_projekt_typ(inv)
    # The name is consulted when the units said "mieszkanie" — ExPro's default
    # for everything, including offices — and equally when they said nothing at
    # all. An empty heuristic is what 13 commercial investments produce: their
    # units are "Lokal użytkowy", which no unit-type pattern covers, so the one
    # signal that does know what they are was never asked. They kept the right
    # value only because the bulk writer drops empty strings, which is luck
    # rather than logic.
    if heuristic in ("", "mieszkanie"):
        by_name = detect_projekt_typ_from_name(inv)
        if by_name:
            return by_name
    return heuristic


def normalize_units(units: list) -> list:
    """Normalize unit keys to Polish format expected by single-inwestycja.php template."""
    KEY_MAP = {
        "name":         "Nazwa",
        "status":       "Status",
        "type":         "Typ",
        "area_raw":     "Powierzchnia",
        "rooms":        "Pokoje",
        "floor":        "Piętro",
        "delivery":     "Termin oddania",
        "price_raw":    "Cena",
        "price_m2_raw": "Cena m2",
        "stage":        "Etap",
    }
    result = []
    for u in units:
        normalized = {}
        for eng_key, pl_key in KEY_MAP.items():
            val = u.get(eng_key) or u.get(pl_key, "")
            if val:
                normalized[pl_key] = val
        # Keep any keys already in Polish (e.g. from top5.json)
        for k, v in u.items():
            if k not in KEY_MAP and k not in normalized:
                normalized[k] = v
        result.append(normalized)
    return result


def build_standard_text(inv: dict) -> str:
    desc = inv.get("description", {})
    parts = []
    for key in ["udogodnienia", "przynaleznosci", "bezpieczenstwo", "garaz", "komunikacja", "odleglosc_centrum"]:
        val = desc.get(key, "").strip()
        if val:
            parts.append(val)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------

def find_existing_post(ssh: SSHClient, expro_id: str) -> Optional[int]:
    """The post this investment should be written to.

    Five expro_ids have two posts each — a published one carrying the units and
    an empty draft created beside it during the runner failures in June. Taking
    whichever row came back first meant taking the draft in all five cases, so
    every run wrote fresh ExPro data into a page nobody can see while the live
    one — 79 units in Legnicka Vita's case — was never updated. That is why its
    units had no coordinates although ExPro sends them.

    Published beats draft, then the lower ID, which is the older post. Both
    parts matter: the first is what makes it correct, the second is what makes
    it the same answer every run.
    """
    try:
        out = ssh.run_wp_cli(
            f"post list --post_type={shlex.quote(WP['post_type'])} "
            f"--post_status=any "
            f"--meta_key=expro_id --meta_value={shlex.quote(expro_id)} "
            f"--fields=ID,post_status --format=csv"
        )
        rows = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1]))
        if not rows:
            return None
        if len(rows) > 1:
            log(f"  NOTE: expro_id={expro_id} has {len(rows)} posts: "
                f"{', '.join(f'{i}({s})' for i, s in rows)}")
        rows.sort(key=lambda r: (r[1] != "publish", r[0]))
        return rows[0][0]
    except Exception as e:
        log(f"  find_existing_post error: {e}")
    return None


def get_all_posts_expro(ssh: SSHClient) -> list:
    """Return list of (post_id, expro_id) for all inwestycja posts. Single SSH call."""
    php = (
        "<?php\n"
        "$posts=get_posts(['post_type'=>'inwestycja','posts_per_page'=>-1,'fields'=>'ids','post_status'=>'publish']);\n"
        "foreach($posts as $id){\n"
        "    $eid=get_post_meta($id,'expro_id',true);\n"
        "    if($eid) echo $id.','.$eid.\"\\n\";\n"
        "}\n"
    )
    ssh.write_remote_file(php, '/tmp/esm_posts.php')
    out = ssh.run_wp_cli('eval-file /tmp/esm_posts.php')
    result = []
    for line in out.strip().splitlines():
        parts = line.strip().split(',', 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].strip():
            result.append((int(parts[0]), parts[1].strip()))
    return result


def get_existing_hash(ssh: SSHClient, post_id: int) -> str:
    try:
        out = ssh.run_wp_cli(f"post meta get {post_id} expro_id_hash")
        return out.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------
# An investment that carries an expro_id is rewritten by this script every
# night, so anything a human corrects on it survives exactly until the next run
# — which is why hand-filled pages were pointless before this existed. Some
# investments arrive from the open dane.gov.pl registry rather than a
# developer's feed (37 of 102 published ones): the registry sends price, area
# and unit count and nothing else, so completion date, rooms, street, heating
# and plot areas have to come from the developer by hand, and they have to
# survive.
#
# The post names the fields it owns in _realsy_manual_lock, a JSON array. Each
# entry is a meta key, `post_title`, `post_content`, or a `tax:<taxonomy>`
# name; a trailing `*` matches a prefix (`projekt_cecha_*`). Everything not
# listed keeps coming from ExPro, which is the point — locking the whole post
# would freeze prices and availability, and stale prices are worse than thin
# ones.
MANUAL_LOCK_META = "_realsy_manual_lock"


def get_manual_locks(ssh: SSHClient, post_id: int) -> set:
    """Field names on this post that a human owns and the sync must not write."""
    if not post_id:
        return set()
    try:
        raw = ssh.run_wp_cli(f"post meta get {post_id} {MANUAL_LOCK_META}", timeout=30).strip()
    except Exception:
        return set()          # no such meta — nothing is locked
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        # A comma-separated list is what someone typing this by hand in WP
        # Admin would leave behind; accept it rather than silently protect
        # nothing.
        entries = raw.split(",")
    return {str(e).strip() for e in entries if str(e).strip()}


def is_locked(locks: set, key: str) -> bool:
    if not locks:
        return False
    for pattern in locks:
        if pattern == key:
            return True
        if pattern.endswith("*") and key.startswith(pattern[:-1]):
            return True
    return False


def sync_investment(ssh: SSHClient, inv: dict) -> Tuple[str, Optional[int]]:
    """
    Returns ('created'|'updated'|'skipped'|'excluded'|'failed', post_id).
    """
    expro_id = inv.get("expro_id", "")
    name = inv.get("name", f"Inwestycja {expro_id}")
    current_hash = inv.get("scrape_hash", "")

    # check existing
    post_id = find_existing_post(ssh, expro_id)

    if is_excluded_from_site(expro_id, inv):
        if post_id:
            try:
                ssh.run_wp_cli(f"post update {post_id} --post_status=draft", timeout=30)
                log(f"  EXCLUDED (not real estate) — set to draft: {name} (WP ID: {post_id})")
            except Exception as e:
                log(f"  EXCLUDED but draft-update failed for {name} (WP ID: {post_id}): {e}")
        else:
            log(f"  EXCLUDED (not real estate) — no post created: {name}")
        return "excluded", post_id

    # Publication ban. ExPro shows "Zakaz publikacji" instead of "Zgoda na
    # publikację" when the developer refuses to have the investment published.
    # The REST API carries neither badge, so this pipeline could not see the
    # refusal and published 50 investments — 2446 units — against it, until one
    # developer phoned. The post and its meta stay; only its visibility goes.
    if inv.get("zakaz_publikacji"):
        if post_id:
            try:
                ssh.run_wp_cli(f"post update {post_id} --post_status=draft", timeout=30)
                ssh.run_wp_cli(f"post meta update {post_id} _expro_zakaz_publikacji 1", timeout=30)
                log(f"  ZAKAZ PUBLIKACJI — hidden: {name} (WP ID: {post_id})")
            except Exception as e:
                log(f"  ZAKAZ PUBLIKACJI but hiding failed for {name} (WP ID: {post_id}): {e}")
        else:
            log(f"  ZAKAZ PUBLIKACJI — no post created: {name}")
        return "excluded", post_id

    # Ban lifted: a post hidden by this rule, and only by this rule, comes back.
    # A post drafted for the other reason — gone from the feed — is left alone.
    if post_id:
        try:
            marked = ssh.run_wp_cli(
                f"post meta get {post_id} _expro_zakaz_publikacji", timeout=30).strip()
            if marked == "1":
                ssh.run_wp_cli(f"post update {post_id} --post_status=publish", timeout=30)
                ssh.run_wp_cli(f"post meta delete {post_id} _expro_zakaz_publikacji", timeout=30)
                log(f"  ZAKAZ LIFTED — republished: {name} (WP ID: {post_id})")
        except Exception:
            pass

    action = "update" if post_id else "create"

    # Fields a human owns on this post. A post being created has none.
    locks = get_manual_locks(ssh, post_id) if post_id else set()

    # change detection
    if post_id and current_hash:
        existing_hash = get_existing_hash(ssh, post_id)
        if existing_hash == current_hash:
            log(f"  SKIP (no changes): {name} (WP ID: {post_id})")
            return "skipped", post_id

    post_content = build_post_content(inv)
    safe_title = name.replace('"', '\\"')

    try:
        if action == "create":
            out = ssh.run_wp_cli(
                f"post create "
                f"--post_type={shlex.quote(WP['post_type'])} "
                f"--post_title={shlex.quote(name)} "
                f"--post_status={shlex.quote(WP['post_status'])} "
                f"--post_author={WP.get('post_author', 1)} "
                f"--porcelain",
                timeout=30,
            )
            post_id_raw = out.strip()
            if not post_id_raw.isdigit():
                raise RuntimeError(f"post create returned non-ID: {post_id_raw!r}")
            post_id = int(post_id_raw)
        elif not is_locked(locks, "post_title"):
            # update title
            ssh.run_wp_cli(
                f"post update {post_id} --post_title={shlex.quote(name)}",
                timeout=30,
            )

        # Update post content via remote file (can be long)
        if not is_locked(locks, "post_content"):
            update_meta_via_eval_or_update_post(ssh, post_id, post_content)

        # ── meta fields ──────────────────────────────────────────────────────
        city     = inv.get("city", "")
        street   = inv.get("street", "")
        district = inv.get("district", "")
        desc     = inv.get("description", {})

        # lokalizacja: "ul. Beli Bartoka, Wrocław" or just "Wrocław"
        lokalizacja_parts = [p for p in [street, city] if p]
        lokalizacja = ", ".join(lokalizacja_parts)

        # tagline: "Dzielnica · Miasto" — NIE pokazujemy nazwy dewelopera
        tagline_parts = [p for p in [district, city] if p]
        tagline = " · ".join(tagline_parts) if tagline_parts else city

        # clean delivery: "od I kwartał 2027 do I kwartał 2027" → "I kwartał 2027"
        delivery_raw = inv.get("delivery", "")
        m_del = re.match(r"od\s+(.+?)\s+do\s+\1$", delivery_raw.strip())
        delivery = m_del.group(1) if m_del else re.sub(r"^od\s+", "", delivery_raw)

        projekt_typ = resolve_projekt_typ(expro_id, inv)

        meta_fields: list[tuple[str, str]] = [
            ("expro_id",              expro_id),
            ("expro_url",             inv.get("expro_url", "")),
            ("expro_id_hash",         current_hash),
            ("expro_last_updated",    inv.get("last_updated_expro", "")),
            ("projekt_developer",     inv.get("developer", "")),
            ("projekt_lokalizacja",   lokalizacja),
            ("projekt_tagline",       tagline),
            ("projekt_cena_od",       format_price(inv)),
            ("projekt_cena_za_m2",    derive_price_m2(inv)),
            ("projekt_pow_mieszkalna", format_area(inv)),
            ("projekt_pokoje",        derive_rooms(inv)),
            ("projekt_termin_oddania", delivery),
            ("projekt_liczba_lokali", str(inv.get("units_count", "") or inv.get("units_available", ""))),
            ("projekt_status",        map_status(inv, delivery)),
            ("projekt_typ",           projekt_typ),
            # cechy 1–4 (individual keys for single-inwestycja.php)
            ("projekt_cecha_1_ikona", "dashicons-admin-home"),
            ("projekt_cecha_1_tytul", "Udogodnienia"),
            ("projekt_cecha_1_opis",  desc.get("udogodnienia", "")),
            ("projekt_cecha_2_ikona", "dashicons-shield"),
            ("projekt_cecha_2_tytul", "Bezpieczeństwo"),
            ("projekt_cecha_2_opis",  desc.get("bezpieczenstwo", "")),
            ("projekt_cecha_3_ikona", "dashicons-car"),
            ("projekt_cecha_3_tytul", "Komunikacja"),
            ("projekt_cecha_3_opis",  desc.get("komunikacja", "")),
            ("projekt_cecha_4_ikona", "dashicons-admin-home"),
            ("projekt_cecha_4_tytul", "Garaż / Parking"),
            ("projekt_cecha_4_opis",  desc.get("garaz", "")),
            # standard & odleglosci
            ("projekt_standard",      desc.get("przynaleznosci", "")),
            ("projekt_odleglosci",    desc.get("odleglosc_centrum", "")),
            # Coordinates come straight from ExPro (api_scraper stores them as
            # "latitude"/"longitude"). This used to read inv["lat"]/["lng"] — keys
            # that never exist — so it always wrote "" and, because the loop below
            # only writes truthy values, projekt_lat was left to a separate
            # geocoding phase that resolved the *street* (no house number) and gave
            # every address on a street one shared centroid (e.g. all "Krakowska"
            # investments landed 1.2 km off). ExPro's own lat/lng are per-building
            # and correct, so use them directly.
            ("projekt_lat",           str(inv.get("latitude", "")).strip()),
            ("projekt_lng",           str(inv.get("longitude", "")).strip()),
            # commission terms from ExPro "Zasady współpracy"
            ("projekt_komisja",       json.dumps(inv.get("zasady_wspolpracy", {}), ensure_ascii=False) if inv.get("zasady_wspolpracy") else ""),
            # …plus the rates as flat, queryable meta (the JSON blob above is not sortable)
            ("projekt_prowizja_standard",   komisja_rate(inv, "stawka_standard")),
            ("projekt_prowizja_vip",        komisja_rate(inv, "stawka_vip")),
            ("projekt_prowizja_termin_dni", komisja_rate(inv, "termin_wyplaty")),
            ("projekt_prowizja_garaz",      (inv.get("zasady_wspolpracy") or {}).get("garaz_w_prowizji", "")),
            # ── Fields the API always sent and nothing was storing ──────────
            # Measured over all 170 before adding these: build_year and
            # investment_type are empty on every one, so they get no key here.
            ("projekt_developer_id",   inv.get("developer_id", "")),
            # Direct contract with the developer, the open dane.gov.pl registry
            # or the VoxCrm API — which matters commercially, not technically.
            ("projekt_zrodlo",         inv.get("source_name", "")),
            ("projekt_liczba_condo",   inv.get("count_condo", "")),
            ("projekt_liczba_uslug",   inv.get("count_utility", "")),
            ("projekt_liczba_wszystkich", inv.get("count_all", "")),
            ("projekt_rating",         inv.get("rating", "")),
            ("projekt_waluta",         inv.get("currency", "")),
            # The far end of the completion range; projekt_termin_oddania
            # above keeps holding the start, which is what templates read.
            ("projekt_termin_do",      inv.get("delivery_to", "")),
        ]

        # Everything below goes up in one call instead of one per field.
        bulk: dict = {k: v for k, v in meta_fields if v}

        dev_url = inv.get("developer_url", "")
        if dev_url:
            bulk["projekt_subdomain_url"] = dev_url

        # extra fields from the investment detail page ("Więcej informacji")
        extra = inv.get("extra", {})
        ogrzewanie = extra.get("rodzaj_ogrzewania", "")
        extra_map = [
            ("expro_winda",          extra.get("winda", "")),
            ("expro_parking",        extra.get("miejsce_postojowe", "")),
            ("expro_komorki",        extra.get("komorki_lokatorskie", "")),
            ("expro_smart_home",     extra.get("smart_home", "")),
            ("expro_stacja_ev",      extra.get("stacja_ev", "")),
            ("expro_ochrona",        extra.get("rodzaje_ochrony", "")),
            ("expro_ogrzewanie",     ogrzewanie),
            ("projekt_ogrzewanie",   ogrzewanie),   # single-inwestycja.php reads this key
            ("expro_okna",           extra.get("rodzaj_okien", "")),
            ("expro_forma_wlasnosci", extra.get("forma_wlasnosci", "")),
            ("expro_pod_klucz",      extra.get("pod_klucz", "")),
            ("expro_wielkosc",       extra.get("wielkosc_projektu", "")),
            ("expro_parking_naziemne_cena",  extra.get("parking_naziemne_cena", "")),
            ("expro_parking_podziemne_cena", extra.get("parking_podziemne_cena", "")),
            ("expro_komorka_cena",   extra.get("komorka_cena", "")),
            ("expro_parking_obowiazkowy",    extra.get("parking_obowiazkowy", "")),
        ]
        bulk.update({k: v for k, v in extra_map if v})

        # Full investment JSON — page-inwestycja-mieszkania.php reads it for $extra
        bulk["expro_investment_json"] = json.dumps(inv, ensure_ascii=False)

        documents = inv.get("documents", [])
        if documents:
            bulk["expro_dokumenty_json"] = json.dumps(documents, ensure_ascii=False)

        # Units, normalised to the Polish keys the template expects
        bulk["expro_lokale_json"] = json.dumps(
            normalize_units(inv.get("units", [])), ensure_ascii=False
        )

        # Hand-held fields drop out here, right before the write, so every path
        # that fills `bulk` above is covered by one rule rather than each
        # remembering to check.
        if locks:
            kept = sorted(k for k in bulk if is_locked(locks, k))
            if kept:
                for k in kept:
                    del bulk[k]
                log(f"  manual lock — kept {len(kept)} field(s): {', '.join(kept)}")

        update_meta_bulk(ssh, post_id, bulk)

        # ── images ───────────────────────────────────────────────────────────
        images = inv.get("images", [])
        gallery_ids: list[int] = []
        for i, img_url in enumerate(images):
            att_id = import_image_to_wp(ssh, img_url, post_id)
            if att_id:
                gallery_ids.append(att_id)
                if i == 0 and not is_locked(locks, "_thumbnail_id"):
                    # set featured image
                    try:
                        ssh.run_wp_cli(f"post meta update {post_id} _thumbnail_id {att_id}")
                    except Exception as e:
                        log(f"  Featured image set failed: {e}")

        if gallery_ids and not is_locked(locks, "projekt_galeria"):
            gallery_str = ",".join(str(x) for x in gallery_ids)
            try:
                update_meta(ssh, post_id, "projekt_galeria", gallery_str)
            except Exception as e:
                log(f"  projekt_galeria update failed: {e}")

        result = "created" if action == "create" else "updated"
        log(f"  ✓ {result.capitalize()}: {name} (WP ID: {post_id})")
        return result, post_id

    except Exception as e:
        log(f"  ✗ FAILED: {name} — {e}")
        return "failed", post_id


def update_meta_via_eval_or_update_post(ssh: SSHClient, post_id: int, content: str):
    """Update post_content using WP-CLI post update with remote temp file workaround."""
    remote_tmp = f"/tmp/wp_content_{post_id}.html"
    try:
        ssh.write_remote_file(content, remote_tmp)
        php = (
            f"wp_update_post(array("
            f"'ID' => {post_id}, "
            f"'post_content' => file_get_contents('{remote_tmp}')"
            f"));"
        )
        ssh.run_wp_cli(f"eval {shlex.quote(php)}", timeout=30)
    except Exception as e:
        log(f"  post_content update failed (will continue): {e}")
    finally:
        ssh.remove_remote_file(remote_tmp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    data_path = DATA_FILE
    if not Path(data_path).exists():
        log(f"ERROR: Data file not found: {data_path}")
        log("Run 'python scraper.py' first, or 'python run.py scrape'")
        sys.exit(1)

    with open(data_path, encoding="utf-8") as f:
        investments: list[dict] = json.load(f)

    log(f"Loaded {len(investments)} investments from {data_path}")

    test_id = os.environ.get("EXPRO_TEST_INV_ID", "").strip()
    if test_id:
        test_ids = set(test_id.split(","))
        investments = [i for i in investments if str(i.get("expro_id", "")) in test_ids]
        log(f"TEST MODE: limiting to {len(investments)} investment(s): {test_id}")

    ssh = SSHClient()
    try:
        ssh._connect()
    except Exception as e:
        log(f"FATAL: SSH connection failed — {e}")
        sys.exit(1)

    # Verify WP-CLI is available
    try:
        ver = ssh.run_wp_cli("--version", timeout=15)
        log(f"WP-CLI version: {ver}")
    except Exception as e:
        log(f"FATAL: WP-CLI not available — {e}")
        ssh.close()
        sys.exit(1)

    created = updated = skipped = excluded = failed = 0

    for idx, inv in enumerate(investments, 1):
        log(f"[{idx}/{len(investments)}] {inv.get('name', inv.get('expro_id', '?'))}")
        try:
            result, _ = sync_investment(ssh, inv)
            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            elif result == "skipped":
                skipped += 1
            elif result == "excluded":
                excluded += 1
            else:
                failed += 1
        except Exception as e:
            log(f"  Unexpected error: {e}")
            failed += 1

        # small pause to avoid hammering the server
        time.sleep(0.5)

    ssh.close()
    log(
        f"\nSync complete: "
        f"{created} created, {updated} updated, "
        f"{skipped} skipped (no changes), {excluded} excluded (not real estate), "
        f"{failed} failed."
    )


if __name__ == "__main__":
    main()
