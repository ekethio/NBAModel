import requests
import json
import os
import time
from datetime import datetime, date, timedelta

SEASON            = '2025-26'
POINTS_THRESHOLD  = 228
OUTPUT_FILE       = 'data/nba_stats.json'

# Season dates — 2025-26 starts Oct 2025
SEASON_START = date(2025, 10, 21)
SEASON_END   = date(2026, 4, 15)   # end of regular season (update if needed)

BASE    = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
SUMMARY = 'https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

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

# ── 1. Collect all completed regular-season game IDs + scores ─────────────────
print("Scanning scoreboard for completed games...")
all_game_stubs = []  # {id, date, home_id, home_name, home_abbr, away_id, ...}
today = date.today()
end   = min(SEASON_END, today)
cur   = SEASON_START

while cur <= end:
    ds = cur.strftime('%Y%m%d')
    try:
        data   = get(f'{BASE}/scoreboard?dates={ds}')
        events = data.get('events', [])
        for ev in events:
            if ev.get('season', {}).get('type') != 2:   # 2 = regular season
                continue
            if not ev.get('status', {}).get('type', {}).get('completed'):
                continue
            comps = ev.get('competitions', [{}])[0]
            competitors = comps.get('competitors', [])
            if len(competitors) != 2:
                continue
            home = next((c for c in competitors if c['homeAway'] == 'home'), None)
            away = next((c for c in competitors if c['homeAway'] == 'away'), None)
            if not home or not away:
                continue
            hs, as_ = int(home.get('score', 0)), int(away.get('score', 0))
            if hs == 0 and as_ == 0:
                continue
            all_game_stubs.append({
                'id':         ev['id'],
                'date':       ev.get('date', '')[:10],
                'home_id':    home['team']['id'],
                'home_name':  home['team']['displayName'],
                'home_abbr':  home['team']['abbreviation'],
                'home_score': hs,
                'away_id':    away['team']['id'],
                'away_name':  away['team']['displayName'],
                'away_abbr':  away['team']['abbreviation'],
                'away_score': as_,
            })
    except Exception as e:
        print(f"  Skipping {ds}: {e}")
    cur += timedelta(days=1)

print(f"  Found {len(all_game_stubs)} completed games")
if not all_game_stubs:
    raise RuntimeError("No games found — check SEASON_START date")

# ── 2. Fetch box score per game to get FGA/FGM/FTA/OREB/DREB/TOV/MIN ─────────
# ESPN summary endpoint returns these in boxscore.teams[].statistics
# Stat names (from ESPN JSON): fieldGoalsMade-fieldGoalsAttempted,
#   freeThrowsMade-freeThrowsAttempted, offensiveRebounds, defensiveRebounds,
#   turnovers, technicalFouls (ignore), minutes

def parse_stat(stats_list, name):
    """Find a stat by name from ESPN's statistics array, return float."""
    for s in stats_list:
        if s.get('name') == name:
            val = s.get('displayValue', '0')
            # Some are "made-attempted" format like "33-82"
            if '-' in str(val):
                return float(val.split('-')[0]), float(val.split('-')[1])
            try:
                return float(val)
            except:
                return 0.0
    return 0.0

print("Fetching box scores for pace calculation...")
game_pace   = {}   # game_id -> pace
game_totals = {}   # game_id -> total pts
game_mins   = {}   # game_id -> game minutes (for reference)

BATCH = 5  # small delay every N games to be polite
for i, g in enumerate(all_game_stubs):
    gid = g['id']
    game_totals[gid] = g['home_score'] + g['away_score']

    try:
        data   = get(f'{SUMMARY}?event={gid}')
        teams  = data.get('boxscore', {}).get('teams', [])

        if len(teams) < 2:
            # Fallback: estimate pace from total pts if box score unavailable
            game_pace[gid] = round(game_totals[gid] * 0.444, 1)
            continue

        # Parse both teams' box score stats
        td = []
        for t in teams:
            stats = t.get('statistics', [])
            fgm_fga = parse_stat(stats, 'fieldGoalsMade-fieldGoalsAttempted')
            ftm_fta = parse_stat(stats, 'freeThrowsMade-freeThrowsAttempted')
            fgm = fgm_fga[0] if isinstance(fgm_fga, tuple) else 0
            fga = fgm_fga[1] if isinstance(fgm_fga, tuple) else 0
            ftm = ftm_fta[0] if isinstance(ftm_fta, tuple) else 0
            fta = ftm_fta[1] if isinstance(ftm_fta, tuple) else 0
            oreb = parse_stat(stats, 'offensiveRebounds')
            dreb = parse_stat(stats, 'defensiveRebounds')
            tov  = parse_stat(stats, 'turnovers')
            # Minutes: ESPN returns total team minutes e.g. "240" or "265" (OT)
            raw_min = parse_stat(stats, 'minutes')
            # Sometimes it's a string like "265:00" — just take the number before colon
            td.append({'fga': fga, 'fgm': fgm, 'fta': fta, 'oreb': oreb, 'dreb': dreb, 'tov': tov, 'min': raw_min})

        t1, t2 = td[0], td[1]

        # Your exact possession formula:
        # poss = FGA + 0.44*FTA - 1.07*(OREB/(OREB+opp_DREB+ε))*(FGA-FGM) + TOV
        eps = 1e-8
        poss1 = t1['fga'] + 0.44*t1['fta'] - 1.07*(t1['oreb']/(t1['oreb']+t2['dreb']+eps))*(t1['fga']-t1['fgm']) + t1['tov']
        poss2 = t2['fga'] + 0.44*t2['fta'] - 1.07*(t2['oreb']/(t2['oreb']+t1['dreb']+eps))*(t2['fga']-t2['fgm']) + t2['tov']
        avg_poss = (poss1 + poss2) / 2

        # Game minutes: use team1 minutes / 5 (same as your original)
        # ESPN gives total team minutes (5 players * 48 min = 240 for regulation)
        raw = t1['min']
        if isinstance(raw, str) and ':' in raw:
            parts = raw.split(':')
            game_min = float(parts[0]) + float(parts[1])/60
        else:
            game_min = float(raw) if raw else 240.0
        game_min = game_min / 5  # convert team-minutes to game-minutes

        if game_min > 0:
            pace = (avg_poss / game_min) * 48
        else:
            pace = avg_poss * 2  # fallback

        game_pace[gid] = round(float(pace), 1)

    except Exception as e:
        print(f"  Box score failed for {gid}: {e} — estimating pace")
        game_pace[gid] = round(game_totals[gid] * 0.444, 1)

    if (i + 1) % BATCH == 0:
        print(f"  Processed {i+1}/{len(all_game_stubs)} games...")
        time.sleep(0.5)

print(f"  Pace calculated for {len(game_pace)} games")

# ── 3. Build per-team row lookup ──────────────────────────────────────────────
game_opponent = {}
team_game_rows = {}

for g in all_game_stubs:
    gid = g['id']
    for (my_id, my_name, my_abbr, my_score, opp_id, opp_abbr, opp_score, is_home) in [
        (g['home_id'], g['home_name'], g['home_abbr'], g['home_score'],
         g['away_id'], g['away_abbr'], g['away_score'], True),
        (g['away_id'], g['away_name'], g['away_abbr'], g['away_score'],
         g['home_id'], g['home_abbr'], g['home_score'], False),
    ]:
        game_opponent[(gid, my_id)] = opp_id
        if my_id not in team_game_rows:
            team_game_rows[my_id] = []
        matchup = f"{my_abbr} vs. {opp_abbr}" if is_home else f"{my_abbr} @ {opp_abbr}"
        wl = 'W' if my_score > opp_score else 'L'
        team_game_rows[my_id].append({
            'game_id': gid, 'date': g['date'], 'pts': my_score,
            'opp_id': opp_id, 'matchup': matchup, 'wl': wl, 'is_home': is_home,
        })

for tid in team_game_rows:
    team_game_rows[tid].sort(key=lambda x: x['date'], reverse=True)

# Active team list
team_set = {}
for g in all_game_stubs:
    team_set[g['home_id']] = g['home_name']
    team_set[g['away_id']] = g['away_name']
active_teams = [{'id': tid, 'name': name} for tid, name in sorted(team_set.items(), key=lambda x: x[1])]

# ── 4. League & team averages ─────────────────────────────────────────────────
all_totals = list(game_totals.values())
all_paces  = list(game_pace.values())
league_avg_points = round(sum(all_totals)/len(all_totals), 1)
league_avg_pace   = round(sum(all_paces)/len(all_paces),   1)
print(f"  League avg pts: {league_avg_points}  |  League avg pace: {league_avg_pace}")

team_season_avg_pts  = {}
team_season_avg_pace = {}
for t in active_teams:
    tid   = t['id']
    rows  = team_game_rows.get(tid, [])
    gids  = [r['game_id'] for r in rows]
    pts   = [game_totals[g] for g in gids if g in game_totals]
    paces = [game_pace[g]   for g in gids if g in game_pace]
    team_season_avg_pts[tid]  = round(sum(pts)/len(pts),     1) if pts   else league_avg_points
    team_season_avg_pace[tid] = round(sum(paces)/len(paces), 1) if paces else league_avg_pace

# ── 5. Helpers ────────────────────────────────────────────────────────────────
def safe_avg(values, min_len=0):
    if len(values) < max(min_len, 1):
        return None
    return round(sum(values)/len(values), 1)

def adj_avg(gids, team_id, val_map, avg_map, league_avg, n):
    recent = gids[:n]
    if len(recent) < n:
        return None
    adj = []
    for gid in recent:
        actual  = val_map.get(gid, league_avg)
        opp_id  = game_opponent.get((gid, team_id))
        opp_avg = avg_map.get(opp_id, league_avg) if opp_id else league_avg
        adj.append(actual + (league_avg - opp_avg))
    return round(sum(adj)/len(adj), 1)

# ── 6. Build tables ───────────────────────────────────────────────────────────
print("Computing team stats...")
points_data = []
pace_data   = []

for team in active_teams:
    tid   = team['id']
    tname = team['name']
    rows  = team_game_rows.get(tid, [])
    gids  = [r['game_id'] for r in rows]

    all_pts  = [game_totals[g] for g in gids if g in game_totals]
    all_pcs  = [game_pace[g]   for g in gids if g in game_pace]
    home_pts = [game_totals[r['game_id']] for r in rows if r['is_home']     and r['game_id'] in game_totals]
    away_pts = [game_totals[r['game_id']] for r in rows if not r['is_home'] and r['game_id'] in game_totals]
    home_pcs = [game_pace[r['game_id']]   for r in rows if r['is_home']     and r['game_id'] in game_pace]
    away_pcs = [game_pace[r['game_id']]   for r in rows if not r['is_home'] and r['game_id'] in game_pace]

    pd_row = {
        'team':     tname,
        'avgPts':   safe_avg(all_pts),
        'homePts':  safe_avg(home_pts),
        'awayPts':  safe_avg(away_pts),
        'l20Pts':   safe_avg(all_pts[:20], 20),
        'l14Pts':   safe_avg(all_pts[:14], 14),
        'l7Pts':    safe_avg(all_pts[:7],  7),
        'adjL7Pts': adj_avg(gids, tid, game_totals, team_season_avg_pts, league_avg_points, 7),
        'l3Pts':    safe_avg(all_pts[:3],  3),
        'adjL3Pts': adj_avg(gids, tid, game_totals, team_season_avg_pts, league_avg_points, 3),
        'over228':  sum(1 for p in all_pts if p >= POINTS_THRESHOLD),
        'games':    len(rows),
    }
    pd_row['ptsScore'] = round(
        (pd_row['avgPts']   or 0)*0.45 + (pd_row['l20Pts']  or 0)*0.30 +
        (pd_row['l14Pts']   or 0)*0.15 + (pd_row['adjL7Pts'] or 0)*0.10, 1)
    points_data.append(pd_row)

    pc_row = {
        'team':      tname,
        'avgPace':   safe_avg(all_pcs),
        'homePace':  safe_avg(home_pcs),
        'awayPace':  safe_avg(away_pcs),
        'l20Pace':   safe_avg(all_pcs[:20], 20),
        'l14Pace':   safe_avg(all_pcs[:14], 14),
        'l7Pace':    safe_avg(all_pcs[:7],  7),
        'adjL7Pace': adj_avg(gids, tid, game_pace, team_season_avg_pace, league_avg_pace, 7),
        'l3Pace':    safe_avg(all_pcs[:3],  3),
        'adjL3Pace': adj_avg(gids, tid, game_pace, team_season_avg_pace, league_avg_pace, 3),
        'games':     len(rows),
    }
    pc_row['paceScore'] = round(
        (pc_row['avgPace']   or 0)*0.45 + (pc_row['l20Pace']  or 0)*0.30 +
        (pc_row['l14Pace']   or 0)*0.15 + (pc_row['adjL7Pace'] or 0)*0.10, 1)
    pace_data.append(pc_row)

# ── 7. Last-7 detail per team ─────────────────────────────────────────────────
detail_data = {}
for team in active_teams:
    tid   = team['id']
    tname = team['name']
    rows  = team_game_rows.get(tid, [])[:7]
    detail_rows = []
    for r in rows:
        gid          = r['game_id']
        opp_id       = r['opp_id']
        actual_total = float(game_totals.get(gid, 0))
        opp_pts_avg  = float(team_season_avg_pts.get(opp_id, league_avg_points))
        adj_total    = round(actual_total + (league_avg_points - opp_pts_avg), 1)
        actual_pace  = float(game_pace.get(gid, 0))
        opp_pace_avg = float(team_season_avg_pace.get(opp_id, league_avg_pace))
        adj_pace_val = round(actual_pace + (league_avg_pace - opp_pace_avg), 1)
        detail_rows.append({
            'date':     r['date'],
            'matchup':  r['matchup'],
            'wl':       r['wl'],
            'pts':      r['pts'],
            'total':    actual_total,
            'oppAvg':   round(opp_pts_avg, 1),
            'adjTotal': adj_total,
            'pace':     actual_pace,
            'oppPace':  round(opp_pace_avg, 1),
            'adjPace':  adj_pace_val,
        })
    detail_data[tname] = detail_rows

# ── 8. Sort & write ───────────────────────────────────────────────────────────
points_data.sort(key=lambda x: x['ptsScore'], reverse=True)
pace_data.sort(key=lambda x: x['paceScore'],  reverse=True)

output = {
    'updatedAt':       datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
    'season':          SEASON,
    'leagueAvgPoints': league_avg_points,
    'leagueAvgPace':   league_avg_pace,
    'pointsThreshold': POINTS_THRESHOLD,
    'totalGames':      len(all_game_stubs),
    'pointsData':      points_data,
    'paceData':        pace_data,
    'detailData':      detail_data,
}

os.makedirs('data', exist_ok=True)
with open(OUTPUT_FILE, 'w') as f:
    json.dump(output, f, indent=2)

print(f"\nDone! {OUTPUT_FILE} written")
print(f"  Teams:           {len(points_data)}")
print(f"  Games:           {len(all_game_stubs)}")
print(f"  League avg pts:  {league_avg_points}")
print(f"  League avg pace: {league_avg_pace}")
