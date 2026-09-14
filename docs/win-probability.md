# Win probability: discussion draft

No probability has been implemented. The current difference in average win rates and average observed ranks are descriptive statistics, not predicted odds.

## Question to answer

Given information available before a completed Ranked Solo/Duo match began, how often would a team with these characteristics win? Use all five players on each side, including the profile owner. Keep the existing four-teammate comparison as a separate research measure.

## Candidate inputs

| Input | Candidate representation | Issue to resolve |
| --- | --- | --- |
| Recent win rate | Average of each player's prior-20 win rate, reduced toward a baseline when the sample is small | A 60% win rate in Gold does not imply more strength than 50% in Emerald. Rank and win rate are related. |
| Observed rank | Difference in team average rank and corresponding-role rank gaps | Only snapshots observed before the target match are eligible. Today's rank cannot be used as historical pre-match rank. The equal-division scale is an approximation, not measured skill distance. |
| Off-role play | Count per team, plus fraction of earlier games played in the selected role | A player with 9 Jungle / 8 Mid games differs from a player with 20 Mid / 0 Jungle games; binary tags alone lose that distinction. |
| Champion familiarity | Prior games on the selected champion, optionally paired with role familiarity | Champion-specific win rates from one or two games are unreliable. Avoid counting the same evidence twice. |
| Rank spread / matchup | Within-team variation and matched-role differences | The same average rank can hide very different team compositions. Test whether this adds useful information. |
| Duo queue | Separate manually confirmed labels from repeated co-teaming evidence, with a missing/unknown category | Do not treat suggestions as known premades or absence as solo queue. Any coordination effect must be learned and validated; no fixed win-probability bonus. |
| Side and patch | Blue/red side and patch identifiers | Conditions change over time. Rank distributions and champion balance may shift. |

Account level should initially remain descriptive. It is not evidence of a smurf by itself, and account age proxies can mislead. Do not infer internal MMR.

## How inputs could move the estimate

Start with a simple learned model using differences between teams. An illustrative form is:

`P(win) = sigmoid(intercept + rank contribution + recent-form contribution + role contribution + champion contribution + side contribution)`

The coefficients would be learned from a larger dataset of completed matches. Do not assign an arbitrary “off role costs 5%” or convert an LP gap into a percentage without validation. Rank-dependent relationships and interactions may be necessary; retain them only if they improve performance on later, unseen games.

For initial exploration, use an explicitly labeled scenario score if we want to experiment with hypothetical weights. It must not be displayed as a calibrated probability. A helpful eventual interface would explain which inputs raised or lowered the prediction and show missing-data coverage.

## Data timing is the main constraint

The pilot's match histories are correctly cut off before the anchor. Current rank snapshots, however, are collected after historical matches. They are appropriate for the new observed-rank comparison, but they would leak later information into a purported pre-match probability.

Prospective evaluation needs ranks that were already recorded before a target match, or another authorized historical source. If a player has no eligible snapshot, mark rank missing and evaluate a model designed for that situation, or withhold the estimate. Never silently substitute a later snapshot. Keep snapshot age and coverage visible.

## Validation before displaying odds

Use substantially more than one account's 20 games and sample across the ranks and patches we intend to support. Deduplicate target matches. Train on earlier matches, tune on a separate period, and evaluate on later held-out matches. Account for repeat players and overlapping histories when estimating uncertainty; a random split of related rows would exaggerate confidence.

Compare against a constant 50% baseline and simpler rank-only and recent-win-rate-only models. Check Brier score / log loss and calibration: among matches assigned around 60%, did about 60% actually end in wins? Examine calibration by rank and missing-data coverage, not just aggregate prediction accuracy.

A calibrated predictor could help describe match difficulty. It still could not establish intentional matchmaking manipulation or the rank-versus-internal-MMR hypothesis, because the relevant internal signals and selection mechanism are not observed.
