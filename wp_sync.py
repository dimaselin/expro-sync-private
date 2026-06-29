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

def check_image_exists(ssh: SSHClient, image_url: str) -> Optional[int]:
    """Return attachment ID if image URL was already imported, else None."""
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


def import_image_to_wp(ssh: SSHClient, image_url: str, post_id: int) -> Optional[int]:
    """Import image URL into WP media library. Returns attachment ID or None."""
    # dedup check
    existing = check_image_exists(ssh, image_url)
    if existing:
        return existing

    try:
        wp_args = (
            f"media import {shlex.quote(image_url)} "
            f"--post_id={post_id} --porcelain"
        )
        out = ssh.run_wp_cli(wp_args, timeout=120)
        att_id = out.strip()
        if att_id.isdigit():
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


# ---------------------------------------------------------------------------
# Content builder
# ---------------------------------------------------------------------------

def build_post_content(inv: dict) -> str:
    desc = inv.get("description", {})
    parts = []

    city = inv.get("city", "")
    developer = inv.get("developer", "")
    if city or developer:
        intro = ", ".join(filter(None, [city, developer]))
        parts.append(f"<p>{intro}</p>")

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
    """Calculate average price per m² from units (Cena m2 / price_m2_raw)."""
    units = inv.get("units", [])
    vals: list[float] = []
    for u in units:
        raw = _uval(u, "Cena m2", "price_m2_raw")
        m = re.search(r"(\d[\d\s]*(?:[,.]\d+)?)\s*PLN", raw)
        if m:
            try:
                num = re.sub(r"\s", "", m.group(1)).replace(",", ".")
                vals.append(float(num))
            except ValueError:
                pass
    if not vals:
        return ""
    avg = round(sum(vals) / len(vals))
    return str(avg)


def map_status(inv: dict) -> str:
    available = inv.get("units_available", 0)
    if available > 0:
        return "dostepne"
    return "sprzedane"


_TYP_PATTERNS = [
    (["bliźniak", "blizniak"],                          "blizniak"),
    (["szeregówka", "szeregowka", "szereg"],             "szeregowka"),
    (["wolnostojący", "wolnostojacy", "wolnostoj"],      "wolnostojacy"),
    (["mieszkanie", "mieszkania", "apartament"],         "mieszkanie"),
    (["dom"],                                            "dom"),
]

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
    try:
        out = ssh.run_wp_cli(
            f"post list --post_type={shlex.quote(WP['post_type'])} "
            f"--meta_key=expro_id --meta_value={shlex.quote(expro_id)} "
            f"--fields=ID --format=csv"
        )
        lines = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "ID"]
        if lines and lines[0].isdigit():
            return int(lines[0])
    except Exception as e:
        log(f"  find_existing_post error: {e}")
    return None


def get_all_posts_expro(ssh: SSHClient) -> list:
    """Return list of (post_id, expro_id) for all inwestycja posts. Single SSH call."""
    out = ssh.run_wp_cli(
        r"eval \"\$posts = get_posts(['post_type'=>'inwestycja','posts_per_page'=>-1,'fields'=>'ids']);"
        r" foreach(\$posts as \$id){"
        r"   \$eid = get_post_meta(\$id,'expro_id',true);"
        r"   if(\$eid) echo \$id.','.\$eid.\"\n\";"
        r" }\""
    )
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


def sync_investment(ssh: SSHClient, inv: dict) -> Tuple[str, Optional[int]]:
    """
    Returns ('created'|'updated'|'skipped'|'failed', post_id).
    """
    expro_id = inv.get("expro_id", "")
    name = inv.get("name", f"Inwestycja {expro_id}")
    current_hash = inv.get("scrape_hash", "")

    # check existing
    post_id = find_existing_post(ssh, expro_id)
    action = "update" if post_id else "create"

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
        else:
            # update title
            ssh.run_wp_cli(
                f"post update {post_id} --post_title={shlex.quote(name)}",
                timeout=30,
            )

        # Update post content via remote file (can be long)
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

        projekt_typ = detect_projekt_typ(inv)

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
            ("projekt_status",        map_status(inv)),
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
            # coordinates (filled later by geocoding phase)
            ("projekt_lat",           str(inv.get("lat", ""))),
            ("projekt_lng",           str(inv.get("lng", ""))),
        ]

        for key, value in meta_fields:
            if value:
                try:
                    update_meta(ssh, post_id, key, value)
                except Exception as e:
                    log(f"  Meta update failed [{key}]: {e}")

        # developer URL — from ExPro (if not already set manually)
        dev_url = inv.get("developer_url", "")
        if dev_url:
            try:
                update_meta(ssh, post_id, "projekt_subdomain_url", dev_url)
            except Exception as e:
                log(f"  projekt_subdomain_url update failed: {e}")

        # extra fields from #collapseMoreInfo
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
        ]
        for key, value in extra_map:
            if value:
                try:
                    update_meta(ssh, post_id, key, value)
                except Exception as e:
                    log(f"  {key} update failed: {e}")

        # Full investment JSON — used by page-inwestycja-mieszkania.php for $extra params
        try:
            update_meta(ssh, post_id, "expro_investment_json", json.dumps(inv, ensure_ascii=False))
        except Exception as e:
            log(f"  expro_investment_json update failed: {e}")

        # documents from ExPro
        documents = inv.get("documents", [])
        if documents:
            docs_json = json.dumps(documents, ensure_ascii=False)
            try:
                update_meta(ssh, post_id, "expro_dokumenty_json", docs_json)
            except Exception as e:
                log(f"  expro_dokumenty_json update failed: {e}")

        # JSON meta via remote file — normalize to Polish keys expected by template
        units_json = json.dumps(normalize_units(inv.get("units", [])), ensure_ascii=False)
        try:
            update_meta(ssh, post_id, "expro_lokale_json", units_json)
        except Exception as e:
            log(f"  expro_lokale_json update failed: {e}")

        # ── images ───────────────────────────────────────────────────────────
        images = inv.get("images", [])
        gallery_ids: list[int] = []
        for i, img_url in enumerate(images):
            att_id = import_image_to_wp(ssh, img_url, post_id)
            if att_id:
                gallery_ids.append(att_id)
                if i == 0:
                    # set featured image
                    try:
                        ssh.run_wp_cli(f"post meta update {post_id} _thumbnail_id {att_id}")
                    except Exception as e:
                        log(f"  Featured image set failed: {e}")

        if gallery_ids:
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

    created = updated = skipped = failed = 0

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
        f"{skipped} skipped (no changes), {failed} failed."
    )


if __name__ == "__main__":
    main()
