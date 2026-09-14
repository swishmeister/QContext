import assert from 'node:assert/strict';
import test from 'node:test';
import { profileStats } from '../lib/profile-stats.ts';

const player = (tier, rank, leaguePoints, isSelf = false, observedAt = 100) => ({
  isSelf, rankSnapshot: { observedAt, rank: tier ? { tier, rank, leaguePoints } : null },
});
const game = (participants, win = true, gap = null) => ({ participants, win, gap, complete: gap !== null });

test('last 20 games determine win rate and only complete comparisons enter the difference', () => {
  const games = Array.from({ length: 21 }, (_, i) => game([], i < 11, i < 2 ? [4, -2][i] : null));
  const stats = profileStats(games);
  assert.equal(stats.games, 20);
  assert.equal(stats.winRate, 55);
  assert.equal(stats.losses, 9);
  assert.equal(stats.comparisonCount, 2);
  assert.equal(stats.historyDifference, 1);
});

test('lobby average includes self and repeated appearances, with missing ranks excluded', () => {
  const stats = profileStats([
    game([player('EMERALD', 'II', 60, true), player('EMERALD', 'I', 20), player(null), { isSelf: false, rankSnapshot: null }]),
    game([player('EMERALD', 'II', 60, true)]),
  ]);
  assert.equal(stats.lobbyRank, 'Emerald II 80 LP');
  assert.equal(stats.rankedPlayers, 3);
  assert.equal(stats.playerCount, 20);
  assert.equal(stats.currentRank, 'Emerald II 60 LP');
});

test('newest self snapshot wins, unranked is distinct from unavailable, and apex uses a shared scale', () => {
  let stats = profileStats([game([player('MASTER', 'I', 100, true, 100), player('CHALLENGER', 'I', 900)]), game([player(null, null, null, true, 200)])]);
  assert.equal(stats.currentRank, 'Unranked');
  assert.equal(stats.rankObservedAt, 200);
  assert.equal(stats.lobbyRank, 'Master+ 500 LP');
  stats = profileStats([]);
  assert.equal(stats.currentRank, null);
  assert.equal(stats.winRate, null);
  assert.equal(stats.lobbyRank, null);
  assert.equal(stats.historyDifference, null);
  assert.equal(profileStats([game([player('EMERALD', 'II', 99), player('EMERALD', 'I', 1)])]).lobbyRank, 'Emerald I 0 LP');
});
