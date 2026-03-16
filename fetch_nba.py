from nba_api.stats.static import teams
from nba_api.stats.endpoints import leaguegamelog
import nba_api.library.http as nba_http
import pandas as pd
import json
import os
import time
from datetime import datetime

SEASON = '2024-25'
SEASON_TYPE = 'Regular Season'
POINTS_THRESHOLD = 228

# ── Patch browser-like headers onto whatever HTTP class this nba_api version uses ──
# stats.nba.com blocks requests that don't look like a real browser (e.g. GitHub Actions)
BROWSER_HEADERS = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'x-nba-stats-origin': 'stats',
    'x-nba-stats-token': 'true',
    'Connection': 'keep-alive',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com',
}
patched = False
for attr in dir(nba_http):
    obj = getattr(nba_http, attr)
    if isinstance(obj, type) and hasattr(obj, 'headers'):
        obj.headers = BROWSER_HEADERS
        print(f"Patched headers onto nba_http.{attr}")
        patched = True
if not patched:
    print("Warning: could not find HTTP class to patch — trying anyway")

def fetch_with_retry(fn, retries=5, delay=15):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            print(f"Attempt {attempt} failed: {e}. Retrying in {delay}s...")
            time.sleep(delay)

print("Fetching game log...")
gamelog = fetch_with_retry(lambda: leaguegamelog.LeagueGameLog(
    season=SEASON,
    season_type_all_star=SEASON_TYPE,
    timeout=60,
))
all_games = gamelog.get_data_frames()[0]
print(f"Got {len(all_games)} team-game rows")

# Calculate pace for each game
game_pace = {}
for game_id in all_games['GAME_ID'].unique():
    game_df = all_games[all_games['GAME_ID'] == game_id]
    if len(game_df) != 2:
        continue
    team1 = game_df.iloc[0]
    team2 = game_df.iloc[1]
    poss1 = team1['FGA'] + 0.44*team1['FTA'] - 1.07*((team1['OREB'])/(team1['OREB'] + team2['DREB'] + 1e-8))*(team1['FGA'] - team1['FGM']) + team1['TOV']
    poss2 = team2['FGA'] + 0.44*team2['FTA'] - 1.07*((team2['OREB'])/(team2['OREB'] + team1['DREB'] + 1e-8))*(team2['FGA'] - team2['FGM']) + team2['TOV']
    avg_poss = (poss1 + poss2) / 2
    game_minutes = team1['MIN'] / 5
    pace = (avg_poss / game_minutes) * 48
    game_pace[game_id] = round(float(pace), 1)

# Active teams
active_team_ids = all_games['TEAM_ID'].unique()
nba_teams = [t for t in teams.get_teams() if t['id'] in active_team_ids]

# Game totals
game_totals = all_games.groupby('GAME_ID')['PTS'].sum().to_dict()

# Opponent lookup
game_opponent = {}
for game_id in all_games['GAME_ID'].unique():
    game_df = all_games[all_games['GAME_ID'] == game_id]
    if len(game_df) != 2:
        continue
    ids = game_df['TEAM_ID'].tolist()
    game_opponent[(game_id, ids[0])] = ids[1]
    game_opponent[(game_id, ids[1])] = ids[0]

# League averages
all_game_ids = all_games['GAME_ID'].unique()
league_avg_points_global = float(pd.Series([game_totals[g] for g in all_game_ids if g in game_totals]).mean())
league_avg_pace_global   = float(pd.Series(list(game_pace.values())).mean())

# Team season averages
team_season_avg      = {}
team_season_avg_pace = {}
for team in nba_teams:
    team_id    = team['id']
    team_games = all_games[all_games['TEAM_ID'] == team_id]
    combined_points = team_games['GAME_ID'].map(game_totals)
    combined_pace   = team_games['GAME_ID'].map(game_pace)
    team_season_avg[team_id]      = float(combined_points.mean()) if not combined_points.empty else league_avg_points_global
    team_season_avg_pace[team_id] = float(combined_pace.mean())   if not combined_pace.empty   else league_avg_pace_global

def adjusted_avg(game_ids, team_id, game_map, team_avg_map, league_avg, n):
    recent = list(game_ids)[:n] if len(game_ids) >= n else None
    if recent is None:
        return None
    adj_values = []
    for gid in recent:
        actual  = game_map.get(gid, league_avg)
        opp_id  = game_opponent.get((gid, team_id))
        opp_avg = team_avg_map.get(opp_id, league_avg) if opp_id else league_avg
        if pd.isna(opp_avg):
            opp_avg = league_avg
        adj_values.append(float(actual) + (league_avg - float(opp_avg)))
    return round(float(pd.Series(adj_values).mean()), 1)

def safe_mean(series, min_len=0):
    if len(series) >= min_len and not series.empty:
        return round(float(series.mean()), 1)
    return None

# Build data
points_data = []
pace_data   = []

for team in nba_teams:
    team_id   = team['id']
    team_name = team['full_name']
    team_games = all_games[all_games['TEAM_ID'] == team_id].sort_values('GAME_DATE', ascending=False)
    team_games = team_games.copy()
    team_games['IS_HOME'] = team_games['MATCHUP'].str.contains(' vs. ')
    game_ids_recent = team_games['GAME_ID'].tolist()

    combined_points = team_games['GAME_ID'].map(game_totals)
    home_points     = team_games[team_games['IS_HOME']]['GAME_ID'].map(game_totals)
    away_points     = team_games[~team_games['IS_HOME']]['GAME_ID'].map(game_totals)
    combined_pace_s = team_games['GAME_ID'].map(game_pace)
    home_pace_s     = team_games[team_games['IS_HOME']]['GAME_ID'].map(game_pace)
    away_pace_s     = team_games[~team_games['IS_HOME']]['GAME_ID'].map(game_pace)

    adj_l7_pts  = adjusted_avg(game_ids_recent, team_id, game_totals, team_season_avg,      league_avg_points_global, 7)
    adj_l3_pts  = adjusted_avg(game_ids_recent, team_id, game_totals, team_season_avg,      league_avg_points_global, 3)
    adj_l7_pace = adjusted_avg(game_ids_recent, team_id, game_pace,   team_season_avg_pace, league_avg_pace_global,   7)
    adj_l3_pace = adjusted_avg(game_ids_recent, team_id, game_pace,   team_season_avg_pace, league_avg_pace_global,   3)

    pd_row = {
        'team':     team_name,
        'avgPts':   safe_mean(combined_points),
        'homePts':  safe_mean(home_points),
        'awayPts':  safe_mean(away_points),
        'l20Pts':   safe_mean(combined_points.head(20), 20),
        'l14Pts':   safe_mean(combined_points.head(14), 14),
        'l7Pts':    safe_mean(combined_points.head(7),  7),
        'adjL7Pts': adj_l7_pts,
        'l3Pts':    safe_mean(combined_points.head(3),  3),
        'adjL3Pts': adj_l3_pts,
        'over228':  int((combined_points >= POINTS_THRESHOLD).sum()),
        'games':    len(team_games),
    }
    pd_row['ptsScore'] = round(
        (pd_row['avgPts'] or 0)*0.45 + (pd_row['l20Pts'] or 0)*0.3 +
        (pd_row['l14Pts'] or 0)*0.15 + (pd_row['adjL7Pts'] or 0)*0.1, 1)
    points_data.append(pd_row)

    pc_row = {
        'team':      team_name,
        'avgPace':   safe_mean(combined_pace_s),
        'homePace':  safe_mean(home_pace_s),
        'awayPace':  safe_mean(away_pace_s),
        'l20Pace':   safe_mean(combined_pace_s.head(20), 20),
        'l14Pace':   safe_mean(combined_pace_s.head(14), 14),
        'l7Pace':    safe_mean(combined_pace_s.head(7),  7),
        'adjL7Pace': adj_l7_pace,
        'l3Pace':    safe_mean(combined_pace_s.head(3),  3),
        'adjL3Pace': adj_l3_pace,
        'games':     len(team_games),
    }
    pc_row['paceScore'] = round(
        (pc_row['avgPace'] or 0)*0.45 + (pc_row['l20Pace'] or 0)*0.3 +
        (pc_row['l14Pace'] or 0)*0.15 + (pc_row['adjL7Pace'] or 0)*0.1, 1)
    pace_data.append(pc_row)

# Per-team last-7 detail rows
detail_data = {}
for team in nba_teams:
    team_id    = team['id']
    team_name  = team['full_name']
    team_games = all_games[all_games['TEAM_ID'] == team_id].sort_values('GAME_DATE', ascending=False)
    last7      = team_games.head(7)
    rows = []
    for _, row in last7.iterrows():
        gid          = row['GAME_ID']
        opp_id       = game_opponent.get((gid, team_id))
        actual_total = float(game_totals.get(gid, 0))
        opp_pts_avg  = float(team_season_avg.get(opp_id, league_avg_points_global)) if opp_id else league_avg_points_global
        adj_total    = round(actual_total + (league_avg_points_global - opp_pts_avg), 1)
        actual_pace  = float(game_pace.get(gid, 0))
        opp_pace_avg = float(team_season_avg_pace.get(opp_id, league_avg_pace_global)) if opp_id else league_avg_pace_global
        adj_pace     = round(actual_pace + (league_avg_pace_global - opp_pace_avg), 1)
        rows.append({
            'date':     row['GAME_DATE'],
            'matchup':  row['MATCHUP'],
            'wl':       row['WL'],
            'pts':      int(row['PTS']),
            'total':    actual_total,
            'oppAvg':   round(opp_pts_avg, 1),
            'adjTotal': adj_total,
            'pace':     actual_pace,
            'oppPace':  round(opp_pace_avg, 1),
            'adjPace':  adj_pace,
        })
    detail_data[team_name] = rows

points_data.sort(key=lambda x: x['ptsScore'], reverse=True)
pace_data.sort(key=lambda x: x['paceScore'], reverse=True)

output = {
    'updatedAt':       datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
    'season':          SEASON,
    'leagueAvgPoints': round(league_avg_points_global, 1),
    'leagueAvgPace':   round(league_avg_pace_global, 1),
    'pointsThreshold': POINTS_THRESHOLD,
    'pointsData':      points_data,
    'paceData':        pace_data,
    'detailData':      detail_data,
}

os.makedirs('data', exist_ok=True)
with open('data/nba_stats.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"Done. Wrote data/nba_stats.json ({len(points_data)} teams)")
print(f"League avg pts: {league_avg_points_global:.1f}  |  League avg pace: {league_avg_pace_global:.1f}")
