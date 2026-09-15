"""Regression coverage for dashboard timeouts with a populated remote database."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.server import Collector, Store
from test_collector import match


class StatusPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'status.sqlite3')
        self.collector = Collector(self.store)
        self.collector.account = {'puuid': 'p0'}
        self.ids = [f'h{i}' for i in range(20)]
        with self.store.session():
            for i, mid in enumerate(self.ids):
                self.store.put_match(match(mid, (i + 1) * 2_000_000, wins=range(5)))
            for i in range(20):
                cutoff = 100_000_000 + i * 2_000_000
                self.store.put_match(match(f'a{i}', cutoff, wins=range(5)))
                for p in range(10):
                    self.store.put_window(f'p{p}', cutoff, {'ids': self.ids, 'missing': 0, 'complete': True})
            self.store.put_setting('anchors', [f'a{i}' for i in range(20)])

    def test_full_dashboard_uses_bounded_queries_with_cold_and_warm_summaries(self):
        previous = None
        for phase in ('cold', 'warm'):
            with self.subTest(phase=phase), patch.object(self.store, 'execute', wraps=self.store.execute) as execute:
                view = self.collector.view()
                # Per-record queries previously required 4,844 cold / 844 warm reads.
                self.assertLessEqual(execute.call_count, 70)
            self.assertEqual(len(view['matches']), 20)
            for anchor in view['matches']:
                self.assertTrue(anchor['complete'])
                self.assertEqual((anchor['allyMean'], anchor['enemyMean'], anchor['gap']), (100, 0, 100))
                self.assertEqual(len(anchor['duoPairs']), 20)
                self.assertTrue(all(pair['sharedGames'] == 20 for pair in anchor['duoPairs']))
            if previous is not None:
                self.assertEqual(view, previous)
            previous = view

    def test_refresh_observes_new_windows_ranks_and_manual_duos(self):
        self.collector.view()
        self.store.put_window('p9', 100_000_000, {'ids': self.ids[:19], 'missing': 0, 'complete': False})
        with patch('collector.server.time.time', return_value=100):
            self.store.put_snapshot('p0', {'rank': {'tier': 'GOLD', 'rank': 'IV', 'leaguePoints': 0}})
        with patch('collector.server.time.time', return_value=200):
            self.store.put_snapshot('p0', {'rank': {'tier': 'PLATINUM', 'rank': 'IV', 'leaguePoints': 5}})
        self.collector.tag_duo('a0', ['p0', 'p1'], 'duo')
        view = self.collector.view()
        anchor = view['matches'][0]
        self.assertFalse(anchor['complete'])
        self.assertIsNone(anchor['gap'])
        self.assertEqual(anchor['participants'][9]['history']['n'], 19)
        rank = anchor['participants'][0]['rankSnapshot']
        self.assertEqual((rank['observedAt'], rank['rank']['tier']), (200000, 'PLATINUM'))
        pair = next(pair for pair in anchor['duoPairs'] if pair['players'] == ['p0', 'p1'])
        self.assertEqual((pair['status'], pair['source']), ('duo', 'manual'))

    def test_missing_records_and_profile_switch_do_not_reuse_other_profile_data(self):
        self.store.put_window('p9', 100_000_000, {'ids': [*self.ids[:19], 'unavailable'], 'missing': 1, 'complete': False})
        anchor = self.collector.view()['matches'][0]
        self.assertFalse(anchor['complete'])
        self.assertIsNone(anchor['gap'])
        self.assertEqual(anchor['participants'][9]['history']['n'], 19)
        self.collector.activate_account({'puuid': 'other', 'gameName': 'Other', 'tagLine': 'TEST'})
        view = self.collector.view()
        self.assertEqual(view['account']['name'], 'Other')
        self.assertEqual(view['matches'], [])

    def test_batch_reads_preserve_missing_values_and_empty_requests(self):
        self.assertEqual(self.store.matches_by_id([]), {})
        self.assertEqual(self.store.windows_for([], 100_000_000), {})
        self.assertEqual(self.store.snapshots_for([]), {})
        self.assertEqual(set(self.store.matches_by_id(['h0', 'h0', 'absent'])), {'h0'})
        self.assertEqual(self.store.windows_for(['p0', 'absent'], 100_000_000), {'p0': self.store.window('p0', 100_000_000)})
        self.assertEqual(self.store.snapshots_for(['absent']), {})
