"""Synthetic transport/clock tests; no credentials or requests to Riot."""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_collector import match

from collector.server import Collector, KeyPool, Paused, RateLimiter, RiotClient, RiotError, Store, retry_after


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.now += delay
        return self.stopped


def pool(size=2, verified=True):
    result = KeyPool()
    result.add(['RGAPI-'+str(i)*24 for i in range(size)])
    for slot in result.slots:
        slot.verified = verified
    return result


def response(value=None, headers=None):
    result = io.BytesIO(json.dumps(value if value is not None else {'ok':True}).encode())
    result.headers = headers or {}
    return result


def error(code, headers=None, reason=''):
    return urllib.error.HTTPError('https://americas.api.riotgames.com/test',code,'Synthetic',
                                  {'Content-Type':'application/json',**(headers or {})},
                                  io.BytesIO(json.dumps({'status':{'message':reason}}).encode()))


class PoolTests(unittest.TestCase):
    def test_distributed_requests_are_faster_without_duplicate_calls(self):
        def run(size):
            keys, clock, sent = pool(size),Clock(),[]
            client = RiotClient(keys,clock,lambda **_:None)
            def transport(req,**_):
                sent.append((req.full_url,req.get_header('X-riot-token'),clock.now))
                return response()
            with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
                for i in range(20):
                    client.get('americas',f'/test/{i}','match')
            self.assertEqual(len(sent),20)
            self.assertEqual(len({url for url,_,_ in sent}),20)
            for slot in keys.slots:
                times=[at for _,key,at in sent if key==slot.value]
                self.assertTrue(all(b-a>=1.3-1e-6 for a,b in zip(times,times[1:])))
            return clock.now-1000,keys
        single,_ = run(1)
        pooled,keys = run(2)
        self.assertLess(pooled,single*.6)
        self.assertEqual([s.requests for s in keys.slots],[10,10])

    def test_full_key_does_not_hold_up_available_key(self):
        keys,clock = pool(),Clock()
        first,second = keys.slots
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now):
            first.limiter.observe('americas','match',{'X-App-Rate-Limit':'1:120'})
            self.assertIs(keys.acquire('americas','match',clock),first)
            self.assertIs(keys.acquire('americas','match',clock),second)
            self.assertGreater(first.limiter.delay('americas','ids',clock.now),119)
            self.assertEqual(first.limiter.delay('na1','rank',clock.now),0)
            self.assertIs(keys.acquire('americas','match',clock),second)
        self.assertLess(clock.now,1002)

    def test_application_429_can_use_other_key_but_service_429_waits_for_all(self):
        for scope in ('application','service',''):
            with self.subTest(scope=scope):
                keys,clock,sent = pool(),Clock(),[]
                client=RiotClient(keys,clock,lambda **_:None)
                def transport(req,**_):
                    sent.append((req.get_header('X-riot-token'),clock.now))
                    if len(sent)==1:
                        raise error(429,{'Retry-After':'30','X-Rate-Limit-Type':scope})
                    return response()
                with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
                    client.get('americas','/test','match')
                if scope=='application':
                    self.assertNotEqual(sent[0][0],sent[1][0])
                    self.assertEqual(sent[0][1],sent[1][1])
                else:
                    self.assertGreaterEqual(sent[1][1]-sent[0][1],30)

    def test_method_cooldown_does_not_block_other_methods(self):
        keys,clock=pool(1),Clock()
        slot=keys.slots[0]
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now):
            keys.throttle(slot,'americas','match',{'Retry-After':'20','X-Rate-Limit-Type':'method'})
            self.assertGreaterEqual(slot.limiter.delay('americas','match',clock.now),20)
            self.assertEqual(slot.limiter.delay('americas','ids',clock.now),0)
            self.assertIs(keys.acquire('americas','ids',clock),slot)

    def test_server_counts_and_pause_prevent_an_extra_request(self):
        keys,clock=pool(1),Clock()
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now):
            keys.observe(keys.slots[0],'americas','match',{'X-App-Rate-Limit':'20:1,100:120','X-App-Rate-Limit-Count':'1:1,100:120'})
            self.assertGreaterEqual(keys.slots[0].limiter.delay('americas','match',clock.now),120)
            clock.stopped=True
            with self.assertRaises(Paused):
                keys.acquire('americas','match',clock)
        self.assertEqual(keys.slots[0].requests,0)

    def test_expired_key_fails_over_and_never_exposes_key(self):
        keys,clock,updates=pool(),Clock(),[]
        client=RiotClient(keys,clock,lambda **value:updates.append(value))
        bad=keys.slots[0].value
        def transport(req,**_):
            if req.get_header('X-riot-token')==bad:
                raise error(403,reason='API key expired '+bad)
            return response({'puuid':'player'})
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
            self.assertEqual(client.get('americas','/test','account'),{'puuid':'player'})
        self.assertTrue(keys.slots[0].disabled)
        self.assertEqual(client.calls,2)
        self.assertNotIn(bad,json.dumps(updates)+json.dumps(keys.summary()))

    def test_all_rejected_keys_stop_after_trying_each_once(self):
        keys,clock=pool(8),Clock()
        client=RiotClient(keys,clock,lambda **_:None)
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=lambda *a,**k: (_ for _ in ()).throw(error(401,reason='Unknown API key'))):
            with self.assertRaises(RiotError):
                client.get('americas','/test','account')
        self.assertEqual([s.requests for s in keys.slots],[1]*8)
        self.assertFalse(keys.usable())

    def test_identity_probe_excludes_incompatible_and_unverified_keys(self):
        keys,clock=pool(3,verified=False),Clock()
        client=RiotClient(keys,clock,lambda **_:None)
        def transport(req,**_):
            key=req.get_header('X-riot-token')
            if key==keys.slots[0].value:
                return response({'puuid':'different-namespace'})
            if key==keys.slots[2].value:
                return response({})
            return response({'puuid':'cached-player'})
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
            self.assertEqual(client.resolve_account('/test','cached-player'),{'puuid':'cached-player'})
            self.assertIs(keys.acquire('americas','match',clock),keys.slots[1])
        self.assertTrue(keys.slots[0].incompatible)
        self.assertFalse(keys.slots[2].verified)

    def test_profile_search_cannot_reintroduce_a_key_that_failed_the_cache_probe(self):
        keys,clock=pool(2,verified=False),Clock()
        client=RiotClient(keys,clock,lambda **_:None)
        calls=[]
        def transport(req,**_):
            calls.append((req.full_url,req.get_header('X-riot-token')))
            if req.full_url.endswith('/probe'):
                return response({} if req.get_header('X-riot-token')==keys.slots[0].value else {'puuid':'cached-player'})
            return response({'puuid':'other-player'})
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
            client.resolve_account('/probe','cached-player')
            self.assertEqual(client.resolve_account('/other',verified_only=True),{'puuid':'other-player'})
        self.assertEqual([key for url,key in calls if url.endswith('/other')],[keys.slots[1].value])
        self.assertFalse(keys.slots[0].verified)

    def test_duplicate_validation_and_removal_preserve_limits(self):
        keys,clock=pool(1),Clock()
        slot=keys.slots[0]
        secret=slot.value
        keys.add([secret,' '+secret+' '])
        self.assertEqual(len(keys.slots),1)
        with self.assertRaises(ValueError):
            keys.add(['RGAPI-'+('z'*24),'bad'])
        self.assertEqual(len(keys.slots),1)
        with patch('collector.server.time.monotonic',side_effect=lambda:clock.now):
            keys.throttle(slot,'americas','match',{'Retry-After':'60','X-Rate-Limit-Type':'application'})
            keys.remove(slot.id)
            keys.add([secret])
            self.assertGreaterEqual(keys.slots[0].limiter.delay('americas','match',clock.now),60)
        self.assertNotEqual(keys.slots[0].id,slot.id)
        self.assertNotIn(secret,json.dumps(keys.summary()))

    def test_retry_after_is_finite_and_supports_http_date(self):
        for value in ('NaN','Infinity','bad'):
            self.assertEqual(retry_after({'Retry-After':value}),120)
        with patch('collector.server.time.time',return_value=0):
            self.assertEqual(retry_after({'Retry-After':'Thu, 01 Jan 1970 00:02:00 GMT'}),120)

    def test_full_import_shares_cache_and_preserves_comparisons_across_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(Store(Path(directory)/'test.sqlite3'))
            collector.keys=pool(2,verified=False)
            clock=Clock()
            collector.stop=clock
            games={f'h{i}':match(f'h{i}',(i+1)*2_000_000,wins=range(5) if i<10 else range(5,10)) for i in range(20)}
            records={'anchor':match('anchor',100_000_000,wins=range(5)),**games}
            fetched=[]
            def transport(req,**_):
                url=req.full_url
                if '/by-riot-id/' in url:
                    return response({'puuid':'p0','gameName':'Example','tagLine':'NA1'})
                if '/summoner/' in url:
                    return response({'profileIconId':1,'summonerLevel':100})
                if '/entries/' in url:
                    return response([])
                if '/ids?' in url:
                    return response(list(games) if 'endTime=' in url else ['anchor'])
                mid=url.rsplit('/',1)[-1]
                fetched.append(mid)
                return response(records[mid])
            with patch('collector.server.TARGET',1),patch('collector.server.time.monotonic',side_effect=lambda:clock.now),patch('collector.server.urllib.request.urlopen',side_effect=transport):
                collector.run(('Example','NA1'))
                self.assertEqual(collector.state['status'],'complete')
                first=collector.view()['matches'][0]
                self.assertTrue(first['complete'])
                self.assertEqual((first['allyMean'],first['enemyMean'],first['gap']),(50,50,0))
                collector.run(('Example','NA1'))
                self.assertEqual(collector.state['status'],'complete')
                self.assertEqual(collector.view()['matches'][0]['gap'],first['gap'])
            self.assertEqual(len(fetched),21)
            self.assertEqual(len(set(fetched)),21)
            self.assertTrue(all(slot.requests>0 for slot in collector.keys.slots))

    def test_server_key_does_not_start_import_or_write_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3')
            secret='RGAPI-'+('z'*24)
            with patch.dict('os.environ', {'RIOT_API_KEY': secret}):
                collector=Collector(store)
            self.assertIsNone(collector.thread)
            self.assertTrue(collector.view()['connected'])
            self.assertNotIn(secret,json.dumps(collector.view()))
            self.assertNotIn(secret,Path(store.path).read_bytes().decode(errors='ignore'))
            with patch.dict('os.environ', {'RIOT_API_KEY': ''}):
                self.assertFalse(Collector(store).view()['connected'])
