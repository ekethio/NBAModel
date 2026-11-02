"""Live time to shot for today's game involving a team (abbreviation in research/live_request.txt)."""
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# reuse the bets page's possession logic, without its 30-possession minimum
src = open('nba_picks.py').read().split('# ── Scan the schedule')[0].replace('if len(v) >= 30', 'if len(v) >= 1')
exec(src)

team = open('research/live_request.txt').read().strip().upper()
day = datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')
board = get(f'{BASE}/scoreboard?dates={day}')
ev = next((e for e in board.get('events', []) if team in e.get('shortName', '')), None)
if ev is None:
    sys.exit(f'No game today for {team}')
summ = get(f"{SUMMARY}?event={ev['id']}")
status = ev['competitions'][0]['status']
print(f"{ev['shortName']}  |  {status['type'].get('shortDetail')}")
comp = summ['header']['competitions'][0]
print('Score:', ', '.join(f"{c['team']['abbreviation']} {c.get('score', '?')}" for c in comp['competitors']))
odds = (ev['competitions'][0].get('odds') or [{}])[0]
print('Total on ESPN:', odds.get('overUnder'))

# per quarter and overall, using the same rules as the bets page
plays = summ.get('plays', [])
periods = sorted({p.get('period', {}).get('number', 1) for p in plays})
for upto in periods:
    part = dict(summ, plays=[p for p in plays if p.get('period', {}).get('number', 1) <= upto])
    res = game_tts(part)
    print(f"Through Q{upto}: " + '  '.join(f"{t} {v['tts']:.2f}s ({v['n']} poss)" for t, v in res.items()))

# season context from the bets page's game log
games = json.load(open('data/tempo_games.json'))
for t in [c['team']['abbreviation'] for c in comp['competitors']]:
    xs = sorted((g['date'], g['tts'][t]['tts']) for g in games.values() if t in g.get('tts', {}))
    if xs:
        avg = sum(x[1] for x in xs) / len(xs)
        print(f"{t} season so far: {avg:.2f}s over {len(xs)} games; last two: {', '.join(f'{x[1]:.2f}' for x in xs[-2:])}")
