type Rank = { tier: string; rank: string; leaguePoints: number };
type Snapshot = { observedAt: number; rank: Rank | null };
type StatsMatch = {
  win: boolean;
  complete: boolean;
  gap: number | null;
  participants: { isSelf: boolean; rankSnapshot: Snapshot | null }[];
};

const tiers = ['IRON', 'BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'EMERALD', 'DIAMOND'];
const divisions = ['IV', 'III', 'II', 'I'];
const apex = ['MASTER', 'GRANDMASTER', 'CHALLENGER'];
const titleCase = (value: string) => value.charAt(0) + value.slice(1).toLowerCase();

function rankScore(rank: Rank | null | undefined): number | null {
  if (!rank || !Number.isFinite(rank.leaguePoints) || rank.leaguePoints < 0) return null;
  const tier = tiers.indexOf(rank.tier);
  const division = divisions.indexOf(rank.rank);
  if (tier >= 0 && division >= 0) return tier * 400 + division * 100 + rank.leaguePoints;
  return apex.includes(rank.tier) ? 2800 + rank.leaguePoints : null;
}

function averageLabel(score: number): string {
  const rounded = Math.round(score);
  if (rounded >= 2800) return `Master+ ${rounded - 2800} LP`;
  return `${titleCase(tiers[Math.floor(rounded / 400)])} ${divisions[Math.floor((rounded % 400) / 100)]} ${rounded % 100} LP`;
}

export function profileStats(matches: StatsMatch[]) {
  const recent = matches.slice(0, 20);
  const wins = recent.filter(match => match.win).length;
  const comparisons = recent.filter(match => match.complete && match.gap !== null);
  const players = recent.flatMap(match => match.participants);
  const scores = players.map(player => rankScore(player.rankSnapshot?.rank)).filter((score): score is number => score !== null);
  const selfSnapshots = players.filter(player => player.isSelf && player.rankSnapshot).map(player => player.rankSnapshot!);
  const current = selfSnapshots.reduce<Snapshot | null>((latest, snapshot) => !latest || snapshot.observedAt > latest.observedAt ? snapshot : latest, null);
  const rank = current?.rank;
  const currentLabel = !current ? null : !rank ? 'Unranked' : rankScore(rank) === null ? null :
    `${titleCase(rank.tier)}${apex.includes(rank.tier) ? '' : ` ${rank.rank}`} ${rank.leaguePoints} LP`;

  return {
    games: recent.length,
    wins,
    losses: recent.length - wins,
    winRate: recent.length ? 100 * wins / recent.length : null,
    currentRank: currentLabel,
    rankObservedAt: current?.observedAt ?? null,
    lobbyRank: scores.length ? averageLabel(scores.reduce((sum, score) => sum + score, 0) / scores.length) : null,
    rankedPlayers: scores.length,
    playerCount: recent.length * 10,
    comparisonCount: comparisons.length,
    historyDifference: comparisons.length ? comparisons.reduce((sum, match) => sum + match.gap!, 0) / comparisons.length : null,
  };
}
