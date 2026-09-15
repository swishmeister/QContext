import json
import io
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from collector.server import Collector, KeyPool, Paused, RateLimiter, RiotClient, RiotError, Store, average_rank, duo_pairs, handler_class, parse_riot_id, retry_after, summarize_history


def match(mid, start, wins=(), duration=1800, queue=420):
    return {'metadata': {'matchId': mid}, 'info': {
        'queueId': queue, 'gameStartTimestamp': start,
        'gameEndTimestamp': start + duration * 1000, 'gameDuration': duration,
        'participants': [dict(puuid=f'p{i}', teamId=100 if i < 5 else 200,
                              win=i in wins, championName='Ahri', teamPosition='MIDDLE',
                              riotIdGameName=f'Player {i}', summonerLevel=50)
                         for i in range(10)]}}


class HistoryTests(unittest.TestCase):
    def test_champion_habits_use_complete_prior_twenty_only(self):
        for count,expected in [(0,'first_in_20'),(1,'flex'),(5,'flex'),(6,'main'),(15,'main'),(16,'one_trick'),(20,'one_trick')]:
            with self.subTest(count=count):
                games=[match(f'h{i}',(i+1)*2_000_000) for i in range(20)]
                for i,g in enumerate(games):
                    g['info']['participants'][0]['championName']='Ahri' if i<count else 'Other'+str(i%4)
                # The anchor's champion is not included in the prior window.
                games.append(match('anchor',100_000_000))
                result=summarize_history(games,'p0',100_000_000,champion='Ahri')
                self.assertEqual(result['championHabit'],expected)
                self.assertEqual(result['championGames'],count)
                self.assertEqual(sum(result['championCounts'].values()),20)
                self.assertEqual(summarize_history(games,'p0',100_000_000,champion='Ahri',missing=1)['championHabit'],'unknown')
        self.assertEqual(summarize_history([],'p0',100_000_000,champion='Ahri')['championHabit'],'unknown')

    def test_main_champion_must_be_most_played_with_ties_allowed(self):
        games=[match(f'h{i}',(i+1)*2_000_000) for i in range(20)]
        for i,g in enumerate(games):
            g['info']['participants'][0]['championName']='Ahri' if i<6 else 'Jinx' if i<12 else 'Other'+str(i)
        self.assertEqual(summarize_history(games,'p0',100_000_000,champion='Ahri')['championHabit'],'main')
        games[-1]['info']['participants'][0]['championName']='Jinx'
        self.assertEqual(summarize_history(games,'p0',100_000_000,champion='Ahri')['championHabit'],'flex')
        games[-1]['info']['participants'][0].pop('championName')
        self.assertEqual(summarize_history(games,'p0',100_000_000,champion='Ahri')['championHabit'],'unknown')

    def test_main_roles_ties_support_and_off_role(self):
        games = [match(f'h{i}', (i+1)*2_000_000) for i in range(20)]
        for i,g in enumerate(games):
            g['info']['participants'][0]['teamPosition'] = 'UTILITY' if i<10 else 'MIDDLE'
        result = summarize_history(games,'p0',100_000_000,role='UTILITY')
        self.assertEqual(result['mainRoles'],['Mid','Support'])
        self.assertEqual(result['mainRoleGames'],10)
        self.assertEqual(result['roleStatus'],'main')
        self.assertEqual(summarize_history(games,'p0',100_000_000,role='JUNGLE')['roleStatus'],'off')
        self.assertEqual(summarize_history([],'p0',100_000_000,role='UTILITY')['roleStatus'],'unknown')

    def test_only_completed_prior_ranked_matches_and_latest_twenty(self):
        cutoff = 100_000_000
        games = [match(f'old{i}', cutoff-(i+1)*2_000_000, wins=[0] if i < 10 else []) for i in range(25)]
        games += [match('anchor', cutoff, wins=[0]),
                  match('overlaps', cutoff-10_000, wins=[0]),
                  match('future', cutoff+2_000_000, wins=[0]),
                  match('remake', cutoff-500_000, wins=[0], duration=90),
                  match('flex', cutoff-2_000_000, wins=[0], queue=440)]
        result = summarize_history(list(reversed(games)), 'p0', cutoff, 'Ahri', 'MIDDLE')
        self.assertEqual(result['matchIds'], [f'old{i}' for i in range(20)])
        self.assertEqual((result['wins'], result['losses'], result['winRate']), (10, 10, 50))
        self.assertEqual((result['championGames'], result['roleGames'], result['streak']), (20, 20, 10))
        self.assertTrue(result['complete'])

    def test_missing_and_short_histories_are_never_complete(self):
        games = [match(f'h{i}', (i+1)*2_000_000, wins=[]) for i in range(20)]
        self.assertFalse(summarize_history(games, 'p0', 100_000_000, missing=1)['complete'])
        result = summarize_history(games[:3], 'p0', 100_000_000)
        self.assertEqual((result['n'], result['streak'], result['winRate']), (3, -3, 0))
        self.assertFalse(result['complete'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'test.sqlite3')
        self.collector = Collector(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_duo_tags_persist_per_match_and_prevent_conflicting_partners(self):
        self.store.put_match(match('anchor',100_000_000))
        self.store.put_setting('anchors',['anchor'])
        self.collector.tag_duo('anchor',['p1','p0'],'duo')
        tags=Collector(self.store).store.duo_tags('anchor')
        self.assertEqual(tags[0]['players'],['p0','p1'])
        self.assertEqual(tags[0]['status'],'duo')
        self.assertEqual(self.store.duo_tags('other-match'),[])
        for pair in (['p0','p2'],['p0','p5'],['p0','p0'],['p0','missing']):
            with self.assertRaises(ValueError):
                self.collector.tag_duo('anchor',pair,'duo')
        self.collector.tag_duo('anchor',['p0','p1'],'not_duo')
        self.assertEqual(self.store.duo_tags('anchor')[0]['status'],'not_duo')
        self.collector.tag_duo('anchor',['p1','p0'],'clear')
        self.assertEqual(self.store.duo_tags('anchor'),[])

    def test_profile_switch_restores_anchors_and_uses_supplied_riot_id(self):
        first = {'puuid':'p0','gameName':'First','tagLine':'NA1'}
        self.collector.activate_account(first)
        self.store.put_setting('anchors',['saved-first-match'])

        class FakeAPI:
            def resolve_account(inner,path,expected=None,verified_only=False):
                if expected=='p0':
                    return first
                self.assertTrue(path.endswith('/Other%20Player/NA2'))
                return {'puuid':'p1','gameName':'Other Player','tagLine':'NA2'}

            def get(inner,host,path,method,params=None):
                if method=='summoner':
                    return {'profileIconId':7128,'summonerLevel':99}
                if method=='ids':
                    return []
                raise AssertionError(method)

        with patch('collector.server.RiotClient',return_value=FakeAPI()):
            self.collector.run(('Other Player','NA2'))
        self.assertEqual(self.collector.account['puuid'],'p1')
        self.assertEqual(self.store.setting('anchors'),[])
        self.assertEqual(self.collector.view()['account']['name'],'Other Player')
        self.assertIn('/7128.png',self.collector.view()['account']['iconUrl'])
        self.collector.activate_account(first)
        self.assertEqual(self.store.setting('anchors'),['saved-first-match'])

    def test_unknown_profile_leaves_existing_account_selected(self):
        first = {'puuid':'p0','gameName':'First','tagLine':'NA1'}
        self.collector.activate_account(first)
        self.store.put_setting('anchors',['saved-first-match'])
        with patch('collector.server.RiotClient') as factory:
            factory.return_value.resolve_account.side_effect=[first,None]
            self.collector.run(('Missing','NA1'))
        self.assertEqual(self.collector.account,first)
        self.assertEqual(self.store.setting('anchors'),['saved-first-match'])
        self.assertEqual(self.collector.state['status'],'error')

    def test_pause_retains_matches_and_resume_reuses_them(self):
        games = {f'h{i}': match(f'h{i}', (30-i)*2_000_000) for i in range(20)}
        calls = []
        collector = self.collector

        class FakeAPI:
            interrupted = False
            def get(self, host, path, method, params=None):
                if method == 'ids':
                    return list(games)
                mid = path.rsplit('/', 1)[-1]
                if mid == 'h2' and not self.interrupted:
                    self.interrupted = True
                    raise Paused()
                calls.append(mid)
                return games[mid]

        api = FakeAPI()
        with self.assertRaises(Paused):
            collector.history(api, 'p0', 100_000_000)
        self.assertIsNotNone(self.store.match('h0'))
        self.assertIsNone(self.store.window('p0', 100_000_000))
        result = collector.history(api, 'p0', 100_000_000)
        self.assertTrue(result['complete'])
        self.assertEqual(calls.count('h0'), 1)
        self.assertEqual(calls.count('h1'), 1)
        with patch.object(api, 'get', side_effect=AssertionError('Unexpected API request')):
            self.assertEqual(collector.history(api, 'p0', 100_000_000), result)

    def test_missing_match_does_not_get_replaced_by_twenty_first_game(self):
        games = {f'h{i}': match(f'h{i}', (30-i)*2_000_000) for i in range(21)}

        class FakeAPI:
            def get(self, host, path, method, params=None):
                if method == 'ids':
                    return list(games)
                mid = path.rsplit('/', 1)[-1]
                return None if mid == 'h2' else games[mid]

        result = self.collector.history(FakeAPI(), 'p0', 100_000_000)
        self.assertEqual((len(result['ids']), result['missing'], result['complete']), (19, 1, False))
        self.assertNotIn('h20', result['ids'])

    def test_four_teammates_against_five_opponents_excludes_self(self):
        cutoff = 100_000_000
        anchor = match('anchor', cutoff, wins=range(5))
        anchor['info']['participants'][0].update(championId=103,profileIcon=7128)
        self.store.put_match(anchor)
        self.store.put_setting('anchors', ['anchor'])
        self.collector.account = {'puuid': 'p0'}
        ids = []
        for i in range(20):
            mid = f'h{i}'
            ids.append(mid)
            # Self always wins; allies 50%, opponents 25%.
            winners = [0] + (list(range(1, 5)) if i < 10 else []) + (list(range(5, 10)) if i < 5 else [])
            self.store.put_match(match(mid, cutoff-(i+1)*2_000_000, winners))
        for i in range(10):
            self.store.put_window(f'p{i}', cutoff, {'ids': ids, 'missing': 0, 'complete': True})
        view = self.collector.view()['matches'][0]
        self.assertEqual((view['allyMean'], view['enemyMean'], view['gap']), (50, 25, 25))
        self.assertEqual((view['participants'][0]['championId'],view['participants'][0]['profileIconId']),(103,7128))
        self.store.put_window('p9', cutoff, {'ids': ids[:19], 'missing': 0, 'complete': False})
        view = self.collector.view()['matches'][0]
        self.assertFalse(view['complete'])
        self.assertIsNone(view['gap'])

    def test_rank_observation_time_and_restart_do_not_persist_key(self):
        self.collector.keys.add(['RGAPI-'+('x'*24),'RGAPI-'+('y'*24)])
        self.collector.update(status='running')
        with patch('collector.server.time.time', return_value=123456):
            self.store.put_snapshot('p0', {'rank': None, 'sourceMatch': 'anchor'})
        self.assertEqual(self.store.snapshot('p0')['observedAt'], 123456000)
        restarted = Collector(self.store)
        self.assertEqual(restarted.state['status'], 'paused')
        self.assertFalse(restarted.keys.usable())
        for secret in ('RGAPI-'+('x'*24),'RGAPI-'+('y'*24)):
            self.assertNotIn(secret,json.dumps(self.collector.view()))
            self.assertNotIn(secret,Path(self.store.path).read_bytes().decode(errors='ignore'))
        self.assertNotIn('apiKeys', restarted.view())

    def test_local_api_requires_valid_origin_and_change_token(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_class(self.collector))
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{httpd.server_port}'

        def request(path, body=None, **headers):
            req = urllib.request.Request(url+path, data=body, headers={'Host': '127.0.0.1:8766', **headers})
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        try:
            code, status = request('/api/status')
            self.assertEqual(code, 200)
            self.assertEqual(request('/api/status', Origin='https://example.com')[0], 403)
            self.assertEqual(request('/api/status', Host='evil.example:8766')[0], 403)
            self.assertEqual(request('/api/pause', b'{}', **{'Content-Type': 'application/json'})[0], 403)
            self.assertEqual(request('/api/pause', b'{}', **{'Content-Type': 'application/json', 'X-Queue-Lab-Token': status['csrf']})[0], 200)
            self.assertNotIn('csrf', request('/api/export')[1])
            synthetic='RGAPI-'+('t'*24)
            body=json.dumps({'keys':[synthetic,synthetic]}).encode()
            self.assertEqual(request('/api/keys',body,**{'Content-Type':'application/json'})[0],403)
            headers={'Content-Type':'application/json','X-Queue-Lab-Token':status['csrf']}
            self.assertEqual(request('/api/keys',body,**headers)[0],404)
            saved=request('/api/status')[1]
            self.assertFalse(saved['connected'])
            self.assertNotIn('apiKeys', saved)
            self.assertNotIn(synthetic,json.dumps(saved))
            self.assertNotIn('apiKeys',request('/api/export')[1])

            self.store.put_match(match('anchor',100_000_000))
            self.store.put_setting('anchors',['anchor'])
            self.collector.account={'puuid':'p0'}
            body=json.dumps({'matchId':'anchor','players':['p0','p1'],'status':'duo'}).encode()
            self.assertEqual(request('/api/duo',body,**{'Content-Type':'application/json'})[0],403)
            self.assertEqual(request('/api/duo',body,**{'Content-Type':'application/json','X-Queue-Lab-Token':status['csrf']})[0],200)
            exported=request('/api/export')[1]['matches'][0]['duoPairs']
            self.assertEqual((exported[0]['status'],exported[0]['source']),('duo','manual'))
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()


class RateTests(unittest.TestCase):
    def test_application_limit_can_pause_without_another_request(self):
        limiter = RateLimiter()
        limiter.observe('americas', 'match', {'X-App-Rate-Limit': '1:120'})
        stop = threading.Event()
        with patch('collector.server.time.monotonic', return_value=1000):
            limiter.wait('americas', 'match', stop)
            stop.set()
            with self.assertRaises(Paused):
                limiter.wait('americas', 'match', stop)
        self.assertEqual(len(limiter.events[('americas', 'app')]), 1)


class RankAndSearchTests(unittest.TestCase):
    def test_rank_average_crosses_divisions_and_excludes_unranked(self):
        def player(division,lp):
            return {'rankSnapshot':{'observedAt':123,'rank':{'tier':'EMERALD','rank':division,'leaguePoints':lp}}}
        players=[player('II',60),player('I',20),{}, {'rankSnapshot':{'rank':None}}]
        value=average_rank(players)
        self.assertEqual((value['label'],value['count'],value['total']),('Emerald II 90 LP',2,4))
        self.assertIsNone(average_rank([{},{}])['label'])
        self.assertEqual(average_rank([player('II',99),player('I',1)])['label'],'Emerald I 0 LP')

    def test_apex_tiers_use_shared_master_lp_scale(self):
        players=[{'rankSnapshot':{'rank':{'tier':tier,'rank':'I','leaguePoints':lp}}} for tier,lp in [('MASTER',100),('CHALLENGER',900)]]
        self.assertEqual(average_rank(players)['label'],'Master+ 500 LP')

    def test_search_requires_full_riot_id(self):
        self.assertEqual(parse_riot_id(' Other Player # NA1 '),('Other Player','NA1'))
        for value in ('name','name#','#tag','name#tag#extra',None,'bad\nname#tag'):
            with self.assertRaises(ValueError):
                parse_riot_id(value)


class DuoEvidenceTests(unittest.TestCase):
    def test_only_prior_same_team_matches_count_once(self):
        anchor=match('anchor',100_000_000)
        records={f'h{i}':match(f'h{i}',10_000_000+i*2_000_000) for i in range(4)}
        records['h3']['info']['participants'][1]['teamId']=200
        records.update(anchor=anchor,future=match('future',110_000_000))
        hist={'matchIds':list(records)+['h0'],'complete':True}
        people=[{'puuid':f'p{i}','team':100,'history':hist} for i in range(2)]
        pairs=duo_pairs(anchor,people,records.get,[])
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0]['sharedGames'],3)
        self.assertEqual(pairs[0]['status'],'possible')
        self.assertEqual(pairs[0]['matchIds'],['h0','h1','h2'])
        people[1]['team']=200
        self.assertEqual(duo_pairs(anchor,people,records.get,[]),[])

    def test_manual_override_and_incomplete_evidence_are_explicit(self):
        anchor=match('anchor',100_000_000)
        records={f'h{i}':match(f'h{i}',10_000_000+i*2_000_000) for i in range(3)}
        people=[{'puuid':f'p{i}','team':100,'history':{'matchIds':list(records),'complete':False}} for i in range(3)]
        tags=[{'players':['p0','p1'],'status':'duo','recordedAt':123}]
        pairs=duo_pairs(anchor,people,records.get,tags)
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0]['source'],'manual')
        self.assertFalse(pairs[0]['historyComplete'])
        tags[0]['status']='not_duo'
        pair=next(p for p in duo_pairs(anchor,people,records.get,tags) if p['players']==['p0','p1'])
        self.assertEqual(pair['status'],'not_duo')


class RiotClientTests(unittest.TestCase):
    def test_expired_key_reports_safe_reason_and_response_code(self):
        updates = []
        client = RiotClient('synthetic-test-key', threading.Event(), lambda **v: updates.append(v))
        body = json.dumps({'status': {'message': 'API key expired synthetic-test-key'}}).encode()
        error = urllib.error.HTTPError('https://americas.api.riotgames.com/test', 403, 'Forbidden', {'Content-Type': 'application/json'}, io.BytesIO(body))
        with patch('collector.server.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(RiotError) as caught:
                client.get('americas', '/test', 'account')
        self.assertIn('expired', str(caught.exception))
        self.assertIn('HTTP 403 during account lookup', str(caught.exception))
        self.assertNotIn('synthetic-test-key', str(caught.exception))
        self.assertIn({'apiStatus': 403, 'apiMethod': 'account'}, updates)

    def test_requests_identify_queue_lab(self):
        client = RiotClient('synthetic-test-key', threading.Event(), lambda **_: None)
        response = io.BytesIO(b'{"puuid":"synthetic-player"}')
        response.headers = {'Content-Type': 'application/json'}
        with patch('collector.server.urllib.request.urlopen', return_value=response) as request:
            self.assertEqual(client.get('americas', '/test', 'account')['puuid'], 'synthetic-player')
        headers = {k.lower(): v for k, v in request.call_args.args[0].header_items()}
        self.assertEqual(headers['user-agent'], 'QueueContext/0.1 (local personal research)')
        self.assertEqual(headers['x-riot-token'], 'synthetic-test-key')

    def test_edge_block_is_distinguished_from_api_key_rejection(self):
        for content_type, expected_status in [('text/plain', 0), ('application/json;charset=utf-8', 403)]:
            with self.subTest(content_type=content_type):
                client = RiotClient('synthetic-test-key', threading.Event(), lambda **_: None)
                error = urllib.error.HTTPError('https://americas.api.riotgames.com/test', 403, 'Forbidden', {'Content-Type': content_type}, io.BytesIO(b'not logged'))
                with patch('collector.server.urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(RiotError) as caught:
                        client.get('americas', '/test', 'account')
                self.assertEqual(caught.exception.status, expected_status)
                self.assertNotIn('synthetic-test-key', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
