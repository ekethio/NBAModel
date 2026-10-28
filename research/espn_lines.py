"""One-off: fetch ESPN opening and closing game totals for the games in espn_game_ids.csv.
usage: python espn_lines.py [limit]   (writes research/espn_lines.csv; a limit prints one raw odds item)"""
import csv
import json
import re
import sys
import time
import requests

CORE = 'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{0}/competitions/{0}/odds'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

def get(url):
    for i in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            time.sleep(3 * (i + 1))
    return None

def num(x):
    m = re.search(r'(\d{3}(?:\.\d)?)', str(x or ''))
    return float(m.group(1)) if m else None

def total_of(block):
    # a block like item['open'] / item['close']; the total sits under 'total' or as over/under lines
    if not isinstance(block, dict):
        return None
    for key in ('total', 'over', 'under'):
        v = block.get(key)
        if isinstance(v, dict):
            for k in ('value', 'line', 'alternateDisplayValue', 'displayValue', 'american'):
                n = num(v.get(k))
                if n:
                    return n
    return None

ids = [r['game_id'] for r in csv.DictReader(open('research/espn_game_ids.csv'))]
if limit:
    # sample games spread across all five seasons
    step = max(1, len(ids) // limit)
    ids = ids[step // 2::step][:limit]
rows = []
for k, gid in enumerate(ids):
    data = get(CORE.format(gid))
    items = (data or {}).get('items', [])
    if limit:
        keyed = [((it.get('provider') or {}).get('name'), sorted(k for k in it if k in ('open', 'close', 'current'))) for it in items]
        print(gid, keyed)
        withopen = [it for it in items if 'open' in it]
        if withopen and not getattr(sys, '_shown', False):
            print(json.dumps({k: withopen[0][k] for k in ('provider', 'overUnder', 'open', 'close', 'current') if k in withopen[0]}, indent=1)[:3000])
            sys._shown = True
    for it in items:
        rows.append({'game_id': gid, 'provider': (it.get('provider') or {}).get('name'),
                     'over_under': it.get('overUnder'),
                     'open_total': total_of(it.get('open')), 'close_total': total_of(it.get('close')),
                     'current_total': total_of(it.get('current'))})
    if not items:
        rows.append({'game_id': gid, 'provider': None, 'over_under': None,
                     'open_total': None, 'close_total': None, 'current_total': None})
    if k % 250 == 0:
        print(f'{k}/{len(ids)} done')
    time.sleep(0.15)

with open('research/espn_lines.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
have = [r for r in rows if r['open_total']]
print(f'{len(ids)} games, {len(rows)} provider rows, {len(have)} with an opening total')
