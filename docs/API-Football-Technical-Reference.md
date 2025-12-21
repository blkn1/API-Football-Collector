# API-FOOTBALL v3 - Technical Reference Documentation

**Version:** 3.9.3  
**Base URL:** `https://v3.football.api-sports.io/`  
**Authentication:** Header `x-apisports-key: YOUR_API_KEY`  
**Method:** GET only (POST/PUT/DELETE not supported)  
**Plan:** Pro (7,500 requests/day, ~300 requests/minute)

---

## Quick Reference

### Critical Constraints
- ✅ **GET requests only** - Other methods return error
- ✅ **Single header only** - `x-apisports-key` (extra headers cause errors)
- ✅ **Rate limits:** 7,500/day, ~300/minute (firewall block risk if exceeded)
- ✅ **Response headers:** Always read `x-ratelimit-requests-remaining` and `X-RateLimit-Remaining`
- ✅ **Standard envelope:** All responses follow `{get, parameters, errors, results, paging, response}` structure
- ✅ **`/status` endpoint:** Does NOT count toward daily quota

### Rate Limit Headers (Returned on Every Response)
```
x-ratelimit-requests-limit: 7500          # Daily quota
x-ratelimit-requests-remaining: 6843      # Remaining daily requests
X-RateLimit-Limit: 300                    # Per-minute limit
X-RateLimit-Remaining: 287                # Remaining per-minute requests
```

### Standard Response Envelope
```json
{
  "get": "fixtures",
  "parameters": {"league": "39", "season": "2024"},
  "errors": [],
  "results": 380,
  "paging": {"current": 1, "total": 10},
  "response": [ /* array of domain objects */ ]
}
```

**Important:** Always check `errors` array even when status is 200. Partial failures are possible.

---

## Endpoint Catalog

### Static Data (Cache: 30+ days)

#### 1. Timezones
**Endpoint:** `GET /timezone`  
**Parameters:** None  
**Purpose:** List of valid timezone strings for fixture queries  
**Update Frequency:** Static (call once)  
**Cache Strategy:** Long-term (30+ days)

**Example:**
```bash
GET /timezone
```

**Response:**
```json
{
  "response": ["Africa/Algiers", "America/New_York", "Europe/Istanbul", ...]
}
```

---

#### 2. Countries
**Endpoint:** `GET /countries`  
**Parameters:**
- `name` (string): Filter by country name
- `code` (string): ISO 2-letter code (e.g., "TR", "GB")
- `search` (string): Fuzzy search

**Purpose:** Country catalog with ISO codes and flags  
**Update Frequency:** Rarely changes  
**Cache Strategy:** 30+ days

**Example:**
```bash
GET /countries?code=TR
```

**Response:**
```json
{
  "response": [
    {
      "name": "Turkey",
      "code": "TR",
      "flag": "https://media.api-sports.io/flags/tr.svg"
    }
  ]
}
```

---

### League & Season Data (Cache: 7-30 days)

#### 3. Leagues
**Endpoint:** `GET /leagues`  
**Parameters:**
- `id` (integer): League ID
- `name` (string): League name
- `country` (string): Country name
- `code` (string): Country code
- `season` (integer): Year (e.g., 2024)
- `type` (string): "league" or "cup"
- `current` (boolean): true/false
- `search` (string): Fuzzy search

**Purpose:** League catalog with seasons and coverage info  
**Update Frequency:** Season start/end  
**Cache Strategy:** 7-30 days

**Important Rules:**
- If `season` not provided, use `current=true` to get active leagues
- Use `/leagues/seasons` to get available seasons for a league

**Example:**
```bash
GET /leagues?country=England&season=2024
```

**Response:**
```json
{
  "response": [
    {
      "league": {
        "id": 39,
        "name": "Premier League",
        "type": "League",
        "logo": "https://media.api-sports.io/football/leagues/39.png"
      },
      "country": {
        "name": "England",
        "code": "GB",
        "flag": "https://media.api-sports.io/flags/gb.svg"
      },
      "seasons": [
        {
          "year": 2024,
          "start": "2024-08-16",
          "end": "2025-05-18",
          "current": true,
          "coverage": { /* coverage details */ }
        }
      ]
    }
  ]
}
```

---

#### 4. Leagues Seasons
**Endpoint:** `GET /leagues/seasons`  
**Parameters:** None  
**Purpose:** List of all available seasons across all leagues  
**Update Frequency:** Yearly  
**Cache Strategy:** 365 days

**Example:**
```bash
GET /leagues/seasons
```

**Response:**
```json
{
  "response": [2010, 2011, 2012, ..., 2024, 2025]
}
```

---

#### 5. Fixtures Rounds
**Endpoint:** `GET /fixtures/rounds`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**
- `current` (boolean): true/false

**Purpose:** List of rounds for a league/season (e.g., "Regular Season - 14")  
**Update Frequency:** Weekly  
**Cache Strategy:** 7 days

**Example:**
```bash
GET /fixtures/rounds?league=39&season=2024&current=true
```

**Response:**
```json
{
  "response": ["Regular Season - 14"]
}
```

---

### Team Data (Cache: 7-30 days)

#### 6. Teams
**Endpoint:** `GET /teams`  
**Parameters:**
- `id` (integer): Team ID
- `name` (string): Team name
- `league` (integer): League ID
- `season` (integer): Season year
- `country` (string): Country name
- `code` (string): Team code (3 letters)
- `venue` (integer): Venue ID
- `search` (string): Fuzzy search

**Purpose:** Team catalog with venue and founding info  
**Update Frequency:** Season start  
**Cache Strategy:** 30 days

**Example:**
```bash
GET /teams?league=39&season=2024
```

**Response:**
```json
{
  "response": [
    {
      "team": {
        "id": 33,
        "name": "Manchester United",
        "code": "MUN",
        "country": "England",
        "founded": 1878,
        "national": false,
        "logo": "https://media.api-sports.io/football/teams/33.png"
      },
      "venue": {
        "id": 556,
        "name": "Old Trafford",
        "address": "Sir Matt Busby Way",
        "city": "Manchester",
        "capacity": 76212,
        "surface": "grass",
        "image": "https://media.api-sports.io/football/venues/556.png"
      }
    }
  ]
}
```

---

#### 7. Teams Statistics
**Endpoint:** `GET /teams/statistics`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**
- `team` (integer): **REQUIRED**
- `date` (YYYY-MM-DD): Optional, stats up to this date

**Purpose:** Aggregated team stats for a league/season  
**Update Frequency:** Daily  
**Cache Strategy:** 1 day

**Critical Rule:** All 3 parameters (league, season, team) are REQUIRED. Do NOT call without resolving team ID first.

**Example:**
```bash
GET /teams/statistics?league=39&season=2024&team=33
```

**Response:**
```json
{
  "response": {
    "league": { /* league info */ },
    "team": { /* team info */ },
    "form": "WWDLL",
    "fixtures": {
      "played": { "home": 15, "away": 14, "total": 29 },
      "wins": { "home": 10, "away": 8, "total": 18 },
      "draws": { "home": 3, "away": 3, "total": 6 },
      "loses": { "home": 2, "away": 3, "total": 5 }
    },
    "goals": {
      "for": { "total": { "home": 32, "away": 28, "total": 60 } },
      "against": { "total": { "home": 12, "away": 18, "total": 30 } }
    },
    /* ... more stats ... */
  }
}
```

---

#### 8. Teams Seasons
**Endpoint:** `GET /teams/seasons`  
**Parameters:**
- `team` (integer): **REQUIRED**

**Purpose:** List of seasons a team has participated in  
**Update Frequency:** Yearly  
**Cache Strategy:** 365 days

**Example:**
```bash
GET /teams/seasons?team=33
```

**Response:**
```json
{
  "response": [2010, 2011, 2012, ..., 2024]
}
```

---

#### 9. Teams Countries
**Endpoint:** `GET /teams/countries`  
**Parameters:** None

**Purpose:** List of countries that have teams in the API  
**Update Frequency:** Rarely  
**Cache Strategy:** 30 days

---

#### 10. Venues
**Endpoint:** `GET /venues`  
**Parameters:**
- `id` (integer): Venue ID
- `name` (string): Venue name
- `city` (string): City name
- `country` (string): Country name
- `search` (string): Fuzzy search

**Purpose:** Stadium/venue details  
**Update Frequency:** Rarely  
**Cache Strategy:** 30 days

**Example:**
```bash
GET /venues?id=556
```

**Response:**
```json
{
  "response": [
    {
      "id": 556,
      "name": "Old Trafford",
      "address": "Sir Matt Busby Way",
      "city": "Manchester",
      "country": "England",
      "capacity": 76212,
      "surface": "grass",
      "image": "https://media.api-sports.io/football/venues/556.png"
    }
  ]
}
```

---

### Fixture Data (Cache: 15 seconds for live, 1 day for completed)

#### 11. Fixtures
**Endpoint:** `GET /fixtures`  
**Parameters:**
- `id` (integer): Single fixture ID
- `ids` (string): Multiple IDs (max 20, comma-separated)
- `live` (string): "all" or "leagueId-leagueId"
- `date` (YYYY-MM-DD): Fixtures on this date
- `league` (integer): League ID
- `season` (integer): Season year
- `team` (integer): Team ID
- `last` (integer): Last N fixtures (max 99)
- `next` (integer): Next N fixtures (max 99)
- `from` (YYYY-MM-DD): Date range start
- `to` (YYYY-MM-DD): Date range end
- `round` (string): Round name (e.g., "Regular Season - 14")
- `status` (string): Match status (NS, 1H, HT, 2H, FT, etc.)
- `venue` (integer): Venue ID
- `timezone` (string): Timezone for date/time display

**Purpose:** Core fixture feed (pre-match, live, post-match)  
**Update Frequency:** 15 seconds (live), hourly (scheduled), daily (completed)  
**Cache Strategy:** 15 seconds (live), 1 hour (today), 1 day (completed)

**Critical Rules:**
- **MAX 20 IDs** in `ids` parameter
- Use `live=all` for ALL live matches (most efficient)
- `status` values: TBD, NS, 1H, HT, 2H, ET, BT, P, FT, AET, PEN, PST, CANC, ABD, AWD, WO, LIVE, SUSP, INT
- Default timezone is UTC; use `timezone` parameter for user display

**Example: Get today's fixtures for Premier League**
```bash
GET /fixtures?date=2024-12-12&league=39&timezone=Europe/Istanbul
```

**Example: Get all live matches**
```bash
GET /fixtures?live=all
```

**Response:**
```json
{
  "response": [
    {
      "fixture": {
        "id": 1234567,
        "referee": "Michael Oliver",
        "timezone": "UTC",
        "date": "2024-12-12T20:00:00+00:00",
        "timestamp": 1702411200,
        "periods": {
          "first": 1702411200,
          "second": 1702414800
        },
        "venue": {
          "id": 556,
          "name": "Old Trafford",
          "city": "Manchester"
        },
        "status": {
          "long": "Match Finished",
          "short": "FT",
          "elapsed": 90
        }
      },
      "league": {
        "id": 39,
        "name": "Premier League",
        "country": "England",
        "logo": "...",
        "flag": "...",
        "season": 2024,
        "round": "Regular Season - 14"
      },
      "teams": {
        "home": {
          "id": 33,
          "name": "Manchester United",
          "logo": "...",
          "winner": true
        },
        "away": {
          "id": 34,
          "name": "Newcastle",
          "logo": "...",
          "winner": false
        }
      },
      "goals": {
        "home": 2,
        "away": 1
      },
      "score": {
        "halftime": { "home": 1, "away": 0 },
        "fulltime": { "home": 2, "away": 1 },
        "extratime": { "home": null, "away": null },
        "penalty": { "home": null, "away": null }
      }
    }
  ]
}
```

---

#### 12. Fixtures Head to Head
**Endpoint:** `GET /fixtures/headtohead`  
**Parameters:**
- `h2h` (string): **REQUIRED** Format: "teamId-teamId" (e.g., "33-34")
- `league` (integer): Optional filter
- `season` (integer): Optional filter
- `last` (integer): Last N matches (max 99)
- `next` (integer): Next N matches (max 99)
- `from` (YYYY-MM-DD): Date range start
- `to` (YYYY-MM-DD): Date range end
- `status` (string): Match status
- `venue` (integer): Venue ID
- `timezone` (string): Timezone

**Purpose:** Historical head-to-head matches between two teams  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

**Critical Rule:** `h2h` parameter format MUST be "teamId-teamId". Do NOT call without resolving team IDs first.

**Example:**
```bash
GET /fixtures/headtohead?h2h=33-34&last=10
```

---

#### 13. Fixtures Statistics
**Endpoint:** `GET /fixtures/statistics`  
**Parameters:**
- `fixture` (integer): **REQUIRED**
- `team` (integer): Optional (home or away)
- `type` (string): Stat type filter

**Purpose:** Match-level statistics (shots, possession, corners, etc.)  
**Update Frequency:** Real-time during match, final after FT  
**Cache Strategy:** 1 minute (live), permanent (FT)

**Critical Rules:**
- Requires completed or live fixture
- Returns stats for BOTH teams if `team` not specified
- Heavy data - only fetch when user requests details

**Example:**
```bash
GET /fixtures/statistics?fixture=1234567
```

**Response:**
```json
{
  "response": [
    {
      "team": {
        "id": 33,
        "name": "Manchester United",
        "logo": "..."
      },
      "statistics": [
        { "type": "Shots on Goal", "value": 8 },
        { "type": "Shots off Goal", "value": 4 },
        { "type": "Total Shots", "value": 12 },
        { "type": "Ball Possession", "value": "58%" },
        { "type": "Corner Kicks", "value": 6 },
        { "type": "Offsides", "value": 2 },
        { "type": "Fouls", "value": 11 },
        { "type": "Yellow Cards", "value": 2 },
        { "type": "Red Cards", "value": 0 },
        { "type": "Goalkeeper Saves", "value": 3 },
        { "type": "Total passes", "value": 487 },
        { "type": "Passes accurate", "value": 412 },
        { "type": "Passes %", "value": "85%" }
      ]
    },
    { /* away team stats */ }
  ]
}
```

---

#### 14. Fixtures Events
**Endpoint:** `GET /fixtures/events`  
**Parameters:**
- `fixture` (integer): **REQUIRED**
- `team` (integer): Optional filter
- `player` (integer): Optional filter
- `type` (string): Event type filter (Goal, Card, subst, Var)

**Purpose:** Timeline events (goals, cards, substitutions, VAR)  
**Update Frequency:** Real-time during match  
**Cache Strategy:** 1 minute (live), permanent (FT)

**Example:**
```bash
GET /fixtures/events?fixture=1234567
```

**Response:**
```json
{
  "response": [
    {
      "time": {
        "elapsed": 23,
        "extra": null
      },
      "team": {
        "id": 33,
        "name": "Manchester United",
        "logo": "..."
      },
      "player": {
        "id": 882,
        "name": "Bruno Fernandes"
      },
      "assist": {
        "id": 2935,
        "name": "Marcus Rashford"
      },
      "type": "Goal",
      "detail": "Normal Goal",
      "comments": null
    },
    {
      "time": { "elapsed": 67, "extra": null },
      "team": { "id": 34, "name": "Newcastle", "logo": "..." },
      "player": { "id": 19146, "name": "Kieran Trippier" },
      "assist": { "id": null, "name": null },
      "type": "Card",
      "detail": "Yellow Card",
      "comments": "Foul"
    }
  ]
}
```

---

#### 15. Fixtures Lineups
**Endpoint:** `GET /fixtures/lineups`  
**Parameters:**
- `fixture` (integer): **REQUIRED**
- `team` (integer): Optional filter
- `player` (integer): Optional filter
- `type` (string): "formation" or "startXI" or "substitutes"

**Purpose:** Confirmed lineups, formations, substitutes  
**Update Frequency:** 1 hour before kickoff, updated during match  
**Cache Strategy:** 1 hour (pre-match), 1 minute (live), permanent (FT)

**Critical Rule:** Lineups are typically available 1 hour before kickoff. Returns empty before then.

**Example:**
```bash
GET /fixtures/lineups?fixture=1234567
```

**Response:**
```json
{
  "response": [
    {
      "team": {
        "id": 33,
        "name": "Manchester United",
        "logo": "...",
        "colors": {
          "player": { "primary": "FF0000", "number": "FFFFFF", "border": "FF0000" },
          "goalkeeper": { "primary": "1E8449", "number": "FFFFFF", "border": "1E8449" }
        }
      },
      "formation": "4-2-3-1",
      "startXI": [
        {
          "player": {
            "id": 882,
            "name": "Bruno Fernandes",
            "number": 8,
            "pos": "M",
            "grid": "3:2"
          }
        }
        /* ... 10 more players ... */
      ],
      "substitutes": [
        {
          "player": {
            "id": 18950,
            "name": "Mason Mount",
            "number": 7,
            "pos": "M",
            "grid": null
          }
        }
        /* ... more substitutes ... */
      ],
      "coach": {
        "id": 4,
        "name": "Erik ten Hag",
        "photo": "..."
      }
    },
    { /* away team lineup */ }
  ]
}
```

---

#### 16. Fixtures Players
**Endpoint:** `GET /fixtures/players`  
**Parameters:**
- `fixture` (integer): **REQUIRED**
- `team` (integer): Optional filter

**Purpose:** Player-level statistics for a match (minutes played, goals, assists, rating)  
**Update Frequency:** Real-time during match  
**Cache Strategy:** 1 minute (live), permanent (FT)

**Example:**
```bash
GET /fixtures/players?fixture=1234567
```

**Response:**
```json
{
  "response": [
    {
      "team": {
        "id": 33,
        "name": "Manchester United",
        "logo": "...",
        "update": "2024-12-12T22:00:00+00:00"
      },
      "players": [
        {
          "player": {
            "id": 882,
            "name": "Bruno Fernandes",
            "photo": "..."
          },
          "statistics": [
            {
              "games": {
                "minutes": 90,
                "number": 8,
                "position": "M",
                "rating": "8.5",
                "captain": true,
                "substitute": false
              },
              "offsides": 0,
              "shots": { "total": 3, "on": 2 },
              "goals": { "total": 1, "conceded": 0, "assists": 1, "saves": null },
              "passes": { "total": 67, "key": 4, "accuracy": "89%" },
              "tackles": { "total": 2, "blocks": 0, "interceptions": 1 },
              "duels": { "total": 12, "won": 8 },
              "dribbles": { "attempts": 3, "success": 2, "past": null },
              "fouls": { "drawn": 2, "committed": 1 },
              "cards": { "yellow": 0, "red": 0 },
              "penalty": { "won": null, "commited": null, "scored": 0, "missed": 0, "saved": null }
            }
          ]
        }
        /* ... more players ... */
      ]
    },
    { /* away team players */ }
  ]
}
```

---

### Standing Data (Cache: 1 day)

#### 17. Standings
**Endpoint:** `GET /standings`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**
- `team` (integer): Optional filter

**Purpose:** League table with points, goals, form  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day (or after matches in this league)

**Critical Rule:** Response is nested: `response[0].league.standings[0]` is the table array.

**Example:**
```bash
GET /standings?league=39&season=2024
```

**Response:**
```json
{
  "response": [
    {
      "league": {
        "id": 39,
        "name": "Premier League",
        "country": "England",
        "logo": "...",
        "flag": "...",
        "season": 2024,
        "standings": [
          [
            {
              "rank": 1,
              "team": {
                "id": 42,
                "name": "Arsenal",
                "logo": "..."
              },
              "points": 40,
              "goalsDiff": 28,
              "group": "Premier League",
              "form": "WWDWW",
              "status": "same",
              "description": "Promotion - Champions League (Group Stage: )",
              "all": {
                "played": 15,
                "win": 12,
                "draw": 4,
                "lose": 1,
                "goals": { "for": 38, "against": 10 }
              },
              "home": {
                "played": 8,
                "win": 6,
                "draw": 2,
                "lose": 0,
                "goals": { "for": 20, "against": 4 }
              },
              "away": {
                "played": 7,
                "win": 6,
                "draw": 2,
                "lose": 1,
                "goals": { "for": 18, "against": 6 }
              },
              "update": "2024-12-12T00:00:00+00:00"
            }
            /* ... 19 more teams ... */
          ]
        ]
      }
    }
  ]
}
```

---

### Player Data (Cache: 7-30 days)

#### 18. Players
**Endpoint:** `GET /players`  
**Parameters:**
- `id` (integer): Player ID
- `team` (integer): Team ID
- `league` (integer): League ID
- `season` (integer): **REQUIRED** (even for search)
- `search` (string): Player name (min 4 chars)
- `page` (integer): Page number (pagination)

**Purpose:** Player season statistics  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

**Critical Rules:**
- `season` is REQUIRED even for search queries
- Results are paginated (50 per page)
- Use `search` for name lookup (min 4 characters)

**Example:**
```bash
GET /players?team=33&season=2024&page=1
```

**Response:**
```json
{
  "response": [
    {
      "player": {
        "id": 882,
        "name": "Bruno Fernandes",
        "firstname": "Bruno Miguel",
        "lastname": "Borges Fernandes",
        "age": 29,
        "birth": {
          "date": "1994-09-08",
          "place": "Maia",
          "country": "Portugal"
        },
        "nationality": "Portugal",
        "height": "179 cm",
        "weight": "69 kg",
        "injured": false,
        "photo": "..."
      },
      "statistics": [
        {
          "team": {
            "id": 33,
            "name": "Manchester United",
            "logo": "..."
          },
          "league": {
            "id": 39,
            "name": "Premier League",
            "country": "England",
            "logo": "...",
            "flag": "...",
            "season": 2024
          },
          "games": {
            "appearences": 15,
            "lineups": 15,
            "minutes": 1350,
            "number": 8,
            "position": "Midfielder",
            "rating": "7.8",
            "captain": true
          },
          "substitutes": {
            "in": 0,
            "out": 2,
            "bench": 0
          },
          "shots": {
            "total": 42,
            "on": 23
          },
          "goals": {
            "total": 5,
            "conceded": 0,
            "assists": 8,
            "saves": null
          },
          "passes": {
            "total": 987,
            "key": 34,
            "accuracy": 82
          },
          "tackles": {
            "total": 28,
            "blocks": 3,
            "interceptions": 12
          },
          "duels": {
            "total": 156,
            "won": 89
          },
          "dribbles": {
            "attempts": 45,
            "success": 28,
            "past": null
          },
          "fouls": {
            "drawn": 18,
            "committed": 23
          },
          "cards": {
            "yellow": 3,
            "yellowred": 0,
            "red": 0
          },
          "penalty": {
            "won": null,
            "commited": null,
            "scored": 2,
            "missed": 0,
            "saved": null
          }
        }
      ]
    }
  ]
}
```

---

#### 19. Players Profiles
**Endpoint:** `GET /players/profiles`  
**Parameters:**
- `id` (integer): Player ID
- `search` (string): Player name (min 4 chars)

**Purpose:** Static player profile and career history  
**Update Frequency:** Rarely (transfers, injuries)  
**Cache Strategy:** 30 days

**Example:**
```bash
GET /players/profiles?id=882
```

---

#### 20. Players Squads
**Endpoint:** `GET /players/squads`  
**Parameters:**
- `team` (integer): **REQUIRED**

**Purpose:** Current squad list for a team  
**Update Frequency:** After transfers  
**Cache Strategy:** 7 days

**Critical Rule:** Season-independent. Returns current squad.

**Example:**
```bash
GET /players/squads?team=33
```

---

#### 21. Players Seasons
**Endpoint:** `GET /players/seasons`  
**Parameters:**
- `player` (integer): **REQUIRED**

**Purpose:** List of seasons a player has data for  
**Update Frequency:** Yearly  
**Cache Strategy:** 365 days

**Example:**
```bash
GET /players/seasons?player=882
```

**Response:**
```json
{
  "response": [2015, 2016, 2017, ..., 2024]
}
```

---

#### 22. Players Top Scorers
**Endpoint:** `GET /players/topscorers`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**

**Purpose:** Goal scoring leaderboard  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

**Critical Rule:** Both `league` and `season` are REQUIRED.

**Example:**
```bash
GET /players/topscorers?league=39&season=2024
```

---

#### 23. Players Top Assists
**Endpoint:** `GET /players/topassists`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**

**Purpose:** Assist leaderboard  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

---

#### 24. Players Top Yellow Cards
**Endpoint:** `GET /players/topyellowcards`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**

**Purpose:** Yellow card leaderboard  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

---

#### 25. Players Top Red Cards
**Endpoint:** `GET /players/topredcards`  
**Parameters:**
- `league` (integer): **REQUIRED**
- `season` (integer): **REQUIRED**

**Purpose:** Red card leaderboard  
**Update Frequency:** After each match  
**Cache Strategy:** 1 day

---

### Injury & Transfer Data (Cache: 1-7 days)

#### 26. Injuries
**Endpoint:** `GET /injuries`  
**Parameters:**
- `fixture` (integer): Fixture ID
- `league` (integer): League ID
- `season` (integer): Season year
- `team` (integer): Team ID
- `player` (integer): Player ID
- `date` (YYYY-MM-DD): Date filter
- `timezone` (string): Timezone

**Purpose:** Injury and suspension feed  
**Update Frequency:** Daily  
**Cache Strategy:** 1 day

**Example:**
```bash
GET /injuries?league=39&season=2024
```

---

#### 27. Transfers
**Endpoint:** `GET /transfers`  
**Parameters:**
- `player` (integer): Player ID
- `team` (integer): Team ID

**Purpose:** Transfer history  
**Update Frequency:** After transfer windows  
**Cache Strategy:** 7 days

**Example:**
```bash
GET /transfers?player=882
```

---

#### 28. Sidelined
**Endpoint:** `GET /sidelined`  
**Parameters:**
- `player` (integer): Player ID
- `coach` (integer): Coach ID

**Purpose:** Temporary absences (injuries, suspensions)  
**Update Frequency:** Daily  
**Cache Strategy:** 1 day

---

#### 29. Trophies
**Endpoint:** `GET /trophies`  
**Parameters:**
- `player` (integer): Player ID
- `coach` (integer): Coach ID

**Purpose:** Career trophy cabinet  
**Update Frequency:** After competition finals  
**Cache Strategy:** 30 days

---

### Coach Data (Cache: 30 days)

#### 30. Coaches
**Endpoint:** `GET /coachs`  
**Parameters:**
- `id` (integer): Coach ID
- `team` (integer): Team ID
- `search` (string): Coach name

**Purpose:** Coach profiles and current team  
**Update Frequency:** After appointments  
**Cache Strategy:** 30 days

---

### Odds Data (Cache: 15 seconds for live, 1 day for pre-match)

#### 31. Odds
**Endpoint:** `GET /odds`  
**Parameters:**
- `fixture` (integer): Fixture ID
- `league` (integer): League ID
- `season` (integer): Season year
- `date` (YYYY-MM-DD): Date filter
- `bookmaker` (integer): Bookmaker ID
- `bet` (integer): Bet type ID
- `page` (integer): Page number

**Purpose:** Pre-match odds history  
**Update Frequency:** Hourly before match  
**Cache Strategy:** 1 hour (pre-match), permanent (after kickoff)

**CRITICAL RULE:** NEVER call `/odds` without filters. ALWAYS use `league`, `season`, or `date`.

**Example:**
```bash
GET /odds?fixture=1234567&bookmaker=8
```

---

#### 32. Odds Live
**Endpoint:** `GET /odds/live`  
**Parameters:**
- `fixture` (integer): Fixture ID
- `league` (integer): League ID
- `bet` (integer): Bet type ID

**Purpose:** Real-time odds during match  
**Update Frequency:** Real-time (15 seconds)  
**Cache Strategy:** 15 seconds

**CRITICAL RULE:** High-frequency endpoint. Implement strict rate limiting.

---

#### 33. Odds Live Bets
**Endpoint:** `GET /odds/live/bets`  
**Parameters:**
- `id` (integer): Bet ID
- `search` (string): Bet name

**Purpose:** Available bet types for live odds  
**Update Frequency:** Static  
**Cache Strategy:** 30 days

---

#### 34. Odds Bookmakers
**Endpoint:** `GET /odds/bookmakers`  
**Parameters:**
- `id` (integer): Bookmaker ID
- `search` (string): Bookmaker name

**Purpose:** Bookmaker directory  
**Update Frequency:** Rarely  
**Cache Strategy:** 30 days

---

#### 35. Odds Bets
**Endpoint:** `GET /odds/bets`  
**Parameters:**
- `id` (integer): Bet ID
- `search` (string): Bet name

**Purpose:** Bet types metadata  
**Update Frequency:** Rarely  
**Cache Strategy:** 30 days

---

#### 36. Odds Mapping
**Endpoint:** `GET /odds/mapping`  
**Parameters:**
- `fixture` (integer): Fixture ID
- `bookmaker` (integer): Bookmaker ID
- `page` (integer): Page number

**Purpose:** Fixture to bookmaker mapping  
**Update Frequency:** Hourly  
**Cache Strategy:** 1 hour

---

### Prediction Data (Cache: 1 day)

#### 37. Predictions
**Endpoint:** `GET /predictions`  
**Parameters:**
- `fixture` (integer): **REQUIRED**

**Purpose:** ML-generated match predictions  
**Update Frequency:** Daily before match  
**Cache Strategy:** 1 day

**Critical Rule:** Only available for supported fixtures. Check API coverage.

**Example:**
```bash
GET /predictions?fixture=1234567
```

**Response:**
```json
{
  "response": [
    {
      "predictions": {
        "winner": {
          "id": 33,
          "name": "Manchester United",
          "comment": "Win or draw"
        },
        "win_or_draw": true,
        "under_over": "Over 2.5",
        "goals": {
          "home": "2.0",
          "away": "1.0"
        },
        "advice": "Combo Double chance : Manchester United or draw and +2.5 goals",
        "percent": {
          "home": "50%",
          "draw": "25%",
          "away": "25%"
        }
      },
      "league": { /* league info */ },
      "teams": {
        "home": { /* team stats, last 5, form */ },
        "away": { /* team stats, last 5, form */ }
      },
      "comparison": { /* head-to-head stats */ }
    }
  ]
}
```

---

## Match Status Codes

| Code | Long Name | Description | Update Strategy |
|------|-----------|-------------|-----------------|
| TBD | Time To Be Defined | Date not set | Check weekly |
| NS | Not Started | Scheduled, not started | Check hourly on match day |
| 1H | First Half, Kick Off | Live | Poll every 15-20 seconds |
| HT | Halftime | Live | Poll every 15-20 seconds |
| 2H | Second Half, 2nd Half Started | Live | Poll every 15-20 seconds |
| ET | Extra Time | Live | Poll every 15-20 seconds |
| BT | Break Time | Live | Poll every 15-20 seconds |
| P | Penalty In Progress | Live | Poll every 15-20 seconds |
| SUSP | Match Suspended | Live | Check every 5 minutes |
| INT | Match Interrupted | Live | Check every 5 minutes |
| FT | Match Finished | Completed | Final fetch + stop polling |
| AET | Match Finished After Extra Time | Completed | Final fetch + stop polling |
| PEN | Match Finished After Penalty | Completed | Final fetch + stop polling |
| PST | Match Postponed | Rescheduled | Check daily |
| CANC | Match Cancelled | Cancelled | Final state |
| ABD | Match Abandoned | Abandoned | Final state |
| AWD | Technical Loss | Awarded | Final state |
| WO | WalkOver | Awarded | Final state |
| LIVE | In Progress (generic) | Live | Poll every 15-20 seconds |

---

## Error Handling

### Standard Error Response
```json
{
  "get": "fixtures",
  "parameters": {"id": "999999"},
  "errors": [
    {
      "time": "2024-12-12T10:00:00+00:00",
      "bug": "This is on our side, please report us this bug on https://dashboard.api-football.com",
      "report": "fixtures?id=999999"
    }
  ],
  "results": 0,
  "response": []
}
```

### HTTP Status Codes
- **200:** Success (but check `errors` array)
- **204:** No Content (valid query, no results)
- **401:** Unauthorized (invalid API key)
- **429:** Too Many Requests (rate limit exceeded)
- **499:** Timeout (API-side timeout)
- **500:** Internal Server Error (API-side error)
- **502/504:** Gateway errors (temporary, retry)

### Error Handling Strategy
1. **Always check `errors` array** even on 200 status
2. **429:** Exponential backoff, update rate limiter state
3. **5xx/499:** Circuit breaker, retry with exponential backoff
4. **401:** Critical - check API key, alert immediately
5. **204:** Valid query, no data (e.g., no matches on this date)

---

## Rate Limit Best Practices

### 1. Token Bucket Implementation
```python
class RateLimiter:
    def __init__(self, daily_limit=7500, minute_limit=300):
        self.daily_limit = daily_limit
        self.minute_limit = minute_limit
        self.daily_remaining = daily_limit
        self.minute_remaining = minute_limit
    
    def update_from_headers(self, response_headers):
        self.daily_remaining = int(response_headers.get('x-ratelimit-requests-remaining', 0))
        self.minute_remaining = int(response_headers.get('X-RateLimit-Remaining', 0))
    
    def can_request(self):
        return self.daily_remaining > 100 and self.minute_remaining > 10
```

### 2. Priority System
- **Critical (always run):** Live fixtures, today's matches
- **High:** Standings, team stats
- **Medium:** Player stats, injuries
- **Low:** Historical data, backfill
- **Emergency stop:** When daily remaining < 1000, stop Low/Medium jobs

### 3. Batch Strategies
- ✅ Use `/fixtures?live=all` instead of per-fixture calls
- ✅ Use `/fixtures?league=X&season=Y` instead of per-team calls
- ✅ Use `/players?team=X&season=Y` instead of per-player calls
- ❌ NEVER loop through fixtures/teams/players individually

### 4. Cache Tiers
- **Hot cache (15s):** Live fixtures, live odds
- **Warm cache (1h):** Today's fixtures, current round
- **Cold cache (1d):** Completed fixtures, standings
- **Frozen cache (30d):** Static data (countries, timezones, teams)

---

## Data Integrity Rules

### 1. Dependency Order (CRITICAL)
```
Countries → Timezones (Static)
    ↓
Leagues → Seasons (Seasonal)
    ↓
Teams → Venues (Seasonal)
    ↓
Fixtures (Operational)
    ↓
Odds, Players, Injuries (Operational)
```

**NEVER insert Fixtures before Leagues and Teams exist.**

### 2. UPSERT Pattern (MANDATORY)
```sql
INSERT INTO core.fixtures (id, league_id, home_team_id, away_team_id, ...)
VALUES (...)
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    goals_home = EXCLUDED.goals_home,
    goals_away = EXCLUDED.goals_away,
    updated_at = NOW();
```

### 3. Primary Keys = API IDs
- `fixture_id` → Immutable even if postponed
- `team_id` → Immutable even if team transfers
- `player_id` → Immutable across teams
- `league_id` → Immutable across seasons

### 4. Timezone Rule
- **Store:** Always UTC in database
- **Display:** Convert to user timezone on client
- **Query:** Use `timezone` parameter for user-facing times

---

## Common Pitfalls & Solutions

### Pitfall 1: Calling `/odds` without filters
**Problem:** Returns massive paginated response, exhausts quota  
**Solution:** ALWAYS filter by `league`, `season`, or `date`

### Pitfall 2: Polling each fixture individually
**Problem:** 50 live fixtures = 50 requests = rate limit  
**Solution:** Use `/fixtures?live=all` (1 request for all)

### Pitfall 3: Ignoring `errors` array on 200
**Problem:** Partial failures not detected  
**Solution:** Always check `errors` array, log/alert on non-empty

### Pitfall 4: Hard-coding league IDs
**Problem:** Code breaks when tracking new leagues  
**Solution:** Store league IDs in config, read from database

### Pitfall 5: Not using UPSERT
**Problem:** Duplicate fixtures on re-fetch  
**Solution:** Use `ON CONFLICT (id) DO UPDATE SET ...`

### Pitfall 6: Fetching lineups too early
**Problem:** Empty response, wasted quota  
**Solution:** Only fetch lineups 1 hour before kickoff or after

### Pitfall 7: Storing times in local timezone
**Problem:** DST bugs, query inconsistencies  
**Solution:** Always store UTC, convert on display

### Pitfall 8: Not implementing circuit breaker
**Problem:** Repeated 500 errors exhaust quota  
**Solution:** After N failures, pause endpoint for X minutes

---

## Quick Start Checklist

### Setup Phase
- [ ] Obtain API key from dashboard
- [ ] Test with `/status` endpoint (free call)
- [ ] Implement rate limiter with header tracking
- [ ] Set up PostgreSQL with RAW/CORE/MART schemas
- [ ] Bootstrap static data: `/timezone`, `/countries`

### Initial Data Load
- [ ] Fetch leagues: `/leagues?season=2024`
- [ ] Fetch teams: `/teams?league=X&season=2024` for each tracked league
- [ ] Fetch venues: `/venues` (or via teams response)
- [ ] Create coverage metrics for each (league, season)

### Operational Phase
- [ ] Daily job: `/fixtures?date=YYYY-MM-DD` for tracked leagues
- [ ] Live job: `/fixtures?live=all` every 15 seconds
- [ ] Standings job: `/standings?league=X&season=Y` daily
- [ ] Monitor rate limits via response headers
- [ ] Monitor coverage via MCP queries

### Error Recovery
- [ ] Log all errors with context (endpoint, params, status)
- [ ] Circuit breaker for repeated failures
- [ ] Alert on daily quota < 1000
- [ ] Alert on 401 (invalid key)
- [ ] Alert on circuit breaker open

---

## Additional Resources

- **Dashboard:** https://dashboard.api-football.com/
- **Live Tester:** Use dashboard to test endpoints before coding
- **Support:** https://www.api-football.com/support
- **Bug Reports:** Include `errors.report` URL from response
- **Changelog:** Check dashboard for API updates

---

**Document Version:** 1.0  
**Last Updated:** 2024-12-12  
**API Version:** 3.9.3
