# Queue Lab

A local League of Legends research pilot for **North American accounts**, starting with Llewellyn#300. Use the profile search to import another full Riot ID (Name#Tag). It compares the recent histories of your teammates and opponents across your latest 20 completed Ranked Solo/Duo games.

The first version is an exploratory dashboard, not a test that proves or disproves “losers queue.” Riot's internal MMR and matchmaking intent are not observable through this tool.

## Run locally

Requirements: Node.js 22.13 or newer, pnpm, and Python 3.11 or newer. Python uses only its standard library. On this computer, the launcher can also use the bundled Codex Python runtime. Set `QUEUE_LAB_PYTHON` to choose another Python executable.

```sh
pnpm install --frozen-lockfile
pnpm dev
```

Open **http://127.0.0.1:5173**. Keep the launcher running; Ctrl+C stops both services. The dashboard uses port 5173 and the collector uses port 8766, both on loopback. This app currently runs through its local development server; the build is a compilation check, not a standalone hosted application.

Get a development key from the [Riot Developer Portal](https://developer.riotgames.com/), then enter it into the dashboard's password field. Do not paste it into chat or commit it. The key stays in collector memory and must be entered again after a restart. Development keys expire after 24 hours.

Choose **Connect & import 20 matches**. A cold import can take roughly 60–90 minutes under personal-key limits, depending on match overlap, API availability, and retries. Comparisons appear as histories finish. Pause and resume are supported; individual match records are saved immediately so they do not need to be downloaded again. Refreshing after new games reuses cached matches and records current ranks for newly encountered rosters and the latest roster.

## What it collects

- Your latest 20 available ranked Solo/Duo matches, excluding games shorter than three minutes as a remake approximation.
- For every participant, up to 20 eligible matches **completed before the shared match began**. Your own history is shown but excluded from teammate averages.
- Prior win rate, win/loss streak, champion/role familiarity, most-played role(s), and main-role/off-role tags. Ties share main-role status; missing histories are labeled. Support is displayed as Support.
- Team averages of observed rank include all five players, excluding missing/unranked records and showing coverage. Ranks are placed on a display scale with 100 LP per division, averaged, and rounded to the nearest LP. Apex tiers share a Master+ LP scale; this is not internal MMR or an official team rank.
- Summoner icons from Riot Data Dragon (asset version 16.18.1), using the selected summoner profile or latest cached match as a fallback.
- Duo labels per match: manually confirmed pairs, manual dismissals, and possible duos inferred from at least three earlier same-team matches present in both players’ available history windows. Suggestions never establish premade status, and no suggestion does not establish solo queue. The anchor match, future games and games as opponents are excluded. Manual labels persist in SQLite and exports, include their source/time, and can be cleared; clearing a dismissal restores any applicable suggestion. A player may have only one manually confirmed partner per match.
- Account level from the shared match record. Low account level is not treated as proof of a smurf.
- Optional current rank observations, timestamped when fetched. These are never represented as rank at the time of an older game.

The comparison is the mean prior win rate of **four teammates minus five opponents**, in percentage points. It appears only when all nine players have full histories. It is not a predicted win probability or a measure of skill. Missing or unavailable histories remain visible.

## Research limits

Twenty anchor games are enough to assess whether the interface and descriptive insights are useful. They are too few to establish matchmaking bias. Repeat players and overlapping histories create dependence, win rate has substantial sampling noise, and matching on hidden skill can generate patterns without intentional unfairness. Historical rank, internal MMR, party membership, and confirmed autofill are not inferred. No causal tests, statistical significance claims, or win-probability model are implemented. See docs/win-probability.md for the proposed research approach.

History queries inspect at most 400 prior IDs per participant per anchor. The anchor search inspects at most 200 IDs. Riot may not return older or unavailable records. Unknown records within a history keep that comparison incomplete instead of silently substituting older games. The under-three-minute rule may include some longer remakes or exclude unusually short legitimate matches.

## Local data and repository

SQLite data is stored in `data/queue-lab.sqlite3`, ignored by Git. It contains player identifiers and match records, so treat it as personal research data. **Export observations** downloads JSON with the visible comparisons and supporting history IDs. It does not include the API key or the complete raw match cache. The database retains all rank observations; the dashboard/export show the latest observation per player.

The key is neither persisted nor returned by the API. The collector validates local host/origin and a per-process request token for changes. It is intended for one trusted user on one computer, not remote or multi-user access. Browser extensions, local programs, and developer tools with access to your session may still see information you enter.

## Development

```sh
pnpm test
pnpm typecheck
pnpm build
```

`app/` contains the React dashboard, `collector/server.py` contains the Riot client, import job, SQLite store and local API, and `tests/` contains synthetic offline tests. Tests do not contact Riot or use real account records. The original account import has been verified with real data. Profile switching and the new summoner lookup are covered by offline fixtures; a live profile search requires reconnecting your key after this update. Profile changes preserve earlier profiles’ anchor lists and share the match cache.

Riot endpoints used: account-v1 by Riot ID and match-v5 on `americas`, and league-v4 entries and summoner-v4 by PUUID on `na1`. See the [official API reference](https://developer.riotgames.com/apis) and [rate-limit documentation](https://developer.riotgames.com/docs/portal). The collector spaces requests conservatively, observes application/method limits, and handles HTTP 429 `Retry-After` responses.

This repository is local, on branch `main`, with no remote configured.

Queue Lab is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
