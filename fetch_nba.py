#!/usr/bin/env python3
"""
fetch_nba.py — run by GitHub Actions daily
Writes data/nba_stats.json with ratings + fouls/FTA data
pip install nba_api pandas requests
"""
import json, os, time
from datetime import datetime, timezone

try:
    import pandas as pd
    from nba_api.stats.static import teams
    from nba_api.stats.endpoints import leaguegamelog
    NBA_API = True
except ImportError:
    NBA_API = False

SEASON      = '2025-26'
SEASON_TYPE = 'Regular Season'
OUT_FILE    = 'data/nba_stats.json'
os.makedirs('data', exist_ok=True)

def retry(fn, n=3):
    for i in range(n):
        try: return fn()
        except Exception as e:
            if i==n-1: raise
            time.sleep(4*(i+1))

def calc_raw(df, tid, ng=None):
    tg = df[df['TEAM_ID']==tid] if ng is None else df[df['TEAM_ID']==tid].head(ng)
    if tg.empty: return {'ortg':0,'drtg':0,'games':0}
    tp=tf=ta=0.0
    for _,tr in tg.iterrows():
        g=df[df['GAME_ID']==tr['GAME_ID']]
        if len(g)!=2: continue
        o=g[g['TEAM_ID']!=tid].iloc[0]
        d=tr['OREB']+o['DREB']
        p=tr['FGA']+0.44*tr['FTA']-(1.07*(tr['OREB']/d if d>0 else 0))*(tr['FGA']-tr['FGM'])+tr['TOV']
        if p<=0: continue
        tp+=p; tf+=tr['PTS']; ta+=o['PTS']
    if tp==0: return {'ortg':0,'drtg':0,'games':len(tg)}
    return {'ortg':round(tf/tp*100,1),'drtg':round(ta/tp*100,1),'games':len(tg)}

def calc(df, tid, sr, lo, ld, ng=None):
    tg = df[df['TEAM_ID']==tid] if ng is None else df[df['TEAM_ID']==tid].head(ng)
    if tg.empty: return {'ortg':0,'drtg':0,'adj_ortg':0,'adj_drtg':0,'pace':0,'games':0,'net':0,'adj_net':0}
    tp=tf=ta=sod=soo=tm=0.0; v=0
    for _,tr in tg.iterrows():
        g=df[df['GAME_ID']==tr['GAME_ID']]
        if len(g)!=2: continue
        o=g[g['TEAM_ID']!=tid].iloc[0]
        d=tr['OREB']+o['DREB']
        p=tr['FGA']+0.44*tr['FTA']-(1.07*(tr['OREB']/d if d>0 else 0))*(tr['FGA']-tr['FGM'])+tr['TOV']
        if p<=0: continue
        oid=int(o['TEAM_ID'])
        sod+=sr.get(oid,{'drtg':ld})['drtg']; soo+=sr.get(oid,{'ortg':lo})['ortg']
        tp+=p; tf+=tr['PTS']; ta+=o['PTS']; tm+=tr['MIN']; v+=1
    if tp==0 or v==0 or tm==0: return {'ortg':0,'drtg':0,'adj_ortg':0,'adj_drtg':0,'pace':0,'games':len(tg),'net':0,'adj_net':0}
    ortg=round(tf/tp*100,1); drtg=round(ta/tp*100,1)
    aod=sod/v; aoo=soo/v
    adj_o=round(ortg*(ld/aod) if aod else ortg,1)
    adj_d=round(drtg*(lo/aoo) if aoo else drtg,1)
    pace=round((tp/(tm/5))*48,1)
    return {'ortg':ortg,'drtg':drtg,'adj_ortg':adj_o,'adj_drtg':adj_d,
            'pace':pace,'games':len(tg),'net':round(ortg-drtg,1),'adj_net':round(adj_o-adj_d,1)}

def build_fouls(df):
    gs=(df.groupby('GAME_ID').agg(pf=('PF','sum'),fta=('FTA','sum'),dt=('GAME_DATE','first'))
        .reset_index().sort_values('dt').reset_index(drop=True))
    stretches=[]; fouls=gs['pf'].tolist(); fta=gs['fta'].tolist()
    for i in range(0,len(fouls),30):
        cf=fouls[i:i+30]; ct=fta[i:i+30]; n=len(cf)
        sf=sum(cf); st=sum(ct)
        stretches.append({'stretch':f"{i+1}-{i+n}",'games':n,
            'fouls_per_game':round(sf/n,2),'fta_per_game':round(st/n,2),
            'fta_foul_ratio':round(st/sf,3) if sf>0 else 0})
    games=[{'game_id':str(r['GAME_ID']),'date':str(r['dt'])[:10],
            'total_pf':int(r['pf']),'total_fta':int(r['fta'])}
           for _,r in gs.iterrows()]
    return stretches, games

def main():
    if not NBA_API:
        json.dump({'error':'nba_api not installed','updated':'','ratings':{},'fouls':{'stretches':[],'games':[]}},
                  open(OUT_FILE,'w'))
        return

    print(f"Fetching {SEASON}...")
    df = retry(lambda: leaguegamelog.LeagueGameLog(
        season=SEASON, season_type_all_star=SEASON_TYPE,
        player_or_team_abbreviation='T').get_data_frames()[0])
    nba_teams = retry(lambda: teams.get_teams())
    td = {t['id']:t['full_name'] for t in nba_teams}

    df['GAME_DATE'] = pd.to_datetime(df['GAME_DATE'])
    df = df.sort_values('GAME_DATE',ascending=False).reset_index(drop=True)
    df['MIN'] = pd.to_numeric(df['MIN'],errors='coerce').fillna(240)
    print(f"  {len(df)} rows")

    sr = {}
    for tid in td:
        s=calc_raw(df,tid); sr[tid]={'ortg':s['ortg'],'drtg':s['drtg']}
    lo=sum(r['ortg'] for r in sr.values())/30
    ld=sum(r['drtg'] for r in sr.values())/30
    mgp=df[df['TEAM_ID']==list(td.keys())[0]].shape[0]
    print(f"  lg avg ORTG:{lo:.1f} DRTG:{ld:.1f} max_gp:{mgp}")

    ratings={}
    for tid,name in td.items():
        ratings[name]={
            'season': calc(df,tid,sr,lo,ld,None),
            'l14':    calc(df,tid,sr,lo,ld,min(14,mgp)),
            'l7':     calc(df,tid,sr,lo,ld,min(7,mgp)),
        }

    stretches,games=build_fouls(df)
    out={'updated':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
         'season':SEASON,'league_avg':{'ortg':round(lo,1),'drtg':round(ld,1)},
         'ratings':ratings,'fouls':{'stretches':stretches,'games':games}}
    with open(OUT_FILE,'w') as f: json.dump(out,f,indent=2)
    print(f"  Saved {OUT_FILE} ({len(ratings)} teams, {len(stretches)} stretches)")

if __name__=='__main__': main()
