"""Local-only Riot collector. Python 3.11+, no third-party dependencies."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
QUEUE = 420
WINDOW = 20
TARGET = 20
ICON_VERSION = '16.18.1'
ROLES = {'TOP':'Top','JUNGLE':'Jungle','MIDDLE':'Mid','BOTTOM':'ADC','UTILITY':'Support','SUPPORT':'Support'}
TIERS = ['IRON','BRONZE','SILVER','GOLD','PLATINUM','EMERALD','DIAMOND']
DIVISIONS = ['IV','III','II','I']


def parse_riot_id(value):
    if not isinstance(value,str) or value.count('#') != 1 or len(value)>80 or any(ord(c)<32 for c in value):
        raise ValueError('Enter a Riot ID as Name#Tag.')
    name, tag = (part.strip() for part in value.split('#'))
    if not name or not tag:
        raise ValueError('Both the game name and tag are required.')
    return name, tag


def average_rank(players):
    values, observations = [], []
    for player in players:
        snapshot = player.get('rankSnapshot') or {}
        rank = snapshot.get('rank') or {}
        tier, division, lp = rank.get('tier'), rank.get('rank'), rank.get('leaguePoints')
        if not isinstance(lp,(int,float)) or lp < 0:
            continue
        if tier in TIERS and division in DIVISIONS:
            value = TIERS.index(tier)*400 + DIVISIONS.index(division)*100 + lp
        elif tier in ('MASTER','GRANDMASTER','CHALLENGER'):
            # Apex tiers share an LP ladder; their changing thresholds aren't divisions.
            value = 2800 + lp
        else:
            continue
        values.append(value)
        if snapshot.get('observedAt'):
            observations.append(snapshot['observedAt'])
    result = dict(label=None,count=len(values),total=len(players),oldestObservation=min(observations) if observations else None,
                  newestObservation=max(observations) if observations else None)
    if values:
        score = int(mean(values)+0.5)
        if score >= 2800:
            result['label'] = f'Master+ {score-2800} LP'
        else:
            result['label'] = f'{TIERS[score//400].title()} {DIVISIONS[(score%400)//100]} {score%100} LP'
    return result


class Paused(Exception):
    pass


class RiotError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def timing(match):
    info = match['info']
    start = int(info.get('gameStartTimestamp') or info.get('gameCreation') or 0)
    duration = float(info.get('gameDuration') or 0)
    # This pilot uses modern match-v5 records, where duration is in seconds.
    end = int(info.get('gameEndTimestamp') or (start + duration * 1000))
    return start, end, duration


def eligible(match):
    info = match.get('info', {})
    return (info.get('queueId') == QUEUE and len(info.get('participants', [])) == 10
            and timing(match)[0] > 0 and timing(match)[2] >= 180)


def participant(match, puuid):
    return next((p for p in match['info']['participants'] if p.get('puuid') == puuid), None)


def summarize_history(games, puuid, before_ms, champion=None, role=None, missing=0):
    # Recheck chronology even for cached data: current/future outcomes never enter a window.
    valid = [g for g in games if eligible(g) and timing(g)[1] <= before_ms
             and timing(g)[0] < before_ms and participant(g, puuid)]
    valid.sort(key=lambda g: timing(g)[0], reverse=True)
    valid = valid[:WINDOW]
    players = [participant(g, puuid) for g in valid]
    role_counts = Counter(ROLES[p['teamPosition']] for p in players if p.get('teamPosition') in ROLES)
    main_count = max(role_counts.values(),default=0)
    main_roles = sorted(r for r,c in role_counts.items() if c==main_count)
    current_role = ROLES.get(role)
    role_status = ('main' if current_role in main_roles else 'off') if current_role and main_roles else 'unknown'
    wins = sum(p.get('win') is True for p in players)
    streak = 0
    if players:
        first = players[0].get('win') is True
        for p in players:
            if (p.get('win') is True) != first:
                break
            streak += 1 if first else -1
    return dict(n=len(players), wins=wins, losses=len(players)-wins,
                winRate=round(100*wins/len(players), 1) if players else None,
                complete=len(players) == WINDOW and missing == 0, missing=missing,
                championGames=sum(p.get('championName') == champion for p in players),
                roleGames=sum(p.get('teamPosition') == role for p in players) if role else None,
                mainRoles=main_roles,mainRoleGames=main_count,roleCounts=dict(role_counts),roleStatus=role_status,
                streak=streak, matchIds=[g['metadata']['matchId'] for g in valid])


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS windows (puuid TEXT, before_ms INTEGER, payload TEXT NOT NULL, PRIMARY KEY (puuid,before_ms))')
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, puuid TEXT, observed_ms INTEGER, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_puuid_observed ON snapshots(puuid,observed_ms DESC)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS roster_snapshots (match_id TEXT PRIMARY KEY)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, name, default=None):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM settings WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_setting(self, name, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (name, json.dumps(value)))

    def match(self, match_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM matches WHERE id=?', (match_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_match(self, match):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO matches VALUES (?,?)', (match['metadata']['matchId'], json.dumps(match)))

    def window(self, puuid, before_ms):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM windows WHERE puuid=? AND before_ms=?', (puuid, before_ms)).fetchone()
        return json.loads(row[0]) if row else None

    def put_window(self, puuid, before_ms, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO windows VALUES (?,?,?)', (puuid, before_ms, json.dumps(value)))

    def snapshot(self, puuid):
        with self.connect() as db:
            row = db.execute('SELECT observed_ms,payload FROM snapshots WHERE puuid=? ORDER BY observed_ms DESC LIMIT 1', (puuid,)).fetchone()
        return dict(observedAt=row[0], **json.loads(row[1])) if row else None

    def put_snapshot(self, puuid, payload):
        with self.connect() as db:
            db.execute('INSERT INTO snapshots (puuid,observed_ms,payload) VALUES (?,?,?)', (puuid, int(time.time()*1000), json.dumps(payload)))


class RateLimiter:
    """Conservative defaults plus Riot's application/method limits and Retry-After."""
    def __init__(self):
        self.events = defaultdict(deque)
        self.limits = {}
        self.blocked_until = 0.0
        self.last = 0.0

    def wait(self, host, method, stop):
        while True:
            now = time.monotonic()
            delay = max(0, self.blocked_until-now, self.last+1.3-now)
            for key, fallback in [((host, 'app'), [(20,1),(100,120)]), ((host,method), [])]:
                history = self.events[key]
                limits = self.limits.get(key, fallback)
                max_window = max([s for _,s in limits]+[120])
                while history and history[0] <= now-max_window:
                    history.popleft()
                for count, seconds in limits:
                    recent = [t for t in history if t > now-seconds]
                    if len(recent) >= count:
                        delay = max(delay, recent[-count]+seconds-now+.1)
            if delay <= 0:
                for key in [(host,'app'),(host,method)]:
                    self.events[key].append(now)
                self.last = now
                return
            if stop.wait(min(delay, 1)):
                raise Paused()

    def observe(self, host, method, headers):
        for header, key in [('X-App-Rate-Limit',(host,'app')),('X-Method-Rate-Limit',(host,method))]:
            try:
                limits = [tuple(map(int, p.split(':'))) for p in headers.get(header,'').split(',') if p]
                if limits and all(c>0 and s>0 for c,s in limits):
                    self.limits[key] = limits
            except ValueError:
                pass


class RiotClient:
    def __init__(self, key, stop, update, limiter=None):
        self.key, self.stop, self.update = key, stop, update
        self.limiter = limiter or RateLimiter()
        self.calls = 0

    def get(self, host, path, method, params=None):
        assert host in ('americas', 'na1')
        url = f'https://{host}.api.riotgames.com{path}'
        if params:
            url += '?' + urllib.parse.urlencode(params)
        for attempt in range(5):
            if self.stop.is_set():
                raise Paused()
            self.limiter.wait(host, method, self.stop)
            # Identify the app: the default Python client identity can be rejected
            # by the edge before the request reaches Riot's API authentication.
            req = urllib.request.Request(url, headers={
                'X-Riot-Token': self.key, 'Accept': 'application/json',
                'User-Agent': 'QueueLab/0.1 (local personal research)',
            })
            self.calls += 1
            self.update(requests=self.calls)
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    self.limiter.observe(host, method, response.headers)
                    return json.load(response)
            except urllib.error.HTTPError as e:
                self.limiter.observe(host, method, e.headers)
                if e.code == 404:
                    return None
                if e.code in (401,403):
                    if 'application/json' not in e.headers.get('Content-Type','').lower():
                        raise RiotError('The connection was blocked before Riot could validate the key. Your key may still be valid; retry the import after checking the connection.') from None
                    # Translate only recognized authentication reasons; never echo
                    # upstream text, which could contain credentials or identifiers.
                    try:
                        reason = json.loads(e.read(4096)).get('status',{}).get('message','').lower()
                    except (ValueError,AttributeError,TypeError):
                        reason = ''
                    self.update(apiStatus=e.code,apiMethod=method)
                    if 'expired' in reason:
                        detail = 'Riot reports that the key has expired.'
                    elif 'invalid api' in reason or 'unknown api' in reason:
                        detail = 'Riot does not recognize this API key.'
                    else:
                        detail = 'Riot rejected the key or access to this endpoint.'
                    raise RiotError(f'{detail} HTTP {e.code} during {method} lookup. Generate a fresh development key in the Riot Developer Portal, then paste it here and retry.', e.code) from None
                if e.code == 429:
                    try:
                        wait = max(1, float(e.headers.get('Retry-After','120')))
                    except ValueError:
                        wait = 120
                    self.limiter.blocked_until = time.monotonic()+wait
                    self.update(message=f'Riot rate limit reached. Waiting {round(wait)} seconds; saved progress is safe.')
                elif e.code >= 500:
                    if self.stop.wait(min(30,2**attempt)):
                        raise Paused()
                else:
                    raise RiotError(f'Riot returned HTTP {e.code}. Saved progress can be resumed.',e.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if self.stop.wait(min(30,2**attempt)):
                    raise Paused()
        raise RiotError('Riot is unavailable or still rate limiting requests. Refresh to resume from saved data.')


class Collector:
    def __init__(self, store):
        self.store = store
        self.key = ''
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.limiter = RateLimiter()
        self.state = store.setting('job', {'status':'idle','message':'Connect a Riot key to start.','requests':0,'done':0,'total':0})
        if self.state['status'] in ('running','pausing'):
            self.state.update(status='paused',message='Collector restarted. Reconnect your key to resume saved progress.')
        self.account = store.setting('account')
        self.summary_cache = {}

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            self.state['updatedAt'] = int(time.time()*1000)
            self.store.put_setting('job',self.state)

    def start(self, key=None, riot_id=None):
        with self.lock:
            target = parse_riot_id(riot_id) if riot_id is not None else (
                (self.account or {}).get('gameName','Llewellyn'),(self.account or {}).get('tagLine','300'))
            if self.thread and self.thread.is_alive():
                raise ValueError('An import is already running.')
            if key is not None:
                if not isinstance(key,str) or not re.fullmatch(r'RGAPI-[A-Za-z0-9-]{20,100}',key.strip()):
                    raise ValueError('Enter a Riot API key beginning with RGAPI-.')
                self.key = key.strip()
            if not self.key:
                raise ValueError('Connect your Riot API key first.')
            self.stop.clear()
            self.update(status='running',message=f'Looking up {target[0]}#{target[1]}…',requests=0,done=0,total=0,warning=None,
                        startedAt=int(time.time()*1000),apiStatus=None,apiMethod=None)
            self.thread = threading.Thread(target=self.run,args=(target,),daemon=True)
            self.thread.start()

    def pause(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.stop.set()
                self.update(status='pausing',message='Pausing after the current request…')

    def get_match(self, api, match_id):
        cached = self.store.match(match_id)
        if cached:
            return cached
        data = api.get('americas',f'/lol/match/v5/matches/{urllib.parse.quote(match_id,safe="")}','match')
        if data:
            if not isinstance(data,dict) or data.get('metadata',{}).get('matchId') != match_id or not isinstance(data.get('info',{}).get('participants'),list):
                raise RiotError('A match response was incomplete. Refresh to retry.')
            self.store.put_match(data)
        return data

    def ids(self, api, puuid, offset, before_ms=None):
        params = dict(queue=QUEUE,start=offset,count=40)
        if before_ms:
            params['endTime'] = before_ms//1000
        return api.get('americas',f'/lol/match/v5/matches/by-puuid/{urllib.parse.quote(puuid,safe="")}/ids','ids',params) or []

    def history(self, api, puuid, before_ms):
        cached = self.store.window(puuid,before_ms)
        if cached and cached.get('complete'):
            return cached
        found, missing, seen = [], 0, set()
        exhausted = False
        for offset in range(0,400,40):
            if self.stop.is_set():
                raise Paused()
            ids = self.ids(api,puuid,offset,before_ms)
            for mid in ids:
                if mid in seen:
                    continue
                seen.add(mid)
                match = self.get_match(api,mid)
                if match is None:
                    missing += 1
                elif eligible(match) and timing(match)[0] < before_ms and timing(match)[1] <= before_ms:
                    if participant(match,puuid):
                        found.append(match)
                    else:
                        missing += 1
                if len(found)+missing >= WINDOW:
                    break
            if len(found)+missing >= WINDOW or len(ids)<40:
                exhausted = len(ids)<40 and len(found)+missing<WINDOW
                break
        found.sort(key=lambda g:timing(g)[0],reverse=True)
        value = dict(ids=[g['metadata']['matchId'] for g in found[:WINDOW]],missing=missing,
                     complete=len(found)>=WINDOW and missing==0,exhausted=exhausted)
        self.store.put_window(puuid,before_ms,value)
        return value

    def ranks(self, api, match, seen):
        mid = match['metadata']['matchId']
        with self.store.connect() as db:
            captured = db.execute('SELECT 1 FROM roster_snapshots WHERE match_id=?',(mid,)).fetchone()
        latest = self.store.setting('anchors',[])
        if captured and latest and mid != latest[0]:
            return
        successful = True
        for p in match['info']['participants']:
            puuid = p['puuid']
            if puuid in seen:
                continue
            seen.add(puuid)
            try:
                rank = api.get('na1',f'/lol/league/v4/entries/by-puuid/{urllib.parse.quote(puuid,safe="")}','rank')
                if rank is None:
                    successful = False
                    continue
                solo = next((r for r in rank if r.get('queueType')=='RANKED_SOLO_5x5'),None)
                self.store.put_snapshot(puuid,dict(rank=solo,sourceMatch=mid))
            except RiotError:
                successful = False
                self.update(warning='Some rank snapshots could not be fetched. Match-history research continues.')
        if successful:
            with self.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO roster_snapshots VALUES (?)',(mid,))

    def activate_account(self, account):
        """Preserve each profile's anchors while sharing the immutable match cache."""
        with self.lock:
            if self.account:
                self.store.put_setting('profile:'+self.account['puuid'],dict(account=self.account,anchors=self.store.setting('anchors',[])))
            saved = self.store.setting('profile:'+account['puuid'],{})
            account = {**saved.get('account',{}),**account}
            self.account = account
            self.store.put_setting('account',account)
            self.store.put_setting('anchors',saved.get('anchors',[]))

    def run(self, target=None):
        api = RiotClient(self.key,self.stop,self.update,self.limiter)
        try:
            name, tag = target or ((self.account or {}).get('gameName','Llewellyn'),(self.account or {}).get('tagLine','300'))
            path = '/riot/account/v1/accounts/by-riot-id/'+urllib.parse.quote(name,safe='')+'/'+urllib.parse.quote(tag,safe='')
            account = api.get('americas',path,'account')
            if not account or not account.get('puuid'):
                raise RiotError(f'Riot could not find {name}#{tag}. Check the full Riot ID and that the player is on North America.')
            account.setdefault('gameName',name)
            account.setdefault('tagLine',tag)
            # Confirm the platform before replacing the selected profile.
            summoner = api.get('na1','/lol/summoner/v4/summoners/by-puuid/'+urllib.parse.quote(account['puuid'],safe=''),'summoner')
            if not summoner:
                raise RiotError('This profile was not found on North America. The current search supports NA accounts.')
            account.update(profileIconId=summoner.get('profileIconId'),summonerLevel=summoner.get('summonerLevel'),profileObservedAt=int(time.time()*1000))
            self.activate_account(account)
            anchors, skipped = [], 0
            self.update(message='Loading your last 20 completed ranked games…')
            for offset in range(0,200,40):
                ids = self.ids(api,account['puuid'],offset)
                for mid in ids:
                    if self.stop.is_set():
                        raise Paused()
                    match = self.get_match(api,mid)
                    if match is None:
                        skipped += 1
                    elif eligible(match) and participant(match,account['puuid']):
                        if mid not in anchors:
                            anchors.append(mid)
                    if len(anchors)>=TARGET:
                        break
                if len(anchors)>=TARGET or len(ids)<40:
                    break
            anchors.sort(key=lambda mid:timing(self.store.match(mid))[0],reverse=True)
            self.store.put_setting('anchors',anchors)
            self.update(total=len(anchors)*10,anchorGaps=skipped)
            if not anchors:
                self.update(status='complete',message='No eligible Ranked Solo/Duo matches were available.')
                return
            # Capture the latest lobby promptly; older/new rosters follow each completed comparison.
            rank_seen = set()
            self.update(message='Recording current ranks for your latest match…')
            self.ranks(api,self.store.match(anchors[0]),rank_seen)
            done = 0
            for i,mid in enumerate(anchors):
                match = self.store.match(mid)
                for p in match['info']['participants']:
                    if self.stop.is_set():
                        raise Paused()
                    name = p.get('riotIdGameName') or p.get('summonerName') or 'Player'
                    self.update(message=f'Match {i+1}/{len(anchors)} · earlier games for {name}',done=done)
                    self.history(api,p['puuid'],timing(match)[0])
                    done += 1
                    self.update(done=done)
                self.update(message=f'Match {i+1}/{len(anchors)} · recording optional ranks')
                self.ranks(api,match,rank_seen)
            self.update(status='complete',message='Import finished. Available comparisons are ready.',finishedAt=int(time.time()*1000))
        except Paused:
            self.update(status='paused',message='Paused. Refresh profile to resume from saved matches.')
        except RiotError as e:
            if e.status in (401,403):
                self.key = ''
            self.update(status='error',message=str(e))
        except Exception:
            # Never echo request objects, key material, or raw upstream bodies.
            self.update(status='error',message='The import stopped unexpectedly. Saved matches are safe; refresh to retry.')

    def view(self):
        with self.lock:
            return self._view()

    def _view(self):
        with self.lock:
            state = dict(self.state)
            connected = bool(self.key)
        result = []
        for mid in self.store.setting('anchors',[]):
            match = self.store.match(mid)
            if not match or not self.account:
                continue
            me = participant(match,self.account['puuid'])
            if not me:
                continue
            start,_,duration = timing(match)
            people = []
            for p in match['info']['participants']:
                saved = self.store.window(p['puuid'],start)
                hist = None
                if saved:
                    cache_key = (p['puuid'],start,p.get('championName'),p.get('teamPosition'),tuple(saved['ids']),saved.get('missing',0))
                    hist = self.summary_cache.get(cache_key)
                    if hist is None:
                        games = [g for key in saved['ids'] if (g:=self.store.match(key))]
                        hist = summarize_history(games,p['puuid'],start,p.get('championName'),p.get('teamPosition'),saved.get('missing',0))
                        self.summary_cache[cache_key] = hist
                people.append(dict(puuid=p['puuid'],name=p.get('riotIdGameName') or p.get('summonerName') or 'Unknown player',
                                   tag=p.get('riotIdTagline',''),champion=p.get('championName',''),role=p.get('teamPosition',''),
                                   level=p.get('summonerLevel'),team=p['teamId'],isSelf=p['puuid']==self.account['puuid'],
                                   history=hist,rankSnapshot=self.store.snapshot(p['puuid'])))
            allies = [p for p in people if p['team']==me['teamId'] and not p['isSelf']]
            enemies = [p for p in people if p['team']!=me['teamId']]
            complete = len(allies)==4 and len(enemies)==5 and all(p['history'] and p['history']['complete'] for p in allies+enemies)
            ally_mean = round(mean(p['history']['winRate'] for p in allies),2) if complete else None
            enemy_mean = round(mean(p['history']['winRate'] for p in enemies),2) if complete else None
            result.append(dict(id=mid,startedAt=start,duration=duration,win=bool(me['win']),champion=me.get('championName'),
                               teamRanks={'allies':average_rank([p for p in people if p['team']==me['teamId']]),'enemies':average_rank(enemies)},
                               team=me['teamId'],participants=people,complete=complete,allyMean=ally_mean,enemyMean=enemy_mean,
                               gap=round(ally_mean-enemy_mean,2) if complete else None,
                               historiesReady=sum(p['history'] is not None for p in people)))
        with self.store.connect() as db:
            snapshots = db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
            cached = db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
        account = self.account or {}
        icon = account.get('profileIconId')
        if icon is None and result:
            latest = self.store.match(result[0]['id'])
            icon = (participant(latest,account['puuid']) or {}).get('profileIcon')
        icon_url = f'https://ddragon.leagueoflegends.com/cdn/{ICON_VERSION}/img/profileicon/{icon}.png' if isinstance(icon,int) and icon>=0 else None
        return dict(account={'name':account.get('gameName','Llewellyn'),'tag':account.get('tagLine','300'),'puuid':account.get('puuid'),
                             'platform':'NA','queue':'Ranked Solo/Duo','iconUrl':icon_url,'level':account.get('summonerLevel')},connected=connected,
                    job=state,matches=result,snapshotCount=snapshots,cachedMatches=cached,
                    methodology={'historyGames':WINDOW,'targetGames':TARGET,'queueId':QUEUE,'minimumDurationSeconds':180,
                                 'comparison':'Mean of four teammate win rates minus mean of five opponent win rates. Only complete 20-game histories enter comparisons.',
                                 'rank':'Rank at observation time, never claimed to be pre-match rank.',
                                 'limits':'Descriptive pilot; no MMR estimate, smurf classification, win prediction, or causal conclusion.'})


def handler_class(collector):
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            host = self.headers.get('Host','')
            origin = self.headers.get('Origin')
            return host in ('127.0.0.1:8766','localhost:8766') and (not origin or origin in ('http://127.0.0.1:5173','http://localhost:5173'))

        def do_GET(self):
            if not self.allowed():
                return self.send(403,{'error':'Local access only.'})
            if self.path == '/api/status':
                return self.send(200,dict(collector.view(),csrf=csrf))
            if self.path == '/api/export':
                return self.send(200,collector.view())
            return self.send(404,{'error':'Not found.'})

        def do_POST(self):
            if not self.allowed() or not secrets.compare_digest(self.headers.get('X-Queue-Lab-Token',''),csrf):
                return self.send(403,{'error':'Refresh the local dashboard before making changes.'})
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 <= length <= 2048 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    return self.send(400,{'error':'Expected a small JSON request.'})
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body,dict):
                    raise ValueError('Expected an object.')
                if self.path == '/api/import':
                    collector.start(body.get('key'),body.get('riotId'))
                elif self.path == '/api/pause':
                    collector.pause()
                else:
                    return self.send(404,{'error':'Not found.'})
                return self.send(200,{'ok':True})
            except (ValueError,TypeError):
                return self.send(400,{'error':'Check your key, or wait for the current import to stop before refreshing.'})

    return Handler


if __name__ == '__main__':
    os.umask(0o077)
    store = Store(ROOT/'data'/'queue-lab.sqlite3')
    collector = Collector(store)
    server = ThreadingHTTPServer(('127.0.0.1',8766),handler_class(collector))
    print('Queue Lab collector listening on http://127.0.0.1:8766',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        collector.pause()
    finally:
        server.server_close()
