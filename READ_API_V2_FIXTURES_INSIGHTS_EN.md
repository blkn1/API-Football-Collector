# Read API v2.4 — `/v2/fixtures/insights` (Field-by-field Reference + Interpretation)

This document describes **every field** returned by:

`GET /v2/fixtures/insights?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&include_evidence=false&league_id=<optional>&limit=50&offset=0`

It is written for client developers (frontend, n8n, dashboards) who want a **readable, deterministic** match preview payload and need to understand:
- what each field means
- why some fields are `null`
- how to interpret the output for an upcoming fixture

## 1) Core principles (contract)

### 1.1 DB-only (quota-safe)
This endpoint **does not call API-Football**. It only reads Postgres (`core.*`) and computes deterministic aggregates.

### 1.2 Tracked-only + NS-only
- Only fixtures where `status_short = "NS"` are returned.
- Only fixtures from tracked leagues are returned (`config/jobs/daily.yaml -> tracked_leagues`).

### 1.3 League+season scoped history (no leakage)
For each upcoming (NS) fixture, team history is computed **only from completed fixtures**:
- Status in `FT/AET/PEN` (configurable via `config/read_api_v2.yaml -> fixture_insights.final_statuses`)
- Same **league_id + season**
- Strictly **before kickoff_utc** of the upcoming fixture (no leakage)

### 1.4 Evidence is opt-in (payload size control)
By default, **evidence is disabled**:
- `include_evidence=false` (default): **no `fixtures_sample`** in the payload
- `include_evidence=true`: includes **slim** evidence samples in each context (`fixtures_sample`)

Why: `fixtures_sample` can be large when repeated across multiple contexts, and most clients do not need it.

## 2) Query parameters (strict)

- `date_from` (required, `YYYY-MM-DD`): UTC date start
- `date_to` (required, `YYYY-MM-DD`): UTC date end
- `include_evidence` (optional, boolean, default `false`): whether to include `fixtures_sample`
- `league_id` (optional, int): restrict scope to a single tracked league. If the league is not tracked, the endpoint returns an empty result deterministically.
- `limit` (optional, int, default 50, max 200): **bucket limit** (pagination is applied on `leagues[]`, not on flattened matches).
- `offset` (optional, int, default 0): **bucket offset** (0-based) for `leagues[]`.

If you pass an unknown query param, you get **400**.

## 3) Top-level response envelope

### 3.1 `ok` (boolean)
`true` if the response was generated successfully.

### 3.2 `date_range` (object)
- `from` (date): the UTC start date applied
- `to` (date): the UTC end date applied

### 3.3 `total_match_count` (integer)
Total number of matches across **all kickoff buckets** in the requested date window (and optional league scope).

Important:
- This is **not** the count of matches in the current paginated slice.
- For the slice, use `paging.returned_match_count`.

### 3.4 `leagues` (array)
List of kickoff buckets. **Important**: a league can appear multiple times if it has multiple kickoff times in the date window.

### 3.5 `paging` (object)
Bucket-based pagination info (pagination is applied on `leagues[]`).

Fields:
- `limit` (int): max buckets returned (cap 200)
- `offset` (int): bucket offset (0-based)
- `total_buckets` (int): total bucket count for the scope/date window
- `returned_buckets` (int): number of buckets returned in this response
- `returned_match_count` (int): number of matches returned in this response slice (sum of `leagues[*].match_count`)

### 3.6 `updated_at_utc` (date-time)
When this response was generated (UTC).

## 4) `leagues[]` kickoff bucket object

Each item represents a **(league_id, kickoff_time)** bucket (not “one row per league per day”).

Fields:
- `league_id` (int): league identifier
- `league_name` (string|null): label
- `country_name` (string|null): label
- `season` (int|null): season year for the bucket (expected to match fixtures inside)
- `match_count` (int): number of matches in this bucket (must equal `matches.length`)
- `has_matches` (boolean): always `true` for non-empty buckets (legacy convenience)
- `matches` (array): matches in this kickoff bucket

## 5) `matches[]` base match object

Fields:
- `id` (int): fixture id (`core.fixtures.id`)
- `round` (string|null): round label from API-Football
- `date_utc` (date-time): kickoff time (UTC)
- `timestamp_utc` (int|null): kickoff epoch seconds (UTC)
- `status_short` (string): should be `"NS"` for this endpoint
- `status_long` (string|null): human-readable status
- `home_team_id` / `away_team_id` (int|null): team ids
- `home_team_name` / `away_team_name` (string|null): labels
- `updated_at_utc` (date-time|null): last update timestamp for the fixture row in CORE
- `insights` (object|null): computed insights for this match

### 5.1 When can `insights` be null?
It is expected to be present for normal data. It may become `null` if required fixture metadata is missing (e.g., `season` or team ids).

## 6) `insights` (match-level) object

Fields:
- `league_id` (int): league id for the match
- `season` (int): season year
- `kickoff_utc` (date-time): kickoff time of the current match
- `home_team` (object): team insight block for the home team
- `away_team` (object): team insight block for the away team

Important:
- This block is **not head-to-head**.
- It is “each team’s recent profile” within the **same league+season**.

## 7) Team insight block (`home_team` / `away_team`)

Fields:
- `team_id` (int): team id (`core.teams.id`)
- `team_name` (string|null): label
- `windows` (object):
  - `last5_n` (int): configured last5 window size
  - `last10_n` (int): configured last10 window size
  - `cutoff_utc` (date-time): equals the match kickoff; history is strictly before this time
- `home_context` (object): how this team performs **at home** in this league+season
- `away_context` (object): how this team performs **away** in this league+season
- `selected_context` (string enum: `home|away`):
  - For the fixture home team: `"home"`
  - For the fixture away team: `"away"`
- `selected_indices_0_10` (object|null): convenience copy of the indices from the selected context

### 7.1 Why do we provide both contexts?
Because home/away effects are real (tempo, tactics, travel, referee, game-state). Clients should primarily use:
- home team → `home_context`
- away team → `away_context`

And fall back to the other context only when sample sizes are too small.

## 8) Context block (`home_context` / `away_context`)

Fields:
- `last10` (object): metrics computed on the last10 completed matches in this context
- `last5` (object): metrics computed on the last5 completed matches in this context
- `trends` (object): deltas computed as `last5_avg - last10_avg` for selected stats
- `indices_0_10` (object): normalized scores for this context

## 9) Metrics object (inside `last10` / `last5`)

### 9.1 `played` (int)
Number of completed matches actually found for this context and window.

Example:
- `played: 9` under last10 means only 9 matches were available (window asked for 10).

### 9.2 `results` (object)
- `wins` (int)
- `draws` (int)
- `losses` (int)
- `points` (int): computed as `W=3, D=1, L=0`

### 9.3 `goals` (object)
- `gf` (int): total goals scored by the team in the window
- `ga` (int): total goals conceded by the team in the window
- `gf_avg` (float|null): `gf / played`
- `ga_avg` (float|null): `ga / played`
- `goal_diff_per_match` (float|null): `(gf - ga) / played`
- `clean_sheet_rate_pct` (float|null): percentage of matches where `ga == 0`

Interpretation:
- `gf_avg` is attack volume.
- `ga_avg` is defensive leakiness.
- `goal_diff_per_match` is a compact “net edge”.

### 9.4 `goals_by_half` (object)
Split into:
- `first_half`
- `second_half`

Each half has:
- `gf` / `ga` (int): total goals in that half
- `gf_avg` / `ga_avg` (float|null): per match average in that half (computed over `matches_available`)
- `scored_rate_pct` (float|null): % of matches where team scored at least 1 goal in that half
- `matches_available` (int): matches where half split could be computed

How half split is computed:
1) Prefer `core.fixtures.score.halftime/fulltime` if available and consistent.
2) Fallback to `core.fixture_events` timing if needed (first half = `elapsed <= first_half_max_minute`).

Interpretation tips:
- To answer “does the team score in 1H/2H?” use `scored_rate_pct`.
- To answer “how much does it score in 1H/2H?” use `gf_avg`.
- Always check `matches_available` (small values → weak inference).

### 9.5 `late_goals` (object)
Late goals are derived from events:
- `from_minute` (int): default 76 (config)
- `goals_for` / `goals_against` (int): total late goals scored/conceded
- `scored_rate_pct` (float|null): % of matches where team scored at least one late goal
- `matches_available` (int): matches where event data existed for this fixture

Interpretation:
- High `scored_rate_pct` → “late push” / “finishing strong” profile.
- High `goals_against` late → “late fragility”.

### 9.6 `match_stats_avg` (object)
This is derived from `core.fixture_statistics` (team-level match statistics). Fields:
- `total_shots` (float|null)
- `shots_on_goal` (float|null)
- `corner_kicks` (float|null)
- `offsides` (float|null)
- `yellow_cards` (float|null)
- `red_cards` (float|null)
- `corners_against` (float|null)
- `offsides_against` (float|null)

Nullability rules (most common source of confusion):
- If the collector did not ingest `/fixtures/statistics` for the history fixtures, then **all of these may be null**.
- `corners_against` / `offsides_against` require the opponent’s stats row in the same fixture too; if only one side exists, these become null.

Interpretation:
- Think of these as “pressure/discipline proxies”, not ground truth tactics.
- Use them to complement goals-based indicators when they are present.

### 9.7 `opponent_strength` (object)
Fields:
- `matches_available` (int): number of matches in the window where opponent form was available
- `avg_points_last5` (float|null): average opponent “form points” computed from `core.team_statistics.form` (last 5: W=3, D=1, L=0, range 0..15)

What it is:
- A **schedule difficulty proxy** for the matches in the window.

What it is not:
- It is **not head-to-head** between the two teams.
- It does not say anything about “these two teams played each other”.

How it helps for the upcoming match:
- It helps you interpret whether a team’s last5/last10 performance came against stronger or weaker opponents.
  - Example: high `gf_avg` with low `avg_points_last5` can be “easy schedule inflation”.
  - Example: modest `gf_avg` with high `avg_points_last5` can be “performance against strong opposition”.

### 9.8 `derived` (object)
Fields:
- `win_streak` (int): consecutive wins from the most recent match backwards within this window/context
- `second_half_goal_diff_per_match` (float|null): `(2H_gf - 2H_ga) / 2H_matches_available`
- `form_points_last5` (int|null): points if `played <= 5` (otherwise null)

Interpretation:
- `win_streak` is a simple momentum signal.
- `second_half_goal_diff_per_match` contributes to “winning_drive” (motivation).

### 9.9 `fixtures_sample` (array, evidence-only)
Only present when `include_evidence=true`.

Each element is a slim evidence row:
- `id` (int): fixture id
- `date_utc` (date-time|null): fixture date
- `league_id` (int)
- `season` (int)
- `opponent_team_id` (int)
- `is_home` (boolean): whether the subject team was home in that history fixture
- `gf` (int): goals for (subject team)
- `ga` (int): goals against (subject team)

This is intended to:
- validate that history selection (league+season, context, cutoff) is correct
- debug anomalies / unexpected aggregates

## 10) `trends` block
Currently:
- `corner_kicks_avg_delta`
- `yellow_cards_avg_delta`
- `red_cards_avg_delta`

Each is computed as:
`delta = last5_avg - last10_avg`

Interpretation:
- Positive delta → recent matches show higher average than the longer window.
- If `match_stats_avg` is null in both windows, trends will be null.

## 11) `indices_0_10` block (normalized scores)
Fields:
- `attack_strength` (0..10 or null)
- `defensive_solidity` (0..10 or null)
- `recent_form` (0..10 or null)
- `winning_drive` (0..10 or null)
- `warnings` (array of strings)

Key points:
- These are **clamp-linear normalized** scores based on config ranges in `config/read_api_v2.yaml -> fixture_insights.normalization`.
- Null means the score couldn’t be computed (missing inputs) or sample size is insufficient.
- `warnings` communicates stability concerns (e.g. small sample sizes).

## 12) Worked interpretation using your examples

### 12.1 Indonesia (Liga 1) example: match_stats mostly null
Example fixture:
- Arema FC (home) vs Persijap (away)

Observed:
- `match_stats_avg.*` is null for both teams/contexts.

What it means:
- You likely do **not** have `core.fixture_statistics` coverage for those history fixtures (either league doesn’t provide, or collector hasn’t ingested it).
- You should interpret the match primarily using:
  - `goals.gf_avg / ga_avg`
  - `goals_by_half.*.scored_rate_pct` (1H/2H scoring likelihood)
  - `late_goals.scored_rate_pct`
  - `indices_0_10` (already computed from goals + available signals)

How to read Arema (home_team, selected_context=home):
- `home_context.last10.played = 9`: sample is decent.
- 1H scoring likelihood: `first_half.scored_rate_pct ≈ 55.6%` (scores in 1H in ~5/9).
- 2H scoring likelihood: `second_half.scored_rate_pct ≈ 66.7%` (scores more often in 2H).
- Late goals: `scored_rate_pct ≈ 55.6%` (frequent late scoring, but also concedes late).
- `opponent_strength.avg_points_last5 ≈ 7.44`: opponents were medium difficulty; goals are not purely “easy schedule”.

How to read Persijap (away_team, selected_context=away):
- Away context looks weak (e.g. high `ga_avg`, low `points`).
- `opponent_strength.avg_points_last5` is high (~8.9 / 9.2 in your sample), meaning they faced strong opponents.
  - This can partially explain poor results, but the defensive numbers are still alarming.

Conclusion style (without match_stats):
- This match looks like “Arema has higher scoring frequency (especially 2H) and Persijap is leaky away”.
- Use indices as a summary, but verify with `played` and half scoring rates.

### 12.2 Saudi (Pro League) example: rich match_stats + meaningful trends
Example fixture:
- Al Riyadh (home) vs Al-Nassr (away)

Observed:
- `match_stats_avg` is populated (shots, corners, cards, offsides).

How to use match_stats_avg here:
- Compare pressure proxies in the **selected contexts**:\n  - Home team’s home_context: corners ~6.25, shots_on_goal ~3.5\n  - Away team’s away_context: high shots, corners, offsides, etc.\n- Use `corners_against` to see who tends to be under pressure.\n- Use `yellow_cards/red_cards` for discipline risk and game-state volatility.\n\nHow to read `trends`:\n- For Al Riyadh home_context, `corner_kicks_avg_delta` positive means recent games had higher corner output than longer window (recent pressure increase).\n- Card deltas can suggest recent discipline change.\n\n`opponent_strength` interpretation:\n- Some Al Riyadh contexts show `avg_points_last5` very high (e.g. ~10), meaning recent matches were against strong opponents.\n- This matters when you compare raw goals: a mediocre `gf_avg` against strong opposition can be “better than it looks”.\n\nConclusion style (with rich stats):\n- You can frame a narrative: Al-Nassr tends to generate high shot volume and late goals; Al Riyadh concedes more away, but home stats are somewhat better; opponent strength explains some variance.\n\n## 13) Practical client reading order (recommended)\n1) Pick the right context:\n   - home team → `home_context`\n   - away team → `away_context`\n2) Check sample size:\n   - `last10.played`, `matches_available`\n3) Read indices summary:\n   - `selected_indices_0_10` and `warnings`\n4) Support with metrics:\n   - half scoring rates (`scored_rate_pct`)\n   - `ga_avg` + clean sheets\n   - late goals\n5) If match_stats_avg exists, use it as extra signal (pressure/discipline), otherwise ignore.\n6) If something looks suspicious, re-run with `include_evidence=true` and inspect `fixtures_sample`.\n+
