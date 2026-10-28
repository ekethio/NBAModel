import requests
import json
import os
import re
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

SEASON_START   = date(2026, 10, 1)
THRESHOLD      = 0.8    # seconds of time-to-shot shift, required in each of the last two games
MODELS         = {'A': 10, 'B': 6}   # model name -> baseline games before the last two
GAMES_FILE     = 'data/tempo_games.json'
PICKS_FILE     = 'data/picks.json'

BASE    = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
SUMMARY = 'https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
ET      = ZoneInfo('America/New_York')

def get(url, retries=4, delay=5):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  Retry {i+1} ({e}), waiting {delay}s...")
            time.sleep(delay)

def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def clock_secs(s):
    s = str(s or '0')
    if ':' in s:
        m, sec = s.split(':')
        return int(m) * 60 + float(sec)
    return float(s)

# ── Time to shot per team from one game's play-by-play ──────────────────────
def game_tts(summary):
    comp = summary['header']['competitions'][0]
    abbr = {c['team']['id']: c['team']['abbreviation'] for c in comp['competitors']}
    plays = sorted(summary.get('plays', []), key=lambda p: int(p.get('sequenceNumber', 0)))

    rows = []
    for p in plays:
        typ = (p.get('type', {}).get('text') or '').lower()
        txt = (p.get('text') or '').lower()
        rows.append({
            'per':  p.get('period', {}).get('number', 1),
            'rem':  clock_secs(p.get('clock', {}).get('displayValue')),
            'team': (p.get('team') or {}).get('id'),
            'typ':  typ, 'txt': txt,
            'shot': bool(p.get('shootingPlay')), 'made': bool(p.get('scoringPlay')),
            'away': int(p.get('awayScore') or 0), 'home': int(p.get('homeScore') or 0),
        })

    # Possession boundaries: (row index, seconds left, kind, team that ended it)
    bounds, last_miss = [], None
    for i, r in enumerate(rows):
        if i == 0 or r['per'] != rows[i - 1]['per']:
            bounds.append((i, 720.0 if r['per'] <= 4 else 300.0, 'start', None))
            last_miss = None
        ft = 'free throw' in r['typ'] or 'free throw' in r['txt']
        special = any(k in r['typ'] or k in r['txt'] for k in ('technical', 'flagrant', 'clear path'))
        if r['shot'] and not ft:
            if r['made']:
                bounds.append((i, r['rem'], 'made', r['team']))
            else:
                last_miss = r['rem']
        elif ft and not special:
            m = re.search(r'(\d) of (\d)', r['typ'] + ' ' + r['txt'])
            if m and m.group(1) == '1' and bounds and bounds[-1][2] == 'made' and bounds[-1][3] == r['team']:
                bounds.pop()                                   # and-one: possession ends on the free throw
            if m and m.group(1) == m.group(2):
                if r['made']:
                    bounds.append((i, r['rem'], 'made', r['team']))
                else:
                    last_miss = r['rem']
        elif 'rebound' in r['typ'] and 'defensive' in (r['typ'] + r['txt']):
            # Rebounds are timed at the miss so older and newer ESPN logging line up
            rem = last_miss if last_miss is not None else r['rem']
            bounds.append((i, rem, 'dreb', r['team']))
            last_miss = None
        elif ('turnover' in r['typ'] or 'turnover' in r['txt'] or r['typ'] in ('traveling', 'offensive charge')
              or r['typ'].startswith('offensive foul')):
            bounds.append((i, r['rem'], 'tov', r['team']))

    teams = list(abbr)
    out = {t: [] for t in teams}
    for k, (i, rem0, kind, who) in enumerate(bounds):
        # the event that ends this possession (often its shot) belongs to it
        end_i = min(bounds[k + 1][0] + 1, len(rows)) if k + 1 < len(bounds) else len(rows)
        per = rows[i]['per']
        if kind == 'dreb':
            offense = who
        elif kind in ('made', 'tov'):
            offense = next((t for t in teams if t != who), None)
        else:
            offense = None
        fga = next((rows[j] for j in range(i + 1, end_i)
                    if rows[j]['per'] == per and rows[j]['shot']
                    and 'free throw' not in rows[j]['typ'] and 'free throw' not in rows[j]['txt']), None)
        if fga is None or fga['team'] not in out:
            continue
        if offense is not None and fga['team'] != offense:
            continue
        margin = abs(rows[i]['home'] - rows[i]['away'])
        late = rem0 <= 35 or (per >= 4 and rem0 <= 120 and margin <= 8)
        blowout = per >= 4 and margin >= 20
        tts = rem0 - fga['rem']
        if late or blowout or not (0 <= tts <= 24):
            continue
        out[fga['team']].append(tts)

    return {abbr[t]: {'tts': round(sum(v) / len(v), 3), 'n': len(v)} for t, v in out.items() if len(v) >= 30}

# ── Scan the schedule ───────────────────────────────────────────────────────
games = load(GAMES_FILE, {})
picks = load(PICKS_FILE, {})
today = datetime.now(ET).date()

last_cached = max((date.fromisoformat(g['date']) for g in games.values()), default=SEASON_START)
cur = max(SEASON_START, last_cached - timedelta(days=3))
upcoming = []
print(f"Scanning scoreboard {cur} to {today}...")
while cur <= today:
    ds = cur.strftime('%Y%m%d')
    try:
        data = get(f'{BASE}/scoreboard?dates={ds}')
    except Exception as e:
        print(f"  Skipping {ds}: {e}")
        cur += timedelta(days=1)
        continue
    for ev in data.get('events', []):
        if ev.get('season', {}).get('type') != 2:
            continue
        comp = ev['competitions'][0]
        home = next(c for c in comp['competitors'] if c['homeAway'] == 'home')
        away = next(c for c in comp['competitors'] if c['homeAway'] == 'away')
        status = comp.get('status', ev.get('status', {})).get('type', {})
        info = {'date': cur.isoformat(), 'tip': ev.get('date', ''),
                'home': home['team']['abbreviation'], 'away': away['team']['abbreviation'],
                'home_score': int(home.get('score') or 0), 'away_score': int(away.get('score') or 0)}
        if status.get('completed'):
            g = games.get(ev['id'])
            if g is None or 'tts' not in g:
                try:
                    tts = game_tts(get(f"{SUMMARY}?event={ev['id']}"))
                    if len(tts) == 2:
                        info['tts'] = tts
                    else:
                        print(f"  {info['away']} @ {info['home']} {info['date']}: play-by-play incomplete")
                except Exception as e:
                    print(f"  Summary failed for {ev['id']}: {e}")
                games[ev['id']] = info
            else:
                g.update({k: info[k] for k in ('home_score', 'away_score')})
        elif cur == today and status.get('state') == 'pre':
            odds = (comp.get('odds') or [{}])[0]
            info['id'] = ev['id']
            info['line'] = odds.get('overUnder')
            upcoming.append(info)
    cur += timedelta(days=1)

with open(GAMES_FILE, 'w') as f:
    json.dump(games, f, separators=(',', ':'))
print(f"  {sum('tts' in g for g in games.values())} games with time to shot; {len(upcoming)} games still to play today")

# ── Shifts: each of the last two games vs the games before them ─────────────
log = {}
for gid, g in games.items():
    for team, v in g.get('tts', {}).items():
        log.setdefault(team, []).append((g['date'], gid, v['tts']))
for team in log:
    log[team].sort()

def shift_direction(team, n):
    hist = log.get(team, [])
    if len(hist) < n + 2:
        return 0, None
    base = sum(x[2] for x in hist[-n - 2:-2]) / n
    d1, d2 = hist[-1][2] - base, hist[-2][2] - base
    if d1 <= -THRESHOLD and d2 <= -THRESHOLD:
        return 1, (round(d1, 2), round(d2, 2))     # faster: over
    if d1 >= THRESHOLD and d2 >= THRESHOLD:
        return -1, (round(d1, 2), round(d2, 2))    # slower: under
    return 0, (round(d1, 2), round(d2, 2))

lg = [v['tts'] for g in games.values() for v in g.get('tts', {}).values()]
if lg:
    print(f"  League time to shot so far: {sum(lg) / len(lg):.2f} s over {len(lg)} team-games")

def grade(history):
    for p in history.values():
        g = games.get(p['id'])
        if g is None or p.get('result') or not (g['home_score'] or g['away_score']):
            continue
        total = g['home_score'] + g['away_score']
        p['final'] = total
        if p.get('line') is None:
            p['result'] = 'No line'
        elif total == p['line']:
            p['result'] = 'P'
        else:
            p['result'] = 'W' if (total > p['line']) == (p['side'] == 'Over') else 'L'

now = datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')
old = picks.get('models', {'A': {'history': picks.get('history', [])}})   # older files held one model
models = {}
for name, n in MODELS.items():
    ready = sum(len(v) >= n + 2 for v in log.values())
    print(f"Model {name}: teams with enough games: {ready} of {len(log)}")
    history = {p['id']: p for p in old.get(name, {}).get('history', [])}
    for g in upcoming:
        dh, sh = shift_direction(g['home'], n)
        da, sa = shift_direction(g['away'], n)
        if dh or da:
            print(f"  {g['away']} @ {g['home']}: {g['away']} {sa} {da:+d}, {g['home']} {sh} {dh:+d}")
        if dh * da < 0 or dh + da == 0:
            continue
        side = 'Over' if dh + da > 0 else 'Under'
        p = history.get(g['id'])
        if p is None:
            history[g['id']] = {'id': g['id'], 'date': g['date'], 'tip': g['tip'],
                                'matchup': f"{g['away']} @ {g['home']}", 'side': side,
                                'line': g['line'], 'posted': now}
        elif p.get('line') is None and g['line'] is not None:
            p['line'] = g['line']
    grade(history)
    hist = sorted(history.values(), key=lambda p: (p['date'], p['tip']), reverse=True)
    w = sum(p.get('result') == 'W' for p in hist)
    l = sum(p.get('result') == 'L' for p in hist)
    pu = sum(p.get('result') == 'P' for p in hist)
    models[name] = {'record': {'w': w, 'l': l, 'p': pu, 'units': round(w * 100 / 110 - l, 2)}, 'history': hist}
    print(f"  {sum(p['date'] == today.isoformat() for p in hist)} bets today, record {w}-{l}-{pu}")

with open(PICKS_FILE, 'w') as f:
    json.dump({'updatedAt': now, 'today': today.isoformat(), 'models': models}, f, indent=1)
print(f"Saved {PICKS_FILE}")
