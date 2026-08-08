# Phase 1: Venue Discovery & Deterministic Filtering

## Purpose

Phase 1 accepts a user-provided city name and an optional list of business categories, discovers candidate venues from mapping APIs, applies cheap deterministic filtering to remove obviously unsuitable venues, and stores the results in a local SQLite cache for reuse during development.

The output is a ranked pool of ~1,500 candidate venues ready for Phase 2 (frontage image acquisition).

---

## Required Keys & Credentials

| Key / Credential | Source | Purpose | Cost Model |
|---|---|---|---|
| `GOOGLE_PLACES_API_KEY` | Google Cloud Console → APIs & Services → Credentials | Nearby Search, Geocoding, Street View Metadata | Pay-as-you-go; Nearby Search Basic ~$17 per 1,000 requests |
| `GOOGLE_GEOCODING_API_KEY` | Same as above (usually same key) | Convert city name to bounding box lat/lng | ~$5 per 1,000 requests; minimal usage |
| `STREET_VIEW_METADATA_API_KEY` | Same as above (same key) | Check if Street View panorama exists near a venue | Cheap / often bundled; metadata-only calls are lower cost |

**Note**: All three Google APIs can typically use the **same API key** with the Google Maps Platform enabled. You need to enable:
- Places API (New)
- Geocoding API
- Street View Static API

**Optional fallback / enrichment** (not required for Phase 1):
- `OVERPASS_API_ENDPOINT` — Free, no key needed. OpenStreetMap Overpass API for secondary venue discovery.

---

## SQLite Cache Strategy

### Why SQLite?

During full product development we will iterate frequently on filtering logic, prompts, compositing code, and QA criteria. Re-calling the Google Places API for the same city and categories on every run is:
- **Slow** (network latency + API pagination)
- **Expensive** (thousands of requests per city)
- **Unnecessary** (venue data does not change minute-to-minute)

A local SQLite database acts as a deterministic read-through cache. Once a city is scanned, subsequent development runs reuse the raw and filtered data instantly.

### Cache Key Design

The cache lookup key is a deterministic hash derived from three inputs:

1. **User query** (city name) — normalized to lowercase, stripped of whitespace
2. **Category list** — sorted alphabetically, joined by comma
3. **Quantity target** — the desired number of raw venues to discover (default 5,000)

Example:
```
query: "london"
categories: ["cafe", "restaurant", "beauty_salon"]
quantity: 5000

cache_key = "london|beauty_salon,cafe,restaurant|5000"
```

This string is hashed (e.g., SHA-256 truncated to 16 chars) and stored as `cache_hash`.

### Cache Invalidation Policy

- **TTL**: 7 days by default. After 7 days, a re-scan is triggered automatically if the same key is requested.
- **Manual override**: A `--force-refresh` flag bypasses the cache entirely.
- **Partial invalidation**: If the cache has <50% of the requested quantity, it is considered stale.

### SQLite Schema

```sql
-- Cache sessions: one row per unique scan (cache_key)
CREATE TABLE IF NOT EXISTS scan_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_hash TEXT UNIQUE NOT NULL,
    query TEXT NOT NULL,
    categories TEXT NOT NULL,
    quantity_target INTEGER NOT NULL,
    raw_found INTEGER DEFAULT 0,
    filtered_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',   -- pending, completed, failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

-- Raw venues: all venues returned by the API before filtering
CREATE TABLE IF NOT EXISTS raw_venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_session_id INTEGER NOT NULL,
    place_id TEXT NOT NULL,
    name TEXT,
    lat REAL,
    lng REAL,
    address TEXT,
    types TEXT,
    user_ratings_total INTEGER,
    business_status TEXT,
    discovery_source TEXT,
    grid_point_lat REAL,
    grid_point_lng REAL,
    UNIQUE(scan_session_id, place_id),
    FOREIGN KEY (scan_session_id) REFERENCES scan_sessions(id)
);

-- Filtered candidates: venues that passed Phase 1 deterministic filters
CREATE TABLE IF NOT EXISTS candidate_venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_session_id INTEGER NOT NULL,
    place_id TEXT NOT NULL,
    name TEXT,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    address TEXT,
    types TEXT,
    user_ratings_total INTEGER,
    business_status TEXT,
    discovery_source TEXT,
    street_view_score INTEGER DEFAULT 0,   -- 0=unknown, 50=wrong-heading, 100=good
    filter_passed_reasons TEXT,             -- JSON array of which filters passed
    filter_dropped_reason TEXT,             -- NULL if candidate; else reason
    is_candidate BOOLEAN DEFAULT 1,
    FOREIGN KEY (scan_session_id) REFERENCES scan_sessions(id)
);
```

### Cache Lookup Flow

```
User Input
  ↓
Normalize query + categories + quantity
  ↓
Generate cache_hash
  ↓
Query SQLite: SELECT * FROM scan_sessions WHERE cache_hash = ? AND expires_at > now()
  ↓
├─ Cache HIT AND status = 'completed' AND filtered_count >= target
│     ↓
│   Return candidates from candidate_venues instantly
│
└─ Cache MISS or stale
      ↓
   Call Google Places API (grid scan)
      ↓
   Store raw_venues
      ↓
   Run deterministic filters
      ↓
   Store candidate_venues
      ↓
   Update scan_sessions status = 'completed'
      ↓
   Return candidates
```

---

## Phase 1 Pipeline Steps

### Step 1: Geocode the City

Input: user-provided city string (e.g., "London", "Manchester").

Call Google Geocoding API or Nominatim (OpenStreetMap) to obtain:
- `bounds.northeast` and `bounds.southwest` (bounding box)
- `geometry.location` (center point)
- `formatted_name` (canonical name for display)

Store these in the scan session record.

### Step 2: Generate Search Grid

Tile the bounding box into search points with ~2km radius coverage.

- Use square grid with 20% overlap between circles to minimize edge gaps.
- For London-sized cities (~40km × 60km), expect ~80–100 grid points.
- For smaller cities, expect 10–30 points.

Each grid point is a `(lat, lng, radius)` tuple used for a Nearby Search call.

### Step 3: Discover Raw Venues

For each grid point, call Google Places Nearby Search (Basic field set):

Parameters:
- `location`: grid point lat,lng
- `radius`: 2000 meters
- `type`: one of the whitelisted categories per call
- `fields`: `place_id`, `name`, `geometry`, `types`, `vicinity`, `business_status`, `user_ratings_total`

Paginate to max 60 results per grid point (3 pages of 20).

Collect all results, deduplicate by `place_id`, and insert into `raw_venues`.

### Step 4: Cheap Deterministic Filtering

Run the filter stack in order. Each filter operates on metadata only — no vision, no image fetching.

| # | Filter | Rule | Action |
|---|---|---|---|
| 1 | **Category Gate** | Must have at least one whitelisted type (`cafe`, `restaurant`, `meal_takeaway`, `bakery`, `beauty_salon`, `hair_care`, `spa`, `bar`) | Drop if no match |
| 2 | **Operational Gate** | `business_status` must be `OPERATIONAL` | Drop if closed |
| 3 | **Street-Facing Gate** | Address must NOT contain: `mall`, `arcade`, `centre`, `terminal`, `airport`, `station`, `kiosk`, `stadium`, `arena`, `hospital`, `university`, `basement`. Type must NOT include `shopping_mall`, `transit_station`, `airport` | Drop if non-street-facing |
| 4 | **Chain Gate (Hard)** | Name fuzzy-matches against chain blacklist (e.g., Starbucks, Costa, McDonald's, Nando's, Wagamama, PizzaExpress, Greggs, Boots, Superdrug) | Drop if match |
| 5 | **Chain Gate (Soft)** | `user_ratings_total > 1000` signals likely chain unless explicitly whitelisted | Drop if exceeds threshold |
| 6 | **Review Existence Gate** | `user_ratings_total >= 3` (must have some social proof) AND `<= 1000` (indie ceiling) | Drop if outside range |
| 7 | **Name Heuristic Gate** | Name contains `hotel`, `inn`, `resort`, `hostel`, `bed and breakfast`, `guest house` AND category is strongly food/beauty | Drop if mismatch |

**Logging**: Every dropped venue records the specific filter reason in `filter_dropped_reason`. Every accepted venue records `filter_passed_reasons` as a JSON array.

### Step 5: Street View Metadata Scoring

For each surviving candidate, call Google Street View Metadata API:
- Query for panoramas within ~30 meters of the venue `lat,lng`
- Calculate bearing from panorama to venue
- Score:
  - `100` = panorama exists within 30m and a viable heading faces the venue
  - `50` = panorama exists but heading is ambiguous or offset
  - `0` = no nearby panorama

Store the score in `street_view_score`.

**Do NOT drop candidates with score 0.** Keep them for Phase 2 fallback handling (Business Photos, website imagery). Use the score for **ranking and routing priority** only.

### Step 6: Output

Return the top N candidates (default 1,500) ordered by:
1. `street_view_score` DESC (process easy ones first)
2. `user_ratings_total` DESC (more established venues)
3. `name` ASC (deterministic tiebreaker)

---

## Chain Blacklist Configuration

Store as a JSON file per locale:

```json
{
  "locale": "gb",
  "chains": [
    "starbucks", "costa", "pret a manger", "caffe nero",
    "mcdonald's", "subway", "kfc", "nando's", "wagamama",
    "pizzaexpress", "leon", "itsu", "greggs", "boots", "superdrug"
  ],
  "fuzzy_match": true,
  "case_sensitive": false
}
```

Make it hot-reloadable so the client can add new chains without code changes.

---

## Environment Variables Required

```bash
GOOGLE_PLACES_API_KEY=your_key_here
CACHE_DB_PATH=./data/venue_cache.db       # SQLite file path
CACHE_TTL_DAYS=7
DEFAULT_QUANTITY_TARGET=5000
DEFAULT_GRID_RADIUS_METERS=2000
STREET_VIEW_SEARCH_RADIUS_METERS=30
```

---

## Success Criteria for Phase 1

- Typing "London" returns 1,000–1,500 candidate venues within 2 minutes on a warm cache, or within 5 minutes on a cold cache.
- Zero manual curation is required to produce the candidate list.
- Every candidate has `lat`, `lng`, `place_id`, `name`, and `address` populated.
- The chain blacklist successfully drops >90% of obvious chains on visual inspection of a sample.
- The SQLite cache can be queried by `cache_hash` and returns identical results without hitting the Places API.
