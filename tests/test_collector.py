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

from collector.server import Collector, Paused, RateLimiter, RiotClient, RiotError, Store, handler_class, summarize_history


def match(mid, start, wins=(), duration=1800, queue=420):
    return {'metadata': {'matchId': mid}, 'info': {
        'queueId': queue, 'gameStartTimestamp': start,
        'gameEndTimestamp': start + duration * 1000, 'gameDuration': duration,
        'participants': [dict(puuid=f'p{i}', teamId=100 if i < 5 else 200,
                              win=i in wins, championName='Ahri', teamPosition='MIDDLE',
                              riotIdGameName=f'Player {i}', summonerLevel=50)
                         for i in range(10)]}}


class HistoryTests(unittest.TestCase):
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
        self.store.put_window('p9', cutoff, {'ids': ids[:19], 'missing': 0, 'complete': False})
        view = self.collector.view()['matches'][0]
        self.assertFalse(view['complete'])
        self.assertIsNone(view['gap'])

    def test_rank_observation_time_and_restart_do_not_persist_key(self):
        self.collector.key = 'not-a-real-key'
        self.collector.update(status='running')
        with patch('collector.server.time.time', return_value=123456):
            self.store.put_snapshot('p0', {'rank': None, 'sourceMatch': 'anchor'})
        self.assertEqual(self.store.snapshot('p0')['observedAt'], 123456000)
        restarted = Collector(self.store)
        self.assertEqual(restarted.state['status'], 'paused')
        self.assertFalse(restarted.key)
        self.assertNotIn('not-a-real-key', json.dumps(restarted.view()))

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


class RiotClientTests(unittest.TestCase):
    def test_requests_identify_queue_lab(self):
        client = RiotClient('synthetic-test-key', threading.Event(), lambda **_: None)
        response = io.BytesIO(b'{"puuid":"synthetic-player"}')
        response.headers = {'Content-Type': 'application/json'}
        with patch('collector.server.urllib.request.urlopen', return_value=response) as request:
            self.assertEqual(client.get('americas', '/test', 'account')['puuid'], 'synthetic-player')
        headers = {k.lower(): v for k, v in request.call_args.args[0].header_items()}
        self.assertEqual(headers['user-agent'], 'QueueLab/0.1 (local personal research)')
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
