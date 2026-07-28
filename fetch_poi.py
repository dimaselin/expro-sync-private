#!/usr/bin/env python3
"""
Realsy — POI Fetcher via Nominatim API (runs on server via SSH)
Fetches nearby points of interest for inwestycja posts and saves to projekt_poi_json.

Uses Nominatim because the hosting server blocks Overpass API.
Nominatim is accessible from the server.

Usage:
  python3 fetch_poi.py --all              # all posts with lat/lng missing poi_json
  python3 fetch_poi.py --post-ids 32834,33861
  python3 fetch_poi.py --typ dom          # only dom posts
  python3 fetch_poi.py --force            # overwrite existing poi_json
"""
import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_sync import SSHClient, log

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from config import SSH, WP
    _SSH_HOST = SSH['host']; _SSH_PORT = SSH['port']
    _SSH_USER = SSH['username']; _SSH_PASS = SSH['password']
    _WP_PATH  = WP['path']
except (ImportError, KeyError):
    _SSH_HOST = '82.198.229.58'; _SSH_PORT = 65002
    _SSH_USER = 'u525644354';    _SSH_PASS = 'Strona2026!'
    _WP_PATH  = '/home/u525644354/domains/realsymanagement.pl/public_html'

# amenity_type, cat, icon
POI_RULES = [
    ["bus_stop",      "transport", ""],
    ["station",       "transport", ""],
    ["school",        "edu",       ""],
    ["kindergarten",  "edu",       ""],
    ["university",    "edu",       ""],
    ["supermarket",   "shop",      ""],
    ["convenience",   "shop",      ""],
    ["hospital",      "health",    ""],
    ["pharmacy",      "health",    ""],
    ["sports_centre", "sport",     ""],
    ["fitness_centre","sport",     ""],
    ["park",          "nature",    ""],
]

MAX_PER_CAT = 3
MAX_TOTAL   = 20
RADIUS_DEG  = 0.015  # ~1.5km


def _build_php(posts_dict: dict, force: bool = False) -> str:
    posts_json  = json.dumps(posts_dict)
    rules_json  = json.dumps(POI_RULES, ensure_ascii=False)
    force_str   = 'true' if force else 'false'
    return f"""<?php
set_time_limit(600);
$posts = json_decode('{posts_json}', true);
$rules = json_decode('{rules_json}', true);
$force = {force_str};

function nominatim_search($amenity, $lat, $lng) {{
  global $radius_deg;
  $vb = ($lng-{RADIUS_DEG}*1.5).','.($lat+{RADIUS_DEG}).','.($lng+{RADIUS_DEG}*1.5).','.($lat-{RADIUS_DEG});
  $url = 'https://nominatim.openstreetmap.org/search?' . http_build_query([
    'format'=>'json','amenity'=>$amenity,'bounded'=>'1',
    'viewbox'=>$vb,'limit'=>'10','countrycodes'=>'pl',
  ]);
  $ch = curl_init($url);
  curl_setopt_array($ch,[CURLOPT_RETURNTRANSFER=>1,CURLOPT_TIMEOUT=>12,
    CURLOPT_HTTPHEADER=>['User-Agent: RealsyManagement/1.0 POI-Bot']]);
  $r = curl_exec($ch); curl_close($ch);
  if(!$r) return [];
  $d = json_decode($r, true);
  return is_array($d) ? $d : [];
}}

function hav($lat1,$lon1,$lat2,$lon2) {{
  $R=6371000;
  $a=sin(deg2rad($lat2-$lat1)/2)**2+cos(deg2rad($lat1))*cos(deg2rad($lat2))*sin(deg2rad($lon2-$lon1)/2)**2;
  return (int)($R*2*atan2(sqrt($a),sqrt(1-$a)));
}}

foreach($posts as $id=>$coords) {{
  $lat=$coords[0]; $lng=$coords[1];
  if(!$force && get_post_meta($id,'projekt_poi_json',true)) {{
    echo "[$id] SKIP (already has poi_json)\\n"; continue;
  }}
  $poi=[]; $seen=[]; $cat_c=[];
  echo "[$id] lat=$lat lng=$lng\\n"; flush();
  foreach($rules as $r) {{
    $amenity=$r[0]; $cat=$r[1]; $ico=$r[2];
    if(($cat_c[$cat]??0)>={MAX_PER_CAT}) continue;
    $els = nominatim_search($amenity, $lat, $lng);
    sleep(1);
    foreach($els as $el) {{
      if(count($poi)>={MAX_TOTAL}) break;
      if(($cat_c[$cat]??0)>={MAX_PER_CAT}) break;
      $name=trim($el['display_name']??'');
      $name=explode(',', $name)[0];
      if(!$name||in_array($name,$seen)) continue;
      $elat=(float)($el['lat']??0); $elng=(float)($el['lon']??0);
      if(!$elat) continue;
      $dist=hav($lat,$lng,$elat,$elng);
      if($dist>2000) continue;
      $dlabel=$dist<1000?$dist.' m':round($dist/1000,1).' km';
      $poi[]=['cat'=>$cat,'ico'=>$ico,'name'=>$name,'dist'=>$dlabel,
              'lat'=>round($elat,7),'lng'=>round($elng,7)];
      $seen[]=$name; $cat_c[$cat]=($cat_c[$cat]??0)+1;
    }}
  }}
  echo "  Found: ".count($poi)."\\n";
  if($poi) {{
    update_post_meta($id,'projekt_poi_json',json_encode($poi,JSON_UNESCAPED_UNICODE));
    echo "  SAVED\\n";
  }} else {{ echo "  SKIP: no results\\n"; }}
  flush();
}}
echo "DONE\\n";
"""


def get_posts_needing_poi(ssh: SSHClient, typ_filter: str, force: bool) -> dict:
    force_str = 'true' if force else 'false'
    typ_cond = f'"projekt_typ","value"=>"{typ_filter}"' if typ_filter else '"projekt_typ","compare"=>"EXISTS"'
    php = f"""<?php
$all = get_posts(['post_type'=>'inwestycja','post_status'=>'publish','posts_per_page'=>-1,
  'meta_query'=>[[{typ_cond}]]]);
$result = [];
foreach($all as $p){{
    $lat = get_post_meta($p->ID,'projekt_lat',true);
    $lng = get_post_meta($p->ID,'projekt_lng',true);
    $poi = get_post_meta($p->ID,'projekt_poi_json',true);
    if($lat && $lng && ({force_str} || !$poi))
        echo $p->ID.'|'.$lat.'|'.$lng."\\n";
}}
"""
    sftp = ssh._client.open_sftp()
    sftp.putfo(io.BytesIO(php.encode()), '/tmp/poi_candidates.php')
    sftp.close()
    out = ssh.run_wp_cli('eval-file /tmp/poi_candidates.php')
    result = {}
    for line in out.strip().splitlines():
        parts = line.split('|')
        if len(parts) >= 3:
            try:
                result[str(int(parts[0]))] = [float(parts[1]), float(parts[2])]
            except ValueError:
                pass
    return result


def main():
    parser = argparse.ArgumentParser(description='Fetch POI via Nominatim (server-side) and save to WP')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all',      action='store_true', help='All posts with lat/lng missing poi_json')
    group.add_argument('--post-ids', type=str,            help='Comma-separated post IDs')
    parser.add_argument('--typ',     default='',          help='Filter: dom | mieszkanie')
    parser.add_argument('--force',   action='store_true', help='Overwrite existing poi_json')
    args = parser.parse_args()

    ssh = SSHClient()
    ssh._connect()

    if args.all:
        posts_dict = get_posts_needing_poi(ssh, args.typ, args.force)
        log(f'Posts needing POI: {len(posts_dict)}')
    else:
        ids = [int(x.strip()) for x in args.post_ids.split(',')]
        php_parts = ''.join(
            f'$lat=get_post_meta({i},"projekt_lat",true);$lng=get_post_meta({i},"projekt_lng",true);'
            f'if($lat&&$lng) echo "{i}|".$lat."|".$lng."\\n";'
            for i in ids
        )
        php = f'<?php {php_parts}'
        sftp = ssh._client.open_sftp()
        sftp.putfo(io.BytesIO(php.encode()), '/tmp/poi_manual.php')
        sftp.close()
        out = ssh.run_wp_cli('eval-file /tmp/poi_manual.php')
        posts_dict = {}
        for line in out.strip().splitlines():
            parts = line.split('|')
            if len(parts) >= 3:
                try:
                    posts_dict[parts[0]] = [float(parts[1]), float(parts[2])]
                except ValueError:
                    pass

    if not posts_dict:
        log('No posts to process.')
        ssh.close()
        return

    php_script = _build_php(posts_dict, args.force)
    sftp = ssh._client.open_sftp()
    sftp.putfo(io.BytesIO(php_script.encode('utf-8')), '/tmp/fetch_poi_nominatim.php')
    sftp.close()

    log(f'Running on server for {len(posts_dict)} posts (~{len(posts_dict)*12}s)...')
    timeout = len(posts_dict) * 15 + 60
    _, out, err = ssh._client.exec_command(
        f'cd {_WP_PATH} && wp eval-file /tmp/fetch_poi_nominatim.php 2>&1',
        timeout=timeout,
    )
    result = out.read().decode('utf-8', errors='replace')
    log(result)

    ssh.close()


if __name__ == '__main__':
    main()
