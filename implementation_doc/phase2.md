# Phase 2: Frontage Image Acquisition, Vision QA & Fallback

## Overview

Phase 2 takes the candidate venues produced by Phase 1 and produces a real, usable photograph of each venue's actual entrance. It validates that the image is suitable for planter compositing, and falls back to alternative sources when Street View coverage is poor or facing the wrong way.

The output of Phase 2 is a local folder of frontage images and a QA report for each venue. Only venues that pass QA proceed to Phase 3 (compositing).

---

## Design Principle: Road Proximity Signals

Before requesting expensive Street View Static images, we use cheap or free signals to estimate whether a venue is physically on a road. This reduces wasted image requests on venues that are set back from the street, in courtyards, or in pedestrian-only zones.

### Signal 1: `road_proximity_meters` (used in the prototype pipeline)

**Source:** Street View Metadata API (already called in Phase 1 for every candidate).

**How it works:** The Street View Metadata response includes the `location` (lat, lng) of the nearest panorama. Street View cars only drive on public roads. Therefore, the straight-line distance from the venue to the panorama is a proxy for "distance from venue to nearest road with photographic coverage."

We compute the haversine distance between the venue lat/lng and the panorama lat/lng. This distance is stored in the `candidate_venues` table as `road_proximity_meters`.

**Gating logic for the prototype:**

| `road_proximity_meters` | Interpretation | Action |
|---|---|---|
| **0–10 m** | Venue is directly on the road. The camera drove right past the front door. | ✅ High priority for Static image request |
| **10–15 m** | Venue is near the road, possibly across the street or slightly set back. | ✅ Normal priority for Static image request |
| **15–25 m** | Venue is near the road but may be behind other buildings or on a side alley. | ⚠️ Request image, but flag `set_back = true` for tighter QA |
| **>25 m** | No panorama found within the Phase 1 search radius (`street_view_score = 0`). | ❌ Skip Static image; go directly to fallback chain |

**Why this is the primary signal:**
- It costs **$0 extra** because the data is already fetched in Phase 1
- It is more useful than a raw "road exists" check because it also confirms Street View coverage exists
- It is more accurate than address-string heuristics for venues in dense urban areas where addresses can be misleading

### Signal 2: Reverse-Geocoded Road Name Match (production enhancement)

**Source:** Google Geocoding API reverse geocoding (`latlng=...`).

**How it works:** Pass the venue lat/lng to the reverse geocoding endpoint. The response includes address components, including `route` (the road name). Compare this reverse-geocoded road name to the road name in the venue's `formatted_address` from the Places API.

| Match Result | Interpretation |
|---|---|
| **Match** | The venue is on the road that the geocoder thinks is primary at that lat/lng. Strong signal. |
| **Mismatch** | The venue might be around the corner, in a courtyard, or the address road name differs from the physical location. Weak signal. |

**Cost:** ~$5 per 1,000 reverse geocode requests.

**Why this is kept for production only:**
- In the prototype, `road_proximity_meters` catches the same bad venues at $0 cost
- The marginal improvement from reverse geocoding is small (~2–5% better filtering)
- At production scale (5,000/week), the $25/week cost is justified for the extra precision, but not for the prototype

**Production pipeline (post-demo):**
```
Phase 1 candidates
  ↓
road_proximity_meters <= 15?  →  YES: request Static image
  ↓  NO
reverse_geocode road name matches address?  →  YES: request Static image
  ↓  NO
flag as "possible_off_road" → fallback chain or human review
```

---

## Step 1: Image Acquisition

### Scope

For the prototype, we process the **top 20 candidates** with `street_view_score = 100` from the Phase 1 SQLite cache. The number 20 is chosen to keep API costs low during development and demonstration. In production, this step would process all candidates with `street_view_score >= 50`.

### Selecting the 20 candidates

Query the `candidate_venues` table:

```sql
SELECT * FROM candidate_venues
WHERE scan_session_id = ? AND is_candidate = 1 AND street_view_score = 100
ORDER BY street_view_score DESC, user_ratings_total DESC, name ASC
LIMIT 20;
```

### Computing the camera heading

For each candidate, we already have:
- Venue `lat, lng` (from Phase 1)
- Panorama `lat, lng` (from the Street View Metadata API call in Phase 1)

We compute the **initial bearing** from the panorama to the venue using the haversine formula. This bearing is the heading that points the Street View camera directly at the venue entrance.

### Requesting images from Street View Static API

We request **2 images per venue**:

1. **Primary image** — `heading = computed_bearing`, `pitch = 0`, `fov = 60`
   - This is the angle we will actually use for compositing in Phase 3.

2. **Validation image** — `heading = computed_bearing ± 20°`, `pitch = 0`, `fov = 60`
   - This is used only for Vision QA cross-check. It confirms that the primary angle is not accidentally showing a side wall or an obstructed view.

**Why only 2 angles?** Four angles per venue was considered but rejected because:
- On straight London roads, ±15° variations show more pavement and less facade, not useful new information
- At production scale (5,000/week), doubling the image count is a $70/week cost increase; quadrupling it is a $210/week cost increase
- Two images give us a safety net without diminishing returns

### Image storage convention

All images are stored locally on disk so they can be reused during development without re-calling the API:

```
data/
  images/
    {venue_id}/
      streetview_primary_{heading}.jpg
      streetview_validation_{heading}.jpg
      metadata.json
```

`metadata.json` contains:
- `venue_id`, `place_id`, `name`, `address`
- `panorama_location` (lat, lng of the Street View panorama)
- `computed_bearing` (degrees)
- `primary_heading`, `validation_heading`
- `pitch`, `fov`
- `image_size` (e.g., 640x480)
- `source` = "google_street_view_static"
- `requested_at` (ISO timestamp)

### Cost estimate for Step 1 (20 venues)

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| Street View Static images | 40 | ~$0.007 | ~$0.28 |

### What about venues with street_view_score = 50?

`score = 50` means a panorama exists within the search radius but is farther than 20 meters away (e.g., across a wide road or down the street). These venues enter Step 1 only after all `score = 100` venues are processed. In the prototype, we focus exclusively on the `score = 100` batch to maximize the chance of clean frontage shots.

---

## Step 2: Vision QA

### Purpose

We now have 40 real photographs (2 per venue). We need a machine to look at each photo and answer: *"Does this actually show the venue's entrance, and is it suitable for a planter?"*

This is done by a multimodal vision model (Gemini 3.5 Flash Lite). The model receives the image and a strict structured prompt. It returns a JSON object. Our Python code makes the accept/reject decision deterministically based on that JSON.

### Why we do not pre-filter with geometry

A common question: can we know whether the panorama is street-facing *before* we download the image, by analyzing lat/lng and road geometry?

**Answer: no, not reliably.** A panorama within 20 meters could be:
- Across the street facing the entrance (good)
- On a side street showing the side wall (bad)
- In an alley behind the building (bad)
- On the correct street but facing perpendicular down the road (bad)

Pre-filtering with OpenStreetMap road geometry is possible but over-engineered for the prototype. The vision model is explicitly good at semantic image understanding. We let it reject side-wall shots empirically.

If the rejection rate is >30% after the first batch of 20, we will tune the heading calculation (e.g., shrink the search radius, add a road-name match check). If the rejection rate is <15%, the current bearing logic is good enough.

### Vision prompt structure

The prompt is deterministic and asks for specific signals:

```
Analyze this storefront image and return ONLY a JSON object:

{
  "entrance_visible": true/false,
  "facade_is_bare": true/false,
  "ground_space_for_planters": true/false,
  "existing_planters": true/false,
  "image_quality": 1-10,
  "obstruction_level": "none" / "partial" / "severe",
  "lighting_condition": "bright" / "overcast" / "shadowed" / "night",
  "confidence": 0.0-1.0,
  "notes": "brief explanation"
}

Rules:
- entrance_visible: true only if the doorway is clearly in frame
- facade_is_bare: true if the entrance has minimal decoration, no large planters, no elaborate signage
- ground_space_for_planters: true if there is flat pavement/sidewalk directly in front of the door
- existing_planters: true if you can already see planter boxes or large potted plants
- image_quality: 10 = sharp, well-lit, daytime; 1 = blurry, dark, or heavily distorted
- confidence: your certainty that the above judgments are correct
```

### Acceptance criteria

A candidate passes Vision QA only if ALL of the following are true:

| Field | Rule |
|---|---|
| `entrance_visible` | must be `true` |
| `facade_is_bare` | must be `true` |
| `ground_space_for_planters` | must be `true` |
| `existing_planters` | must be `false` |
| `image_quality` | must be `>= 7` |
| `confidence` | must be `>= 0.75` |

If any field fails, the image is **rejected**. The rejection reason is logged.

### Cross-validation with the second angle

Each venue has 2 images. We run Vision QA on both. The outcomes are:

| Primary | Validation | Decision |
|---|---|---|
| PASS | PASS | ✅ Accept primary image for Phase 3 |
| PASS | FAIL | ⚠️ Accept primary image, but log the mismatch |
| FAIL | PASS | ⚠️ Accept validation image for Phase 3 instead |
| FAIL | FAIL | ❌ Reject venue entirely |

In the prototype, we default to the **primary angle** unless it fails and the validation passes. We do not attempt to "blend" or average the two angles.

### Cost estimate for Step 2 (20 venues)

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| Vision QA (Gemini 3.5 Flash Lite) | 40 images | ~$0.005 | ~$0.20 |

> **Note on API keys and rate limits:**
> The prototype uses the **Gemini free tier** (`gemini-3.5-flash-lite`) for Vision QA. Free tier has strict daily request limits and can hit `429 RESOURCE_EXHAUSTED` during active development.
>
> **For production deployment:**
> - Enable billing on your Google Cloud project at [Google AI Studio → Billing](https://aistudio.google.com/app/billing)
> - This upgrades rate limits from ~15 requests/minute to **1,500+ requests/minute**
> - Cost remains extremely low: ~$0.075 per 1M input tokens (roughly **$0.005 per image**)
> - At 5,000 venues/week × 2 images = 10,000 images/week → **~$50/week** for Vision QA
>
> The same `GOOGLE_GEMINI_API_KEY` environment variable is used; only the billing status on the Google Cloud project needs to change.

### QA output storage

```
data/
  images/
    {venue_id}/
      qa_report.json
```

`qa_report.json` contains:
- `primary_qa` (full JSON response from vision model)
- `validation_qa` (full JSON response from vision model)
- `final_decision`: "accepted" / "rejected"
- `rejection_reason`: human-readable string if rejected
- `processed_at`: ISO timestamp

---

## Step 3: Fallback Strategy

### When is the fallback triggered?

The fallback chain activates for any venue where:
- `street_view_score = 0` in Phase 1 (no nearby Street View panorama)
- OR the Street View image failed Vision QA in Phase 2 (wrong angle, obscured, poor quality)

### Fallback hierarchy

```
Street View image
  ↓
├─ PASS → Phase 3
│
└─ FAIL
      ↓
  Fallback 1: Google Business Photos
      ↓
  ├─ Photo available and exterior-facing?
  │     ↓
  │   Request Place Details (photos field)
  │     ↓
  │   Vision QA on the Business Photo
  │     ↓
  │   ├─ PASS → Phase 3
  │   └─ FAIL → Fallback 2
  │
  └─ No exterior photos?
        ↓
    Fallback 2: Venue website imagery
        ↓
    Request venue website URL from Place Details
        ↓
    Scrape website for exterior images (headless browser or image extraction heuristics)
        ↓
    Vision QA on the best candidate image
        ↓
    ├─ PASS → Phase 3
    └─ FAIL → Mark "unusable"
```

### Why we need aggressive pre-filtering before Vision QA

A venue's Google Business listing may contain 5–20 images. Its website may contain 30–50+ images. Most of these are interiors, food shots, staff portraits, logos, or event posters.

**You cannot send 50 images per venue to Gemini.** That is $0.25 per venue. At 500 fallback venues per week, that is $125/week just to filter out junk.

The solution is a **three-tier pre-filtering pipeline** that eliminates 85–90% of bad images for free before any expensive vision API is invoked.

---

### Tier 1: Deterministic metadata filters (free, drops ~70% of junk)

These filters require **zero image downloads** and **zero ML**. They operate on URLs, dimensions, and filenames.

**A. Aspect ratio filter**
- Exterior/frontage photos are almost always **landscape** (wider than tall)
- Interior/food photos are often **square** (Instagram-style) or **portrait** (menu shots)
- **Rule:** Keep only images with aspect ratio between **1.3 and 3.0**
- This alone drops 40–50% of bad images

**B. Minimum size filter**
- Thumbnails, icons, and compressed mobile uploads are < 400px wide
- Professional exterior shots are usually > 800px wide
- **Rule:** Reject images < 400px wide or < 30KB

**C. URL / filename keyword filter**
- Boost score if URL contains: `exterior`, `outside`, `front`, `street`, `storefront`, `building`, `facade`, `shop`, `venue`, `store`
- Penalize if URL contains: `interior`, `inside`, `menu`, `food`, `dish`, `plate`, `staff`, `team`, `chef`, `logo`, `icon`, `banner`, `event`, `party`, `wedding`

**Why this works:** Business owners and web developers often name files descriptively. `storefront-summer-2024.jpg` is obvious. `team-photo-chef.jpg` is also obvious.

**For Google Business Photos:** The Places API (New) returns `widthPx` and `heightPx` in the `photos` array. You can apply aspect ratio and size filters **before downloading a single byte.**

**For website images:** Many `<img>` tags have `width` and `height` attributes in the HTML. You can filter those immediately. For images without attributes, you can send an HTTP `HEAD` request or download only the first 2KB (JPEG/PNG headers contain dimensions) before fetching the full file.

---

### Tier 2: Lightweight visual scoring (classical CV, ~$0, drops another ~20%)

After Tier 1, you might still have 8–12 images per venue. You need to rank them by "how likely is this an exterior shot?" without calling an expensive API.

I would use **two classical computer vision techniques** that run in milliseconds per image:

**A. Color histogram analysis**
- Exterior daytime shots have:
  - **Blue channel** (sky, 10–30% of pixels)
  - **Gray / brown channel** (pavement, building facade)
  - High brightness variance (sunlit areas + shadows)
- Interior shots have:
  - **Warm yellow / orange** (indoor lighting, 40–60% of pixels)
  - Low brightness variance (even artificial light)
- **Implementation:** Resize image to 64×64, compute mean RGB and brightness variance. Score = `(blue_ratio × gray_ratio × variance) / warm_ratio`. Takes ~1ms per image with PIL/NumPy.

**B. Edge detection + vertical line density**
- Building facades have strong **vertical lines**: doors, windows, columns, signage poles
- Street scenes have **horizontal lines**: pavement edges, road markings, awnings
- Interior shots have chaotic, curved, or soft edges (furniture, people, fabric)
- **Implementation:** Run Canny edge detection, then Hough line transform. Count vertical vs. horizontal lines. Score = `vertical_lines / (horizontal_lines + 1)`. Takes ~5ms per image with OpenCV.

**Combined Tier 2 score:**
```
score = 0.4 × aspect_ratio_score
      + 0.2 × size_score
      + 0.2 × keyword_score
      + 0.1 × color_exterior_score
      + 0.1 × vertical_line_score
```

You rank all surviving images by this score. Only the **top 2** move to Tier 3.

---

### Tier 3: Vision QA (expensive, only 1–2 images per venue)

Now you have 1–2 high-probability exterior images per venue. You send them to Gemini with the same structured prompt from Step 2.

- **Cost per venue:** 2 images × $0.01 = **$0.02**
- **Cost without pre-filtering:** 50 images × $0.01 = **$0.50**
- **Savings:** **96% cost reduction**

---

### Fallback 1: Google Business Photos

**Expected schema from Places API (New) `photos` field:**

```json
{
  "photos": [
    {
      "name": "places/ChIJ.../photos/ABC123...",
      "widthPx": 1200,
      "heightPx": 800,
      "authorAttributions": [
        {
          "displayName": "John Doe",
          "uri": "https://maps.google.com/...",
          "photoUri": "https://lh3.googleusercontent.com/..."
        }
      ]
    }
  ]
}
```

**Approach:**
1. Call Place Details with `fieldMask=photos,websiteUri`
2. For each photo in the `photos` array, apply **Tier 1 filters** using `widthPx` and `heightPx` (no download needed)
3. Keep the top 5 by combined score
4. Download the full image bytes for those 5
5. Run **Tier 2 visual scoring** (color histogram + edge detection)
6. Rank again and take the **#1 ranked image**
7. Send to **Tier 3 Vision QA**
8. If it passes → Phase 3. If it fails → Fallback 2.

**Why we do not use a neural network classifier here:**
- You could train a small CNN (MobileNetV2) to classify "exterior" vs "interior" vs "food"
- But the combination of aspect ratio + URL keywords + color histogram catches 85–90% of bad images
- A CNN might get you to 93%, but the marginal gain is not worth the engineering complexity
- You do not have a labeled dataset of "London cafe exterior images"
- Classical CV is deterministic and debuggable. A neural network is a black box.

**If you want a neural network anyway:** Use **CLIP** (zero-shot) with the prompt: *"a photo of the outside of a restaurant on a city street."* It runs locally, requires no training, and gives you a similarity score. But the classical pipeline above is faster and cheaper.

**Cost:** ~$17 per 1,000 Place Details calls. For the prototype, negligible.

---

### Fallback 2: Venue website imagery

If Google Business Photos do not yield a usable exterior shot, we scrape the venue's own website.

**Fuzzy page detection:**

We do not just scrape the homepage. We score every linked page by how likely it is to contain an exterior photo, using fuzzy matching on the URL path:

| Page type | URL patterns | Exterior probability |
|---|---|---|
| Landing / Home | `/`, `/home`, `/index` | Medium |
| About Us | `/about`, `/about-us`, `/our-story`, `/who-we-are` | **High** — often has a storefront photo |
| Contact / Find Us | `/contact`, `/contact-us`, `/find-us`, `/location`, `/directions`, `/visit` | **High** — almost always has an exterior or street photo |
| Gallery | `/gallery`, `/photos`, `/images`, `/media` | **High** |
| Menu | `/menu`, `/food`, `/drinks`, `/cuisine` | Low — usually food photos |
| Events | `/events`, `/weddings`, `/parties`, `/bookings` | Low — usually interior/event photos |
| Team / Staff | `/team`, `/staff`, `/chefs`, `/people` | Low — portraits |

**Scoring logic:**
```python
page_score = 0
if fuzzy_match(path, "contact|find-us|location|visit"): page_score += 40
if fuzzy_match(path, "about|story|gallery|photos"): page_score += 30
if fuzzy_match(path, "home|index"): page_score += 20
if fuzzy_match(path, "menu|food|drink"): page_score -= 30
if fuzzy_match(path, "event|wedding|party|team|staff|chef"): page_score -= 40
```

We crawl the homepage, extract all `<a href="...">` links, score each linked page, and then crawl the **top 3 scoring pages** for `<img>` tags.

**Image extraction and filtering:**

For every `<img>` tag found on the crawled pages:
1. Extract `src` URL. Resolve relative URLs to absolute.
2. If `width` and `height` attributes exist in HTML, use them for Tier 1 aspect ratio filtering.
3. If attributes are missing, download only the first **2KB** of the image file. JPEG and PNG headers contain dimensions in the first few bytes. This avoids downloading a 5MB image just to learn it's a square logo.
4. Apply **Tier 1** filters: aspect ratio, size, URL keywords.
5. For survivors, download the full image.
6. Apply **Tier 2** visual scoring: color histogram + edge detection.
7. Rank and take the **top 2**.
8. Send to **Tier 3 Vision QA**.

**Limitations:**
- Some independent venues have no website
- Some websites block scraping (robots.txt, Cloudflare)
- JavaScript-rendered galleries (React/Vue) may not expose `<img>` tags in the raw HTML
- Must respect `robots.txt` and terms of service

**Cost:** Free (local HTTP requests), but slower than API calls.

---

### Fallback 3: Mark as unusable

If both fallback sources fail, the venue is marked:

```json
{
  "venue_id": "...",
  "status": "rejected",
  "rejection_source": "image_acquisition",
  "rejection_reason": "No Street View, no Business Photos, no website imagery passed QA",
  "phase2_completed_at": "2026-08-08T16:30:00Z"
}
```

This venue is **never** sent to Phase 3. It is logged for analytics but does not consume any further compute.

---

## Unified Fallback Service Architecture

Rather than calling Business Photos and Website Crawler separately and managing their results by hand, the prototype wraps both sources behind a single service: `FallbackImageService`.

### Single-call API

```python
service = FallbackImageService(api_key=settings.google_places_api_key)

result = service.fetch_images(
    place_id="ChIJ...",
    website_url=None,        # optional; if None, uses Place Details response
    force_refresh=False,
)
```

### What happens internally

```
fetch_images(place_id)
  ↓
[1] Check local cache (data/fallback_images/{place_id}/metadata.json)
    ├─ Cache hit → return immediately (0 API calls)
    └─ Cache miss → continue
  ↓
[2] Call Google Place Details (fieldMask=photos,websiteUri)
    ├─ Extract raw photos array
    └─ Extract websiteUri
  ↓
[3] Source A: Business Photos pipeline
    ├─ Tier 1 metadata filtering (aspect ratio, size, URL keywords)
    ├─ Download top 3 survivors
    └─ Tier 2 visual scoring (color + edge)
  ↓
[4] Source B: Website Crawler pipeline (if websiteUri exists)
    ├─ Discover internal links
    ├─ Score pages by exterior-relevance (fuzzy URL matching)
    ├─ Crawl top 3 pages
    ├─ Extract <img> tags
    ├─ Tier 1 filtering (aspect ratio, size, URL keywords)
    ├─ Tier 2 visual scoring (color + edge)
    └─ Return ranked candidates
  ↓
[5] Union: combine all candidates from both sources
    ├─ Sort by tier2_score descending
    └─ Pick best candidate
  ↓
[6] Persist metadata.json to disk
  ↓
Return result dict with best_candidate, candidates[], websiteUri
```

### Why this architecture matters

1. **One call per venue.** The caller does not need to know whether Business Photos or Website Crawler produced the best image. The service decides.
2. **Cache at the union level.** Once a venue has been processed, `metadata.json` contains the complete ranked union. Second call = 0 API calls.
3. **Source-agnostic ranking.** A website image can outrank a Business Photo. The tier2_score is computed the same way regardless of source.

### Real example: Fortunella Café

| Source | Candidates | Best Tier 2 Score |
|---|---|---|
| Google Business Photos | 3 | 35.8 |
| Website (`fortunella.co.uk`) | 2 | **36.52** |
| **Union winner** | **website** | **36.52** |

The website's exterior photo scored higher than all three Google Business Photos. Without the union architecture, we would have used an inferior Business Photo and never discovered the better website image.

### Cache structure

```
data/fallback_images/
  {place_id}/
    metadata.json          ← union result + ranking
```

`metadata.json` contains:
- `place_id`, `status`, `websiteUri`
- `candidate_count`
- `candidates[]` — all candidates from both sources, ranked by tier2_score
- `best_candidate` — the #1 candidate
- `sources.business_photos_count`
- `sources.website_count`

---

### Cost reality check

| Approach | Images per venue | Vision QA cost per venue | Weekly cost (500 fallbacks) |
|---|---|---|---|
| Naive: send everything | 50 | $0.50 | $250 |
| Tier 1 only (metadata) | 8 | $0.08 | $40 |
| **Tier 1 + Tier 2 (recommended)** | **2** | **$0.02** | **$10** |

---

### Cost estimate for Step 3 (20-venue prototype)

In the prototype, we expect ~15–18 of the 20 venues to pass with Street View alone. The remaining 2–5 might trigger fallback. The cost is negligible:

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| Place Details (fallback) | ~5 | ~$0.017 | ~$0.09 |
| Vision QA on fallback images | ~5 | ~$0.01 | ~$0.05 |
| **Total fallback cost** | | | **~$0.14** |

---

## Summary of Phase 2 decisions

| Decision | Rationale |
|---|---|
| 20 venues for prototype | Keeps image + vision costs under $1 for development |
| Only `street_view_score = 100` venues in Step 1 | Maximizes probability of clean frontage shots |
| 2 images per venue (primary + validation) | Safety net without 4× cost explosion |
| No geometric pre-filtering | Vision QA is cheaper and more accurate than OSM road geometry analysis |
| Empirical tuning | If rejection rate >30%, we tighten heading logic; if <15%, we proceed |
| Acceptance criteria are hard gates | All 6 fields must pass. No partial credit. |
| Fallback chain is explicit | Street View → Business Photos → Website → Reject. No infinite loops. |

---

## What Phase 2 produces

For each of the ~15–18 venues that survive:

```
data/
  images/
    {venue_id}/
      streetview_primary_{heading}.jpg
      streetview_validation_{heading}.jpg
      metadata.json
      qa_report.json
```

`qa_report.json` contains the full vision model response, the cross-validation logic, and the final `accepted` / `rejected` decision.

Only venues with `final_decision: accepted` proceed to Phase 3 (compositing).
