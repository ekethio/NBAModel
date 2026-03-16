import requests
import json
import os
import time
from datetime import datetime

SEASON = '2024-25'
POINTS_THRESHOLD = 228
OUTPUT_FILE = 'data/nba_stats.json'

# ESPN scoreboard API - fast, no auth, no bot blocking
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def get(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  Retry {i+1}: {e}")
            time.sleep(3)

# ── 1. Get all teams ──────────────────────────────────────────────────────────
print("Fetching teams...")
data = get(f'{BASE}/teams?limit=40')
teams_raw = data['sports'][0]['leagues'][0]['teams']
nba_teams = [{'id': t['team']['id'], 'name': t['team']['displayName'], 'abbr': t['team']['abbreviation']} for t in teams_raw]
team_by_id = {t['id']: t for t in nba_teams}
print(f"  {len(nba_teams)} teams")

# ── 2. Fetch all regular season games (ESPN scoreboard by week) ───────────────
# ESPN scoreboard supports dates — fetch completed games for full 2024-25 season
# Season runs roughly Oct 2024 – Apr 2025
print("Fetching scoreboard data...")

all_game_rows = []  # each entry: {game_id, date, home_id, away_id, home_score, away_score}

# Fetch using the season/seasontype endpoint which returns all games
# Use the team schedule endpoint per team to get full box score access
# More reliable: use the scoreboard endpoint date-by-date in bulk

from datetime import date, timedelta

start_date = date(2024, 10, 22)
end_date   = date(2025, 4, 15)   # end of regular season

current = start_date
total_games = 0
while current <= end_date:
    date_str = current.strftime('%Y%m%d')
    try:
        data = get(f'{BASE}/scoreboard?dates={date_str}')
        events = data.get('events', [])
        for event in events:
            # Only completed regular season games
            status = event.get('status', {}).get('type', {}).get('completed', False)
            season_type = event.get('season', {}).get('type', 2)  # 2 = regular season
            if not status or season_type != 2:
                continue
            comps = event.get('competitions', [{}])[0]
            competitors = comps.get('competitors', [])
            if len(competitors) != 2:
                continue
            home = next((c for c in competitors if c['homeAway'] == 'home'), None)
            away = next((c for c in competitors if c['homeAway'] == 'away'), None)
            if not home or not away:
                continue
            home_score = int(home.get('score', 0))
            away_score = int(away.get('score', 0))
            if home_score == 0 and away_score == 0:
                continue

            game_date = event.get('date', '')[:10]
            all_game_rows.append({
                'game_id':    event['id'],
                'date':       game_date,
                'home_id':    home['team']['id'],
                'home_name':  home['team']['displayName'],
                'home_abbr':  home['team']['abbreviation'],
                'away_id':    away['team']['id'],
                'away_name':  away['team']['displayName'],
                'away_abbr':  away['team']['abbreviation'],
                'home_score': home_score,
                'away_score': away_score,
                'total':      home_score + away_score,
            })
            total_games += 1
    except Exception as e:
        print(f"  Skipping {date_str}: {e}")

    current += timedelta(days=1)
    # Small delay every 30 days to be polite
    if (current - start_date).days % 30 == 0:
        time.sleep(0.5)

print(f"  {total_games} completed games found")

if total_games == 0:
    raise RuntimeError("No games fetched — check date range or ESPN API availability")

# ── 3. Build lookup structures ────────────────────────────────────────────────
game_totals  = {g['game_id']: g['total'] for g in all_game_rows}
game_opponent = {}  # (game_id, team_id) -> opp_team_id
game_dates   = {g['game_id']: g['date'] for g in all_game_rows}

# Build per-team row list
team_game_rows = {}  # team_id -> list of {game_id, date, pts, opp_id, matchup, wl, is_home}
for g in all_game_rows:
    for (my_id, my_score, my_abbr, opp_id, opp_abbr, is_home) in [
        (g['home_id'], g['home_score'], g['home_abbr'], g['away_id'], g['away_abbr'], True),
        (g['away_id'], g['away_score'], g['away_abbr'], g['home_id'], g['home_abbr'], False),
    ]:
        game_opponent[(g['game_id'], my_id)] = opp_id
        if my_id not in team_game_rows:
            team_game_rows[my_id] = []
        matchup = f"{my_abbr} vs. {opp_abbr}" if is_home else f"{my_abbr} @ {opp_abbr}"
        wl = 'W' if my_score > (g['away_score'] if is_home else g['home_score']) else 'L'
        team_game_rows[my_id].append({
            'game_id': g['game_id'],
            'date':    g['date'],
            'pts':     my_score,
            'opp_id':  opp_id,
            'matchup': matchup,
            'wl':      wl,
            'is_home': is_home,
        })

# Sort each team's games by date descending (most recent first)
for tid in team_game_rows:
    team_game_rows[tid].sort(key=lambda x: x['date'], reverse=True)

# Only keep teams that actually played
active_teams = [t for t in nba_teams if t['id'] in team_game_rows]

# ── 4. Pace: estimate from totals (ESPN doesn't expose possession data) ───────
# Using the same empirical approach: pace ≈ total_pts * 0.455
# (calibrated so ~220 pts ≈ pace 100, matching NBA averages)
game_pace = {gid: round(total * 0.455, 1) for gid, total in game_totals.items()}

# ── 5. League & team season averages ─────────────────────────────────────────
all_totals = list(game_totals.values())
all_paces  = list(game_pace.values())
league_avg_points = round(sum(all_totals) / len(all_totals), 1)
league_avg_pace   = round(sum(all_paces)  / len(all_paces),  1)

team_season_avg_pts  = {}
team_season_avg_pace = {}
for team in active_teams:
    tid   = team['id']
    rows  = team_game_rows.get(tid, [])
    gids  = [r['game_id'] for r in rows]
    pts   = [game_totals[g] for g in gids if g in game_totals]
    paces = [game_pace[g]   for g in gids if g in game_pace]
    team_season_avg_pts[tid]  = round(sum(pts)/len(pts),     1) if pts   else league_avg_points
    team_season_avg_pace[tid] = round(sum(paces)/len(paces), 1) if paces else league_avg_pace

# ── 6. Helper functions ───────────────────────────────────────────────────────
def safe_avg(values, min_len=0):
    if len(values) < max(min_len, 1):
        return None
    return round(sum(values) / len(values), 1)

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
    return round(sum(adj) / len(adj), 1)

# ── 7. Build points & pace tables ────────────────────────────────────────────
print("Computing stats...")
points_data = []
pace_data   = []

for team in active_teams:
    tid   = team['id']
    tname = team['name']
    rows  = team_game_rows.get(tid, [])
    gids  = [r['game_id'] for r in rows]

    all_pts  = [game_totals[g] for g in gids if g in game_totals]
    all_pcs  = [game_pace[g]   for g in gids if g in game_pace]
    home_pts = [game_totals[r['game_id']] for r in rows if r['is_home']  and r['game_id'] in game_totals]
    away_pts = [game_totals[r['game_id']] for r in rows if not r['is_home'] and r['game_id'] in game_totals]

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
        (pd_row['avgPts']   or 0) * 0.45 +
        (pd_row['l20Pts']   or 0) * 0.30 +
        (pd_row['l14Pts']   or 0) * 0.15 +
        (pd_row['adjL7Pts'] or 0) * 0.10, 1)
    points_data.append(pd_row)

    pc_row = {
        'team':      tname,
        'avgPace':   safe_avg(all_pcs),
        'homePace':  safe_avg([game_pace[r['game_id']] for r in rows if r['is_home']  and r['game_id'] in game_pace]),
        'awayPace':  safe_avg([game_pace[r['game_id']] for r in rows if not r['is_home'] and r['game_id'] in game_pace]),
        'l20Pace':   safe_avg(all_pcs[:20], 20),
        'l14Pace':   safe_avg(all_pcs[:14], 14),
        'l7Pace':    safe_avg(all_pcs[:7],  7),
        'adjL7Pace': adj_avg(gids, tid, game_pace, team_season_avg_pace, league_avg_pace, 7),
        'l3Pace':    safe_avg(all_pcs[:3],  3),
        'adjL3Pace': adj_avg(gids, tid, game_pace, team_season_avg_pace, league_avg_pace, 3),
        'games':     len(rows),
    }
    pc_row['paceScore'] = round(
        (pc_row['avgPace']   or 0) * 0.45 +
        (pc_row['l20Pace']   or 0) * 0.30 +
        (pc_row['l14Pace']   or 0) * 0.15 +
        (pc_row['adjL7Pace'] or 0) * 0.10, 1)
    pace_data.append(pc_row)

# ── 8. Last-7 detail per team ─────────────────────────────────────────────────
detail_data = {}
for team in active_teams:
    tid   = team['id']
    tname = team['name']
    rows  = team_game_rows.get(tid, [])[:7]
    detail_rows = []
    for r in rows:
        gid          = r['game_id']
        opp_id       = r['opp_id']
        actual_total = game_totals.get(gid, 0)
        opp_pts_avg  = team_season_avg_pts.get(opp_id, league_avg_points)
        adj_total    = round(actual_total + (league_avg_points - opp_pts_avg), 1)
        actual_pace  = game_pace.get(gid, 0)
        opp_pace_avg = team_season_avg_pace.get(opp_id, league_avg_pace)
        adj_pace_val = round(actual_pace + (league_avg_pace - opp_pace_avg), 1)
        detail_rows.append({
            'date':     r['date'],
            'matchup':  r['matchup'],
            'wl':       r['wl'],
            'pts':      r['pts'],
            'total':    float(actual_total),
            'oppAvg':   round(float(opp_pts_avg), 1),
            'adjTotal': adj_total,
            'pace':     float(actual_pace),
            'oppPace':  round(float(opp_pace_avg), 1),
            'adjPace':  adj_pace_val,
        })
    detail_data[tname] = detail_rows

# ── 9. Sort & write ───────────────────────────────────────────────────────────
points_data.sort(key=lambda x: x['ptsScore'], reverse=True)
pace_data.sort(key=lambda x: x['paceScore'], reverse=True)

output = {
    'updatedAt':       datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
    'season':          SEASON,
    'leagueAvgPoints': league_avg_points,
    'leagueAvgPace':   league_avg_pace,
    'pointsThreshold': POINTS_THRESHOLD,
    'totalGames':      total_games,
    'pointsData':      points_data,
    'paceData':        pace_data,
    'detailData':      detail_data,
}

os.makedirs('data', exist_ok=True)
with open(OUTPUT_FILE, 'w') as f:
    json.dump(output, f, indent=2)

print(f"\nDone! Wrote {OUTPUT_FILE}")
print(f"  Teams: {len(points_data)}")
print(f"  Games: {total_games}")
print(f"  League avg pts:  {league_avg_points}")
print(f"  League avg pace: {league_avg_pace}")
