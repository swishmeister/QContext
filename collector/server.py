"""Loopback Riot collector. Hosted access goes through the authenticated gateway."""
from __future__ import annotations

import hashlib
import json
import math
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
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
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


def champion_habit(counts, champion, n, complete):
    """Descriptive labels for this complete prior window, never lifetime mastery."""
    if not complete or n!=WINDOW or not champion or sum(counts.values())!=n:
        return 'unknown'
    played = counts.get(champion,0)
    if played==0:
        return 'first_in_20'
    if played>=16:
        return 'one_trick'
    if played>=6 and played==max(counts.values()):
        return 'main'
    return 'flex'


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
    champion_counts = Counter(p['championName'] for p in players if p.get('championName'))
    complete = len(players)==WINDOW and missing==0
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
                complete=complete, missing=missing,
                championCounts=dict(champion_counts),championHabit=champion_habit(champion_counts,champion,len(players),complete),
                championGames=sum(p.get('championName') == champion for p in players),
                roleGames=sum(p.get('teamPosition') == role for p in players) if role else None,
                mainRoles=main_roles,mainRoleGames=main_count,roleCounts=dict(role_counts),roleStatus=role_status,
                streak=streak, matchIds=[g['metadata']['matchId'] for g in valid])


def duo_pairs(anchor, people, get_match, tags):
    """Repeated co-teaming is evidence, never API confirmation of a premade."""
    cutoff = timing(anchor)[0]
    manual = {tuple(t['players']):t for t in tags}
    confirmed = {p for t in tags if t['status']=='duo' for p in t['players']}
    records, result = {}, []
    for a,b in combinations(people,2):
        if a['team'] != b['team']:
            continue
        pair = tuple(sorted([a['puuid'],b['puuid']]))
        ah,bh = a.get('history') or {},b.get('history') or {}
        shared = []
        for mid in sorted(set(ah.get('matchIds',[])) & set(bh.get('matchIds',[]))):
            if mid not in records:
                records[mid] = get_match(mid)
            match = records[mid]
            if not match or mid==anchor['metadata']['matchId'] or not eligible(match) or timing(match)[0]>=cutoff or timing(match)[1]>cutoff:
                continue
            pa,pb = participant(match,pair[0]),participant(match,pair[1])
            if pa and pb and pa['teamId']==pb['teamId']:
                shared.append(mid)
        tag = manual.get(pair)
        # Don't propose conflicting partners for a manually confirmed duo.
        if not tag and (len(shared)<3 or any(p in confirmed for p in pair)):
            continue
        result.append(dict(players=list(pair),status=tag['status'] if tag else 'possible',
                           source='manual' if tag else 'history',recordedAt=tag.get('recordedAt') if tag else None,
                           sharedGames=len(shared),matchIds=shared,historyComplete=bool(ah.get('complete') and bh.get('complete'))))
    return result


class Store:
    def __init__(self, path):
        self.database_url = os.environ.get('DATABASE_URL')
        self.is_postgres = bool(self.database_url)
        self.path = str(path)

        if not self.is_postgres:
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        with self.connect() as db:
            if not self.is_postgres:
                self.execute(db, 'PRAGMA journal_mode=WAL')

            self.execute(db, 'CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            self.execute(db, 'CREATE TABLE IF NOT EXISTS windows (puuid TEXT, before_ms BIGINT, payload TEXT NOT NULL, PRIMARY KEY (puuid,before_ms))')

            if self.is_postgres:
                self.execute(db, 'CREATE TABLE IF NOT EXISTS snapshots (id BIGSERIAL PRIMARY KEY, puuid TEXT, observed_ms BIGINT, payload TEXT NOT NULL)')
            else:
                self.execute(db, 'CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, puuid TEXT, observed_ms INTEGER, payload TEXT NOT NULL)')

            self.execute(db, 'CREATE INDEX IF NOT EXISTS idx_snapshots_puuid_observed ON snapshots(puuid,observed_ms DESC)')
            self.execute(db, 'CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            self.execute(db, 'CREATE TABLE IF NOT EXISTS roster_snapshots (match_id TEXT PRIMARY KEY)')
            self.execute(db, "CREATE TABLE IF NOT EXISTS duo_tags (match_id TEXT NOT NULL,a TEXT NOT NULL,b TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('duo','not_duo')),recorded_ms BIGINT NOT NULL,PRIMARY KEY(match_id,a,b))")

    @contextmanager
    def connect(self):
        if self.is_postgres:
            import psycopg2

            db = psycopg2.connect(self.database_url, connect_timeout=20)
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        else:
            db = sqlite3.connect(self.path, timeout=20)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def execute(self, db, sql, params=()):
        if self.is_postgres:
            cursor = db.cursor()
            cursor.execute(sql.replace('?', '%s'), params)
            return cursor
        return db.execute(sql, params)

    def setting(self, name, default=None):
        with self.connect() as db:
            row = self.execute(db, 'SELECT payload FROM settings WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_setting(self, name, value):
        with self.connect() as db:
            self.execute(
                db,
                'INSERT INTO settings (name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                (name, json.dumps(value)),
            )

    def duo_tags(self, mid):
        with self.connect() as db:
            rows = self.execute(db, 'SELECT a,b,status,recorded_ms FROM duo_tags WHERE match_id=? ORDER BY a,b', (mid,)).fetchall()
        return [dict(players=[a,b],status=status,recordedAt=at) for a,b,status,at in rows]

    def match(self, match_id):
        with self.connect() as db:
            row = self.execute(db, 'SELECT payload FROM matches WHERE id=?', (match_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_match(self, match):
        with self.connect() as db:
            self.execute(
                db,
                'INSERT INTO matches (id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                (match['metadata']['matchId'], json.dumps(match)),
            )

    def window(self, puuid, before_ms):
        with self.connect() as db:
            row = self.execute(db, 'SELECT payload FROM windows WHERE puuid=? AND before_ms=?', (puuid, before_ms)).fetchone()
        return json.loads(row[0]) if row else None

    def put_window(self, puuid, before_ms, value):
        with self.connect() as db:
            self.execute(
                db,
                'INSERT INTO windows (puuid,before_ms,payload) VALUES (?,?,?) ON CONFLICT(puuid,before_ms) DO UPDATE SET payload=excluded.payload',
                (puuid, before_ms, json.dumps(value)),
            )

    def snapshot(self, puuid):
        with self.connect() as db:
            row = self.execute(db, 'SELECT observed_ms,payload FROM snapshots WHERE puuid=? ORDER BY observed_ms DESC LIMIT 1', (puuid,)).fetchone()
        return dict(observedAt=row[0], **json.loads(row[1])) if row else None

    def put_snapshot(self, puuid, payload):
        with self.connect() as db:
            self.execute(
                db,
                'INSERT INTO snapshots (puuid,observed_ms,payload) VALUES (?,?,?)',
                (puuid, int(time.time()*1000), json.dumps(payload)),
            )

class RateLimiter:
    """One key's budgets, independently scoped by routing host and method."""
    def __init__(self):
        self.events = defaultdict(deque)
        self.limits = {}
        self.blocked = defaultdict(float)
        self.last = defaultdict(float)

    def delay(self, host, method, now):
        delay = max(0, self.blocked[(host,'app')]-now,
                    self.blocked[(host,method)]-now, self.last[host]+1.3-now)
        for key, fallback in [((host, 'app'), [(20,1),(100,120)]), ((host,method), [])]:
            history = self.events[key]
            limits = self.limits.get(key, fallback)
            max_window = max([seconds for _,seconds in limits]+[120])
            while history and history[0] <= now-max_window:
                history.popleft()
            for count, seconds in limits:
                recent = [t for t in history if t > now-seconds]
                if len(recent) >= count:
                    delay = max(delay, recent[-count]+seconds-now+.1)
        return delay

    def reserve(self, host, method, now):
        for key in [(host,'app'),(host,method)]:
            self.events[key].append(now)
        self.last[host] = now

    def wait(self, host, method, stop):
        while not stop.is_set():
            now = time.monotonic()
            delay = self.delay(host,method,now)
            if delay <= 0:
                self.reserve(host,method,now)
                return
            stop.wait(min(delay,1))
        raise Paused()

    def observe(self, host, method, headers):
        for prefix, key in [('X-App-Rate-Limit',(host,'app')),('X-Method-Rate-Limit',(host,method))]:
            try:
                limits = [tuple(map(int, p.split(':'))) for p in headers.get(prefix,'').split(',') if p]
                if limits and all(c>0 and seconds>0 for c,seconds in limits):
                    self.limits[key] = limits
                counts = {seconds:count for count,seconds in
                          (map(int,p.split(':')) for p in headers.get(prefix+'-Count','').split(',') if p)}
                # Server counts include calls made outside this process. When full,
                # conservatively wait a whole window rather than assuming its start.
                now = time.monotonic()
                for count,seconds in self.limits.get(key,[]):
                    if counts.get(seconds,0)>=count:
                        self.blocked[key] = max(self.blocked[key],now+seconds+.1)
            except (ValueError,TypeError):
                pass


def retry_after(headers):
    raw = headers.get('Retry-After','120')
    try:
        seconds = float(raw)
    except (ValueError,TypeError):
        try:
            seconds = parsedate_to_datetime(raw).timestamp()-time.time()
        except (ValueError,TypeError,OverflowError):
            seconds = 120
    return max(1,seconds) if math.isfinite(seconds) else 120


class KeySlot:
    def __init__(self, value, label, limiter=None):
        self.value = value
        self.id = secrets.token_hex(12)
        self.label = label
        self.limiter = limiter or RateLimiter()
        self.requests = 0
        self.disabled = False
        self.incompatible = False
        self.verified = False
        self.denied = {}
        self.reason = None


class KeyPool:
    """Memory-only approved credentials; the import has a single request dispatcher."""
    MAX_KEYS = 10

    def __init__(self):
        self.slots = []
        self.lock = threading.RLock()
        self.next_label = 1
        self.cursor = 0
        self.service_blocks = defaultdict(float)
        # Removed secrets are forgotten, but their limits survive re-entry this session.
        self.retired = {}

    def add(self, values):
        if not isinstance(values,list) or not 1<=len(values)<=self.MAX_KEYS:
            raise ValueError('Enter between 1 and 10 API keys.')
        clean = []
        for value in values:
            if not isinstance(value,str) or not re.fullmatch(r'RGAPI-[A-Za-z0-9-]{20,100}',value.strip()):
                raise ValueError('Each key must begin with RGAPI- and contain a valid key value.')
            if value.strip() not in clean:
                clean.append(value.strip())
        with self.lock:
            new = [value for value in clean if all(slot.value!=value for slot in self.slots)]
            if len(self.slots)+len(new)>self.MAX_KEYS:
                raise ValueError('You can connect up to 10 API keys. Remove an old key first.')
            for value in new:
                digest = hashlib.sha256(value.encode()).digest()
                slot = KeySlot(value,f'Key {self.next_label}',self.retired.pop(digest,None))
                self.next_label += 1
                self.slots.append(slot)

    def remove(self, slot_id):
        with self.lock:
            slot = next((slot for slot in self.slots if slot.id==slot_id),None)
            if slot is None:
                raise ValueError('That key is no longer connected. Refresh Settings.')
            self.retired[hashlib.sha256(slot.value.encode()).digest()] = slot.limiter
            self.slots.remove(slot)

    def usable(self):
        with self.lock:
            return any(not slot.disabled for slot in self.slots)

    def summary(self):
        with self.lock:
            return [dict(id=slot.id,label=slot.label,requests=slot.requests,
                         status='incompatible' if slot.incompatible else 'rejected' if slot.disabled else 'limited' if slot.denied else 'ready' if slot.verified else 'unverified',
                         reason=slot.reason) for slot in self.slots]

    def acquire(self, host, method, stop, only=None):
        while not stop.is_set():
            with self.lock:
                available = [slot for slot in self.slots if not slot.disabled and (host,method) not in slot.denied and (only is None or slot is only) and (method=='account' or slot.verified)]
                if not available:
                    reason = next((slot.denied.get((host,method)) or slot.reason for slot in self.slots if slot.reason),None)
                    raise RiotError(reason or 'No usable API keys. Open Settings to add a valid key.',403)
                now = time.monotonic()
                ordered = self.slots[self.cursor:]+self.slots[:self.cursor]
                candidates = [(max(self.service_blocks[host]-now,slot.limiter.delay(host,method,now)),slot)
                              for slot in ordered if slot in available]
                delay,slot = min(candidates,key=lambda item:item[0])
                if delay<=0:
                    slot.limiter.reserve(host,method,now)
                    slot.requests += 1
                    self.cursor = (self.slots.index(slot)+1)%len(self.slots)
                    return slot
            stop.wait(min(delay,1))
        raise Paused()

    def observe(self, slot, host, method, headers):
        with self.lock:
            slot.limiter.observe(host,method,headers)

    def throttle(self, slot, host, method, headers):
        seconds = retry_after(headers)
        until = time.monotonic()+seconds+.1
        scope = headers.get('X-Rate-Limit-Type','').lower()
        with self.lock:
            if scope in ('application','method'):
                key = (host,'app' if scope=='application' else method)
                slot.limiter.blocked[key] = max(slot.limiter.blocked[key],until)
            else:
                # A service/unknown limit applies to all keys, never rotate around it.
                self.service_blocks[host] = max(self.service_blocks[host],until)
        return seconds, scope

    def reject(self, slot, host, method, reason, disable):
        with self.lock:
            slot.reason = reason
            if disable:
                slot.disabled = True
            else:
                slot.denied[(host,method)] = reason


class RiotClient:
    def __init__(self, keys, stop, update, limiter=None):
        self.stop, self.update = stop, update
        if isinstance(keys,KeyPool):
            self.pool = keys
        else:
            # Keep the single-key client interface for isolated clients and fixtures.
            self.pool = KeyPool()
            self.pool.slots = [KeySlot(keys,'Key 1',limiter)]
            self.pool.slots[0].verified = True
        self.calls = 0

    def resolve_account(self, path, expected=None, verified_only=False):
        # Riot can encrypt PUUIDs differently across applications. Prove that keys
        # share an identity namespace before mixing their responses in one cache.
        account = None
        last_error = None
        with self.pool.lock:
            candidates = [slot for slot in self.pool.slots if not verified_only or slot.verified]
            for slot in self.pool.slots:
                slot.verified = False
        for slot in candidates:
            if slot.disabled:
                continue
            try:
                candidate = self.get('americas',path,'account',only=slot)
            except RiotError as error:
                last_error = error
                if error.status in (401,403):
                    continue
                raise
            if not candidate or not candidate.get('puuid'):
                continue
            baseline = expected or (account or {}).get('puuid')
            if baseline and candidate['puuid']!=baseline:
                with self.pool.lock:
                    slot.incompatible = True
                message = 'This key returns different player identifiers and cannot share this profile’s saved data. Use keys from the same Riot application.'
                self.pool.reject(slot,'americas','account',message,True)
                last_error = RiotError(message)
                continue
            with self.pool.lock:
                slot.verified = True
            account = account or candidate
        if account is None and last_error:
            raise last_error
        return account

    def get(self, host, path, method, params=None, only=None):
        assert host in ('americas', 'na1')
        url = f'https://{host}.api.riotgames.com{path}'
        if params:
            url += '?' + urllib.parse.urlencode(params)
        attempt = 0
        while attempt<5:
            slot = self.pool.acquire(host,method,self.stop,only)
            req = urllib.request.Request(url, headers={
                'X-Riot-Token': slot.value, 'Accept': 'application/json',
                'User-Agent': 'QueueContext/0.1 (local personal research)',
            })
            self.calls += 1
            self.update(requests=self.calls)
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    self.pool.observe(slot,host,method,response.headers)
                    return json.load(response)
            except urllib.error.HTTPError as e:
                self.pool.observe(slot,host,method,e.headers)
                if e.code == 404:
                    return None
                if e.code in (401,403):
                    if 'application/json' not in e.headers.get('Content-Type','').lower():
                        raise RiotError('The connection was blocked before Riot could validate the key. Your keys may still be valid; retry after checking the connection.') from None
                    try:
                        reason = json.loads(e.read(4096)).get('status',{}).get('message','').lower()
                    except (ValueError,AttributeError,TypeError):
                        reason = ''
                    self.update(apiStatus=e.code,apiMethod=method)
                    expired = 'expired' in reason
                    invalid = 'invalid api' in reason or 'unknown api' in reason
                    detail = ('Riot reports that the key has expired.' if expired else
                              'Riot does not recognize this API key.' if invalid else
                              'Riot rejected the key or access to this endpoint.')
                    safe_reason = f'{detail} HTTP {e.code} during {method} lookup. Replace it in Settings.'
                    self.pool.reject(slot,host,method,safe_reason,e.code==401 or expired or invalid)
                    self.update(warning=f'{slot.label} was rejected for {method}. Check Settings; other usable keys will continue.')
                    # Authentication failures don't consume the transient-retry budget.
                    if only is not None or not any(not item.disabled and (host,method) not in item.denied for item in self.pool.slots):
                        raise RiotError(safe_reason,e.code) from None
                    continue
                if e.code == 429:
                    seconds,scope = self.pool.throttle(slot,host,method,e.headers)
                    self.update(message=(f'Riot asked all keys to wait {round(seconds)} seconds on {host}.'
                                         if scope not in ('application','method') else
                                         f'{slot.label} is cooling down for {round(seconds)} seconds; scheduling available keys.'))
                elif e.code >= 500:
                    if self.stop.wait(min(30,2**attempt)):
                        raise Paused()
                else:
                    raise RiotError(f'Riot returned HTTP {e.code}. Saved progress can be resumed.',e.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if self.stop.wait(min(30,2**attempt)):
                    raise Paused()
            attempt += 1
        raise RiotError('Riot is unavailable or still rate limiting requests. Refresh to resume from saved data.')


class Collector:
    def __init__(self, store):
        self.store = store
        self.keys = KeyPool()
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
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
                self.keys.add([key])
            if not self.keys.usable():
                raise ValueError('Open Settings and connect a valid Riot API key first.')
            self.stop.clear()
            self.update(status='running',message=f'Looking up {target[0]}#{target[1]}…',requests=0,done=0,total=0,warning=None,
                        startedAt=int(time.time()*1000),apiStatus=None,apiMethod=None)
            self.thread = threading.Thread(target=self.run,args=(target,),daemon=True)
            self.thread.start()

    def configure_keys(self, keys=None, remove_id=None):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Pause the import before changing API keys.')
            if (keys is None)==(remove_id is None):
                raise ValueError('Add keys or remove one key at a time.')
            if keys is not None:
                self.keys.add(keys)
            elif isinstance(remove_id,str):
                self.keys.remove(remove_id)
            else:
                raise ValueError('Choose a connected key to remove.')

    def pause(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.stop.set()
                self.update(status='pausing',message='Pausing after the current request…')

    def tag_duo(self, mid, players, status):
        with self.lock:
            if not isinstance(mid,str) or not isinstance(players,list) or len(players)!=2 or not all(isinstance(p,str) for p in players) or len(set(players))!=2 or status not in ('duo','not_duo','clear'):
                raise ValueError('Choose two different teammates and a valid duo label.')
            if mid not in self.store.setting('anchors',[]):
                raise ValueError('Select a match from the current profile.')
            match = self.store.match(mid)
            if not match:
                raise ValueError('The selected match record is unavailable.')
            a,b = sorted(players)
            pa,pb = participant(match,a),participant(match,b)
            if not pa or not pb or pa['teamId']!=pb['teamId']:
                raise ValueError('A duo must contain two players on the same team in this match.')
            with self.store.connect() as db:
                if status=='clear':
                    self.store.execute(db,'DELETE FROM duo_tags WHERE match_id=? AND a=? AND b=?',(mid,a,b))
                else:
                    if status=='duo':
                        existing = self.store.execute(db,"SELECT a,b FROM duo_tags WHERE match_id=? AND status='duo'",(mid,)).fetchall()
                        if any((x,y)!=(a,b) and ({x,y}&{a,b}) for x,y in existing):
                            raise ValueError('One player already has a confirmed duo partner in this match. Clear that tag first.')
                    self.store.execute(db,'INSERT INTO duo_tags (match_id,a,b,status,recorded_ms) VALUES (?,?,?,?,?) ON CONFLICT(match_id,a,b) DO UPDATE SET status=excluded.status, recorded_ms=excluded.recorded_ms',(mid,a,b,status,int(time.time()*1000)))

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
            captured = self.store.execute(db,'SELECT 1 FROM roster_snapshots WHERE match_id=?',(mid,)).fetchone()
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
                self.store.execute(db,'INSERT INTO roster_snapshots (match_id) VALUES (?) ON CONFLICT(match_id) DO NOTHING',(mid,))

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
        api = RiotClient(self.keys,self.stop,self.update)
        try:
            name, tag = target or ((self.account or {}).get('gameName','Llewellyn'),(self.account or {}).get('tagLine','300'))
            path = '/riot/account/v1/accounts/by-riot-id/'+urllib.parse.quote(name,safe='')+'/'+urllib.parse.quote(tag,safe='')
            probe = self.store.setting('identity_probe') or self.account
            probe_account = None
            same_probe = False
            if probe and probe.get('gameName') and probe.get('tagLine'):
                probe_path = '/riot/account/v1/accounts/by-riot-id/'+urllib.parse.quote(probe['gameName'],safe='')+'/'+urllib.parse.quote(probe['tagLine'],safe='')
                probe_account = api.resolve_account(probe_path,probe['puuid'])
                if not probe_account:
                    # A renamed probe must not silently reset the cache's namespace.
                    raise RiotError('The saved identity-check profile could not be found. Its Riot ID may have changed; the import stopped before mixing key data.')
                same_probe = probe['gameName'].casefold()==name.casefold() and probe['tagLine'].casefold()==tag.casefold()
            account = probe_account if same_probe else api.resolve_account(path,verified_only=bool(probe_account))
            if not account or not account.get('puuid'):
                raise RiotError(f'Riot could not find {name}#{tag}. Check the full Riot ID and that the player is on North America.')
            account.setdefault('gameName',name)
            account.setdefault('tagLine',tag)
            # Confirm the platform before replacing the selected profile.
            summoner = api.get('na1','/lol/summoner/v4/summoners/by-puuid/'+urllib.parse.quote(account['puuid'],safe=''),'summoner')
            if not summoner:
                raise RiotError('This profile was not found on North America. The current search supports NA accounts.')
            account.update(profileIconId=summoner.get('profileIconId'),summonerLevel=summoner.get('summonerLevel'),profileObservedAt=int(time.time()*1000))
            if not self.store.setting('identity_probe'):
                self.store.put_setting('identity_probe',dict(puuid=account['puuid'],gameName=account['gameName'],tagLine=account['tagLine']))
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
            connected = self.keys.usable()
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
                                   tag=p.get('riotIdTagline',''),champion=p.get('championName',''),championId=p.get('championId'),
                                   profileIconId=p.get('profileIcon'),role=p.get('teamPosition',''),
                                   level=p.get('summonerLevel'),team=p['teamId'],isSelf=p['puuid']==self.account['puuid'],
                                   history=hist,rankSnapshot=self.store.snapshot(p['puuid'])))
            allies = [p for p in people if p['team']==me['teamId'] and not p['isSelf']]
            enemies = [p for p in people if p['team']!=me['teamId']]
            complete = len(allies)==4 and len(enemies)==5 and all(p['history'] and p['history']['complete'] for p in allies+enemies)
            ally_mean = round(mean(p['history']['winRate'] for p in allies),2) if complete else None
            enemy_mean = round(mean(p['history']['winRate'] for p in enemies),2) if complete else None
            result.append(dict(id=mid,startedAt=start,duration=duration,win=bool(me['win']),champion=me.get('championName'),
                               duoPairs=duo_pairs(match,people,self.store.match,self.store.duo_tags(mid)),
                               teamRanks={'allies':average_rank([p for p in people if p['team']==me['teamId']]),'enemies':average_rank(enemies)},
                               team=me['teamId'],participants=people,complete=complete,allyMean=ally_mean,enemyMean=enemy_mean,
                               gap=round(ally_mean-enemy_mean,2) if complete else None,
                               historiesReady=sum(p['history'] is not None for p in people)))
        with self.store.connect() as db:
            snapshots = self.store.execute(db,'SELECT COUNT(*) FROM snapshots').fetchone()[0]
            cached = self.store.execute(db,'SELECT COUNT(*) FROM matches').fetchone()[0]
        account = self.account or {}
        icon = account.get('profileIconId')
        if icon is None and result:
            latest = self.store.match(result[0]['id'])
            icon = (participant(latest,account['puuid']) or {}).get('profileIcon')
        icon_url = f'https://ddragon.leagueoflegends.com/cdn/{ICON_VERSION}/img/profileicon/{icon}.png' if isinstance(icon,int) and icon>=0 else None
        return dict(account={'name':account.get('gameName','Llewellyn'),'tag':account.get('tagLine','300'),'puuid':account.get('puuid'),
                             'platform':'NA','queue':'Ranked Solo/Duo','iconUrl':icon_url,'level':account.get('summonerLevel')},connected=connected,
                    job=state,matches=result,snapshotCount=snapshots,cachedMatches=cached,apiKeys=self.keys.summary(),
                    methodology={'historyGames':WINDOW,'targetGames':TARGET,'queueId':QUEUE,'minimumDurationSeconds':180,
                                 'comparison':'Mean of four teammate win rates minus mean of five opponent win rates. Only complete 20-game histories enter comparisons.',
                                 'rank':'Rank at observation time, never claimed to be pre-match rank.',
                                 'limits':'Descriptive pilot; no MMR estimate, smurf classification, win prediction, or causal conclusion.'})


def handler_class(collector, *, port=8766, public_origin=None):
    csrf = secrets.token_urlsafe(32)
    origins = {public_origin} if public_origin else {'http://127.0.0.1:5173','http://localhost:5173'}

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
            return host in (f'127.0.0.1:{port}',f'localhost:{port}') and (not origin or origin in origins)

        def do_GET(self):
            if not self.allowed():
                return self.send(403,{'error':'Local access only.'})
            if self.path == '/api/health':
                with collector.store.connect() as db:
                    collector.store.execute(db,'SELECT 1')
                return self.send(200,{'ok':True})
            if self.path == '/api/status':
                return self.send(200,dict(collector.view(),csrf=csrf))
            if self.path == '/api/export':
                observations = collector.view()
                observations.pop('apiKeys',None)
                return self.send(200,observations)
            return self.send(404,{'error':'Not found.'})

        def do_POST(self):
            if not self.allowed() or not secrets.compare_digest(self.headers.get('X-Queue-Lab-Token',''),csrf):
                return self.send(403,{'error':'Refresh the dashboard before making changes.'})
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 <= length <= 4096 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    return self.send(400,{'error':'Expected a small JSON request.'})
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body,dict):
                    raise ValueError('Expected an object.')
                if self.path == '/api/import':
                    collector.start(body.get('key'),body.get('riotId'))
                elif self.path == '/api/keys':
                    collector.configure_keys(body.get('keys'),body.get('removeId'))
                elif self.path == '/api/pause':
                    collector.pause()
                elif self.path == '/api/duo':
                    collector.tag_duo(body.get('matchId'),body.get('players'),body.get('status'))
                else:
                    return self.send(404,{'error':'Not found.'})
                return self.send(200,{'ok':True})
            except ValueError as e:
                if self.path in ('/api/duo','/api/keys'):
                    return self.send(400,{'error':str(e)})
                return self.send(400,{'error':'Check your key, or wait for the current import to stop before refreshing.'})
            except TypeError:
                return self.send(400,{'error':'Check your key, or wait for the current import to stop before refreshing.'})

    return Handler


if __name__ == '__main__':
    os.umask(0o077)
    store = Store(Path(os.environ.get('QUEUE_CONTEXT_DATA_DIR',str(ROOT/'data')))/'queue-lab.sqlite3')
    collector = Collector(store)
    port = int(os.environ.get('QUEUE_CONTEXT_COLLECTOR_PORT','8766'))
    server = ThreadingHTTPServer(('127.0.0.1',port),handler_class(collector,port=port,public_origin=os.environ.get('QUEUE_CONTEXT_PUBLIC_URL')))
    print(f'Queue Context collector listening on http://127.0.0.1:{port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        collector.pause()
    finally:
        server.server_close()
