"""Context for a tempo-rule trigger: half-by-half shifts in the two trigger games and lineup changes vs the baseline.

usage: python research/trigger_check.py TEAM [TEAM ...] [--base 10]

For each team: the baseline is the BASE games before its last two (same as the bets page, Model A = 10, B = 6).
Prints each trigger game's time to shot vs the baseline, split by half (second halves compared with baseline second
halves, since they always run slower), then rotation changes: players averaging 20+ minutes in the baseline who
missed both trigger games, and players under 10 who played 20+ in both.

Backtest (2024-26, 301 bets) for reading the output: neither check is a filter.
  - Halves: all 4 halves shifted 53.5%; 3 of 4 or fewer 64.1%. A shift that shows in every half is no better a bet.
  - Lineup: shifts with a lineup change still in place carry forward more (median 45% vs 16%), but bet 56.2%
    vs 60.0% with no change; the market prices injuries. Unexplained shifts are fine to bet.
"""
import json
import statistics as st
import sys
from collections import defaultdict

exec(open('research/live_mix.py').read().split("if __name__ == '__main__':")[0])

ALIAS = {'GSW': 'GS', 'NOP': 'NO', 'NYK': 'NY', 'SAS': 'SA', 'UTA': 'UTAH', 'WAS': 'WSH'}


def team_game(gid, team):
    s = get(f'{SUMMARY}?event={gid}')
    ps = [p for p in possessions(s, extended=False) if ALIAS.get(p[0], p[0]) == team]
    mins = {}
    for t in s['boxscore']['players']:
        if ALIAS.get(t['team']['abbreviation'], t['team']['abbreviation']) != team:
            continue
        st0 = t['statistics'][0]
        k = st0['names'].index('MIN')
        for a in st0['athletes']:
            try:
                m = int(float(a['stats'][k])) if a.get('stats') else 0
            except (ValueError, IndexError):
                m = 0
            if m > 0:
                mins[a['athlete'].get('displayName') or a['athlete'].get('shortName')] = m
    half = lambda lo, hi: [p[2] for p in ps if lo <= p[3] <= hi]
    h1, h2 = half(1, 2), half(3, 4)
    return {'tts': st.mean(p[2] for p in ps), 'h1': st.mean(h1) if len(h1) >= 12 else None,
            'h2': st.mean(h2) if len(h2) >= 12 else None, 'mins': mins}


def check(team, base_n):
    games = json.load(open('data/tempo_games.json'))
    mine = sorted(((g['date'], gid, g) for gid, g in games.items() if team in g.get('tts', {})))
    if len(mine) < base_n + 2:
        print(f'{team}: only {len(mine)} games, need {base_n + 2}')
        return
    rows = [(d, gid, g, team_game(gid, team)) for d, gid, g in mine[-base_n - 2:]]
    base, trig = rows[:-2], rows[-2:]
    b = st.mean(r[3]['tts'] for r in base)
    bh = {h: st.mean(x for x in (r[3][h] for r in base) if x is not None) for h in ('h1', 'h2')}
    print(f"\n{team}: baseline {b:.2f} s over {base_n} games (1st halves {bh['h1']:.2f}, 2nd halves {bh['h2']:.2f})")
    dirs = []
    for d, gid, g, x in trig:
        opp = g['away'] if g['home'] == team else g['home']
        us, them = (g['home_score'], g['away_score']) if g['home'] == team else (g['away_score'], g['home_score'])
        sh = [x[h] - bh[h] if x[h] is not None else None for h in ('h1', 'h2')]
        dirs.append(x['tts'] - b)
        f = lambda v: '  n/a' if v is None else f'{v:+.2f}'
        print(f"  {d} vs {opp} ({'W' if us > them else 'L'} {us}-{them}): game {x['tts'] - b:+.2f} s | "
              f"1st half {f(sh[0])} | 2nd half {f(sh[1])}")
    direction = 1 if all(v >= 0.8 for v in dirs) else -1 if all(v <= -0.8 for v in dirs) else 0
    if direction:
        halves = [v for _, _, _, x in trig for v in (x['h1'] and x['h1'] - bh['h1'], x['h2'] and x['h2'] - bh['h2']) if v is not None]
        n = sum(v * direction >= 0.4 for v in halves)
        print(f"  Trigger: {'slower (under)' if direction > 0 else 'faster (over)'}; {n} of {len(halves)} halves shifted 0.4+ s that way")
    else:
        print('  No trigger (both games need 0.8+ s the same way)')
    bm = defaultdict(float)
    for r in base:
        for p, m in r[3]['mins'].items():
            bm[p] += m / len(base)
    t1, t2 = trig[0][3]['mins'], trig[1][3]['mins']
    out = [f'{p} ({bm[p]:.0f} mpg)' for p in sorted(bm, key=lambda p: -bm[p]) if bm[p] >= 20 and not t1.get(p) and not t2.get(p)]
    new = [f'{p} ({t1[p]}, {t2[p]} min; {bm.get(p, 0):.0f} before)' for p in t1 if bm.get(p, 0) < 10 and t1.get(p, 0) >= 20 and t2.get(p, 0) >= 20]
    big = [f'{p} {bm.get(p, 0):.0f} -> {(t1.get(p, 0) + t2.get(p, 0)) / 2:.0f}' for p in set(bm) | set(t1) | set(t2)
           if abs((t1.get(p, 0) + t2.get(p, 0)) / 2 - bm.get(p, 0)) >= 10 and f'{p} (' not in ' '.join(out + new)]
    print('  Out both trigger games: ' + (', '.join(out) or 'none'))
    print('  New 20+ min both games: ' + (', '.join(new) or 'none'))
    print('  Other minutes swings of 10+: ' + (', '.join(sorted(big)) or 'none'))


if __name__ == '__main__':
    args = sys.argv[1:]
    n = 10
    if '--base' in args:
        i = args.index('--base')
        n = int(args[i + 1])
        del args[i:i + 2]
    for t in args:
        check(t.upper(), n)
