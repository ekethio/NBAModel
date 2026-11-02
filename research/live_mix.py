"""Live time to shot, raw and adjusted for how possessions started.

usage: python research/live_mix.py [YYYYMMDD] [TEAM ...]   (default: today ET, every game in progress or final)

Possessions are grouped by how they started: after a made basket or free throw, after a defensive
rebound, after a steal, after a dead-ball turnover. Each team's time to shot in each group is compared
with the league's for that group this season, small groups are shrunk toward the team's overall gap,
and the groups are recombined with league-wide weights, so a game full of steals doesn't read as fast
just because it had more transition chances. The league reference is cached in research/league_mix.json.

The clock stops at the first field-goal attempt, or, for possessions with no attempt, at a shooting foul or a
shot-clock violation, so possessions that never record a shot still count. Bonus fouls are left out.
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

src = open('nba_picks.py').read().split('# ── Scan the schedule')[0]
exec(src)

SHRINK = 5        # possessions of pull toward the team's overall gap, per start group
GROUPS = ('made', 'dreb', 'steal', 'deadtov')
CACHE = 'research/league_mix.json'


def ends_shot(r, extended):
    """The event that stops the time-to-shot clock: a field-goal attempt; with extended, also a shooting foul
    (a shot the box score doesn't count) and a shot-clock violation. Bonus fouls are left out: when they come
    says little about the offense and they pile up late in quarters."""
    ft = 'free throw' in r['typ'] or 'free throw' in r['txt']
    if r['shot'] and not ft:
        return True
    if not extended:
        return False
    return 'shooting foul' in r['typ'] or 'shot clock' in r['typ'] or 'shot clock' in r['txt']


def possessions(summary, extended=True):
    """[(team abbreviation, start group, tts)] for possessions that count. extended=True also ends the clock at
    shooting fouls and shot-clock violations; extended=False matches the bets page."""
    comp = summary['header']['competitions'][0]
    abbr = {c['team']['id']: c['team']['abbreviation'] for c in comp['competitors']}
    plays = sorted(summary.get('plays', []), key=lambda p: int(p.get('sequenceNumber', 0)))
    rows = []
    for p in plays:
        rows.append({'per': p.get('period', {}).get('number', 1), 'rem': clock_secs(p.get('clock', {}).get('displayValue')),
                     'team': (p.get('team') or {}).get('id'), 'typ': (p.get('type', {}).get('text') or '').lower(),
                     'txt': (p.get('text') or '').lower(), 'shot': bool(p.get('shootingPlay')), 'made': bool(p.get('scoringPlay')),
                     'away': int(p.get('awayScore') or 0), 'home': int(p.get('homeScore') or 0)})
    bounds, last_miss = [], None
    for i, r in enumerate(rows):
        if i == 0 or r['per'] != rows[i - 1]['per']:
            bounds.append((i, 720.0 if r['per'] <= 4 else 300.0, 'start', None, 'made'))
            last_miss = None
        ft = 'free throw' in r['typ'] or 'free throw' in r['txt']
        special = any(k in r['typ'] or k in r['txt'] for k in ('technical', 'flagrant', 'clear path'))
        if r['shot'] and not ft:
            if r['made']:
                bounds.append((i, r['rem'], 'made', r['team'], 'made'))
            else:
                last_miss = r['rem']
        elif ft and not special:
            m = re.search(r'(\d) of (\d)', r['typ'] + ' ' + r['txt'])
            if m and m.group(1) == '1' and bounds and bounds[-1][2] == 'made' and bounds[-1][3] == r['team']:
                bounds.pop()
            if m and m.group(1) == m.group(2):
                if r['made']:
                    bounds.append((i, r['rem'], 'made', r['team'], 'made'))
                else:
                    last_miss = r['rem']
        elif 'rebound' in r['typ'] and 'defensive' in (r['typ'] + r['txt']):
            bounds.append((i, last_miss if last_miss is not None else r['rem'], 'dreb', r['team'], 'dreb'))
            last_miss = None
        elif ('turnover' in r['typ'] or 'turnover' in r['txt'] or r['typ'] in ('traveling', 'offensive charge')
              or r['typ'].startswith('offensive foul')):
            live = 'steal' in r['txt'] or (i + 1 < len(rows) and 'steal' in rows[i + 1]['typ'])
            bounds.append((i, r['rem'], 'tov', r['team'], 'steal' if live else 'deadtov'))
    teams = list(abbr)
    out = []
    for k, (i, rem0, kind, who, group) in enumerate(bounds):
        end_i = min(bounds[k + 1][0] + 1, len(rows)) if k + 1 < len(bounds) else len(rows)
        per = rows[i]['per']
        offense = who if kind == 'dreb' else (next((t for t in teams if t != who), None) if kind in ('made', 'tov') else None)
        fga = next((rows[j] for j in range(i + 1, end_i) if rows[j]['per'] == per and ends_shot(rows[j], extended)), None)
        if fga is not None and 'shooting foul' in fga['typ']:   # fouls are logged to the defense
            fga = dict(fga, team=offense or next((t for t in teams if t != fga['team']), None))
        elif fga is not None and fga['team'] is None:         # shot-clock violations are often logged to no team
            fga = dict(fga, team=offense)
        if fga is None or fga['team'] not in abbr or (offense is not None and fga['team'] != offense):
            continue
        margin = abs(rows[i]['home'] - rows[i]['away'])
        if rem0 <= 35 or (per >= 4 and rem0 <= 120 and margin <= 8) or (per >= 4 and margin >= 20):
            continue
        tts = rem0 - fga['rem']
        if 0 <= tts <= 24:
            out.append((abbr[fga['team']], group, tts, per))
    return out


def league_reference():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    games = json.load(open('data/tempo_games.json'))
    allp = []
    for gid in games:
        try:
            allp += possessions(get(f'{SUMMARY}?event={gid}'))
        except Exception as e:
            print(f'  skipped {gid}: {e}')
    ref = {'all': sum(p[2] for p in allp) / len(allp), 'n': len(allp), 'games': len(games)}
    for g in GROUPS:
        xs = [p[2] for p in allp if p[1] == g]
        ref[g] = {'tts': sum(xs) / len(xs), 'share': len(xs) / len(allp)}
    json.dump(ref, open(CACHE, 'w'), indent=1)
    return ref


def adjusted(poss, ref):
    """raw mean, mix-adjusted mean and group detail for one team's possessions."""
    raw = sum(p[2] for p in poss) / len(poss)
    gap_all = raw - sum(ref[p[1]]['tts'] for p in poss) / len(poss)     # gap vs league, given this mix
    adj, detail = ref['all'], []
    for g in GROUPS:
        xs = [p[2] for p in poss if p[1] == g]
        gap = (sum(xs) / len(xs) - ref[g]['tts']) if xs else gap_all
        gap = (len(xs) * gap + SHRINK * gap_all) / (len(xs) + SHRINK)
        adj += ref[g]['share'] * gap
        detail.append(f"{g} {len(xs)}")
    return raw, adj, ', '.join(detail)


if __name__ == '__main__':
    args = sys.argv[1:]
    day = args.pop(0) if args and args[0].isdigit() else datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')
    want = {a.upper() for a in args}
    ref = league_reference()
    print(f"League reference ({ref['games']} games): all {ref['all']:.2f}s | " +
          ' | '.join(f"{g} {ref[g]['tts']:.2f}s ({ref[g]['share']:.0%})" for g in GROUPS))
    for ev in get(f'{BASE}/scoreboard?dates={day}').get('events', []):
        st = ev['competitions'][0]['status']['type']
        if want and not want & set(ev['shortName'].split(' @ ')):
            continue
        if st.get('state') == 'pre':
            continue
        poss = possessions(get(f"{SUMMARY}?event={ev['id']}"))
        score = ', '.join(f"{c['team']['abbreviation']} {c['score']}" for c in ev['competitions'][0]['competitors'])
        print(f"\n{ev['shortName']} | {st.get('shortDetail')} | {score}")
        for team in sorted({p[0] for p in poss}):
            tp = [p for p in poss if p[0] == team]
            raw, adj, detail = adjusted(tp, ref)
            print(f"  {team}: raw {raw:.2f}s | mix-adjusted {adj:.2f}s | {len(tp)} poss ({detail})")
