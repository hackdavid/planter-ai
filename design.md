# Planter Prospecting Engine — Design Document

> **System:** Automated venue discovery and visual prospecting for design-led outdoor planters  
> **Scope:** End-to-end pipeline from London postcode → venue shortlist → validated frontage image → composited "planter installed" visual  
> **Author:** Daud Ibrahim  
> **Date:** August 2026

---

## Table of Contents

1. [Executive Summary & Whole Workflow](#1-executive-summary--whole-workflow)
2. [Phase 1: Automated Venue Discovery](#2-phase-1-automated-venue-discovery)
3. [Phase 2: Frontage Image Acquisition & Validation](#3-phase-2-frontage-image-acquisition--validation)
4. [Phase 3: Planter Compositing](#4-phase-3-planter-compositing)
5. [Imagery Rights & Ethics](#5-imagery-rights--ethics)
6. [Rejection Criteria](#6-rejection-criteria)
7. [Three Selected Venues](#7-three-selected-venues)
8. [Rejected Venues & Why](#8-rejected-venues--why)
9. [Key Technical Decisions](#9-key-technical-decisions)
10. [Cost & Scale Estimates](#10-cost--scale-estimates)

---

## 1. Executive Summary & Whole Workflow

### What This System Does

The Planter Prospecting Engine automates the work a sales rep would do manually: find independent cafés and restaurants in London with bare frontages, capture a photo of the actual entrance, and produce a realistic visual showing the client's planter installed outside — convincing enough to send to the venue owner as a cold-outreach asset.

### High-Level Workflow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PLANTER PROSPECTING ENGINE                          │
│                    (Fully Automated, No Manual Eyeballing)                  │
└─────────────────────────────────────────────────────────────────────────────┘

   ┌──────────────┐
   │   START      │
   │  London, UK  │
   └──────┬───────┘
          │
          ▼
┌─────────────────────┐     ┌─────────────────────┐
│   PHASE 1           │     │   GOOGLE PLACES API │
│   Venue Discovery   │◄────┤   + SQLite Cache    │
│                     │     │   + Chain Filter    │
└──────────┬──────────┘     └─────────────────────┘
           │
           │ 50 candidates
           ▼
┌─────────────────────┐
│   Auto-Scoring      │
│   street_view_score │  ← distance to nearest road + panorama availability
└──────────┬──────────┘
           │
           │ Top N with SV=100
           ▼
┌─────────────────────┐     ┌─────────────────────┐
│   PHASE 2A          │     │   GOOGLE STREET     │
│   Street View       │◄────┤   VIEW STATIC API   │
│   Image Capture     │     │   (heading, FOV)    │
└──────────┬──────────┘     └─────────────────────┘
           │
           │ 20 images cached
           ▼
┌─────────────────────┐     ┌─────────────────────┐
│   PHASE 2B          │     │   GEMINI 3.5        │
│   Vision QA Gate    │◄────┤   FLASH-LITE        │
│   (Pass / Fail)     │     │   Vision Model      │
└──────────┬──────────┘     └─────────────────────┘
           │
           ▼
┌─────────────────────┐     ┌─────────────────────┐
│   PHASE 2C          │     │   GEMINI 3.5        │
│   Scene Analysis    │◄────┤   FLASH-LITE        │
│   (Geometry +       │     │   Vision Model      │
│    Lighting)        │     │                     │
└──────────┬──────────┘     └─────────────────────┘
           │
     ┌─────┴─────┐
     │           │
   PASS       FAIL
     │           │
     │           ▼
     │    ┌─────────────────────┐
     │    │   FALLBACK PATH     │
     │    │   Business Photos   │
     │    │   + Website Crawler │
     │    └──────────┬──────────┘
     │               │
     │               ▼
     │    ┌─────────────────────┐
     │    │   Vision QA Gate    │
     │    │   (Union Candidates)│
     │    └──────────┬──────────┘
     │               │
     └───────────────┘
                     │
                     │ 3 usable venues
                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PHASE 3A: SCENE ANALYSIS                             │
│                                                                              │
│   ┌─────────────────────────┐                                                │
│   │   Gemini Vision         │                                                │
│   │   (one call per image)  │                                                │
│   │                         │                                                │
│   │   → pixels_per_meter    │                                                │
│   │   → shadow_angle_deg    │                                                │
│   │   → shadow_softness     │                                                │
│   │   → ground_plane_y      │                                                │
│   │   → placement_ranking   │  [(x,y), (x,y), ...]                         │
│   │   → door_bbox           │                                                │
│   └──────────┬──────────────┘                                                │
│              │                                                                │
│              ▼                                                                │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │                         PHASE 3B: COMPOSITING                          │  │
│  │                                                                         │  │
│  │   ┌─────────────────────────┐         ┌─────────────────────────┐       │  │
│  │   │   HYBRID GENERATIVE     │         │   CV FALLBACK MODE      │       │  │
│  │   │   (Primary)             │         │   (Safety Net)          │       │  │
│  │   │                         │         │                         │       │  │
│  │   │   1. CV places real     │         │   rembg extraction    │       │  │
│  │   │      planter (exact     │         │   + real-world scale  │       │  │
│  │   │      product photo)     │         │   + scene-matched     │       │  │
│  │   │   2. Gen refines only   │         │    shadow + paste     │       │  │
│  │   │      boundary pixels    │         │                         │       │  │
│  │   │      (mask = planter    │         │   → Deterministic comp  │       │  │
│  │   │      bbox + 20% dil)    │         │                         │       │  │
│  │   └──────────┬──────────────┘         └──────────┬──────────────┘       │  │
│  │              │                                    │                      │  │
│  │              │  If ANY error (402, 429, etc.)     │                      │  │
│  │              └───────────────────────────────────►│                      │  │
│  │                                                  │                      │  │
│  │                                                  ▼                      │  │
│  │                                   ┌─────────────────────────┐          │  │
│  │                                   │   QUALITY GATE          │          │  │
│  │                                   │   (8 automated checks)  │          │  │
│  │                                   │   → scale, placement,     │          │  │
│  │                                   │     lighting, fidelity, │          │  │
│  │                                   │     integrity, grounding│          │  │
│  │                                   └──────────┬────────────┘          │  │
│  │                                              │                         │  │
│  │                                         PASS │ FAIL → retry or skip   │  │
│  │                                              │                         │  │
│  │                                              ▼                         │  │
│  │                                   ┌─────────────────────────┐          │  │
│  │                                   │   OUTPUT: 3 variations  │          │  │
│  │                                   │   per venue             │          │  │
│  │                                   └─────────────────────────┘          │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Design Philosophy

Every phase is **automated, cacheable, and defensible**. At 5,000+ venues per week, no human can eyeball candidates or approve frontage images. The system must decide, unaided, what is good enough — and fall back gracefully when it isn't.

---

## 2. Phase 1: Automated Venue Discovery

### Goal
Produce ~50 independent, street-facing, operational cafés and restaurants in a target area (London, UK) with no manual curation.

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     PHASE 1: VENUE DISCOVERY                                │
└─────────────────────────────────────────────────────────────────────────────┘

   ┌──────────────┐
   │  User Query  │
   │ London, UK   │
   └──────┬───────┘
          │
          ▼
┌─────────────────────────┐
│  Google Places API      │
│  Nearby Search          │
│  type=cafe|restaurant   │
│  radius=2km per grid    │
└──────────┬──────────────┘
           │
           │ ~3,500 raw results
           ▼
┌─────────────────────────┐
│  SQLite Cache Layer     │
│  (dedup by place_id)    │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  Automated Filters      │
│  (heuristic pipeline)   │
└──────────┬──────────────┘
           │
     ┌─────┴─────┐
     │           │
   PASS       DROP
     │           │
     │           ▼
     │    ┌─────────────────────────┐
     │    │  Dropped Reasons Log    │
     │    │  (for audit/debug)      │
     │    └─────────────────────────┘
     │
     ▼
┌─────────────────────────┐
│  Scoring Layer          │
│  street_view_score      │
│  = f(road_distance,     │
│       has_panorama)     │
└──────────┬──────────────┘
           │
           │ Top 50
           ▼
┌─────────────────────────┐
│  SQLite: candidate_venues│
│  scan_session            │
│  (persistent cache)      │
└─────────────────────────┘
```

### Automated Selection Criteria

| Filter | Rule | Why |
|---|---|---|
| **Category** | `types` includes `cafe` or `restaurant` | Targets the client's core market |
| **Operational** | `business_status == "OPERATIONAL"` | Avoids closed/renovated venues |
| **Street-facing** | `road_proximity_meters < 50` | Frontage must be visible from the street |
| **Independent** | Name not in `chain_blacklist.json` | The client targets independents, not Costa/Starbucks |
| **Review range** | `user_ratings_total` between 10 and 2,000 | Filters out unreviewed ghost venues and mega-chains |
| **Name heuristic** | No "Ltd", "Hotel", "Express", "Drive Thru" | Removes non-café businesses misclassified by Google |

### Scoring: `street_view_score`

```
street_view_score = 100 IF:
    - road_proximity_meters <= 30
    - panorama metadata exists (lat/lng/heading/pitch)

street_view_score = 50 IF:
    - road_proximity_meters <= 50
    - no panorama metadata

Otherwise: 0
```

This score drives Phase 2 priority — the closer a venue is to a Street View panorama with known camera heading, the more likely we get a usable frontage photo.

### Why This Approach

| Aspect | Our Approach | Why Not Alternative |
|---|---|---|
| **Data source** | Google Places API | **Not OpenStreetMap:** OSM has poor coverage of London café categories and no review counts/ratings to filter chains |
| **Filtering** | Heuristic rules in code | **Not ML classification:** Heuristics are fast, deterministic, and debuggable. An ML venue classifier would need labeled training data we don't have |
| **Caching** | SQLite with session IDs | **Not in-memory only:** The pipeline re-runs often. SQLite lets us resume, audit, and compare scan sessions over time |
| **Chain detection** | Name-based blacklist | **Not ownership lookup:** Google Places doesn't expose parent company data. Name matching is the only free signal |

---

## 3. Phase 2: Frontage Image Acquisition & Validation

### Goal
For each top candidate, produce a single validated frontage photograph — the actual entrance, from street level, unobscured, with no existing planter blocking the view.

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│              PHASE 2: FRONTAGE IMAGE ACQUISITION & VALIDATION               │
└─────────────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────┐
   │  Candidate Venue        │
   │  street_view_score=100  │
   └──────────┬──────────────┘
              │
              ▼
    ┌───────────────────────┐
    │  PRIMARY PATH          │
    │  Google Street View    │
    │  Static API            │
    │                       │
    │  heading = venue→road │
    │  FOV = 90°            │
    │  size = 640×480       │
    └──────────┬────────────┘
               │
               │ image downloaded
               ▼
    ┌───────────────────────┐
    │  Vision QA Gate        │
    │  Gemini 3.5 Flash-Lite│
    │                       │
    │  Checklist:           │
    │  1. Exterior front?   │
    │  2. Street-level?     │
    │  3. Entrance visible? │
    │  4. No big planter?   │
    │  5. Is a photograph?  │
    └──────────┬────────────┘
               │
         ┌─────┴─────┐
         │           │
       PASS       FAIL
         │           │
         │           ▼
         │    ┌───────────────────────┐
         │    │  FALLBACK PATH         │
         │    │  (fills quota gap)     │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │  Step 1: Business      │
         │    │  Photos from Google    │
         │    │  Places API            │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │  Step 2: Website       │
         │    │  Crawler               │
         │    │  (scrapes gallery      │
         │    │   for exterior images) │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │  Union of candidates   │
         │    │  from both sources     │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │  Vision QA on each     │
         │    │  candidate image       │
         │    │  (stop on first pass)  │
         │    └──────────┬────────────┘
         │               │
         └───────────────┘
                         │
                         ▼
              ┌───────────────────────┐
              │  Validated Frontage    │
              │  Image (JPG)           │
              │  → Phase 3             │
              └───────────────────────┘
```

### Primary Path: Street View

**How framing is derived:**

1. **Heading:** Calculate the bearing from the venue coordinates to the nearest road segment (OpenStreetMap). The camera should face the road, which is the direction pedestrians approach from.
2. **FOV:** 90° gives a wide enough view of the storefront without excessive fisheye distortion.
3. **Pitch:** 10° downward tilt — Street View cameras are mounted high; tilting down captures the entrance at pedestrian eye level.
4. **Search radius:** 30m around the venue. If no panorama exists within 30m, the venue gets `street_view_score=0` and is deprioritized.

**Cache strategy:** Images are saved to disk by `place_id` with a deterministic filename. Re-running the pipeline with `force_refresh=False` skips re-downloads entirely.

### Vision QA Gate (Gemini)

**Model:** `gemini-3.5-flash-lite` via Google AI Studio  
**Prompt:** A strict checklist:

> 1. The image clearly shows the exterior front of the venue  
> 2. The photo is taken from street level / pedestrian perspective  
> 3. The entrance or shop-front is visible and unobscured  
> 4. The image is a photograph, not a sketch or logo  
> 5. There is no large planter, flower box, or heavy greenery already blocking the frontage  

**Output format:** JSON with `pass` (bool), `confidence` (0.0–1.0), `reason` (one sentence).  
**Caching:** Results are cached to disk by MD5 hash of image + prompt. Re-evaluating the same image costs $0.

**Why this model:** Gemini Flash-Lite is fast (~1s), cheap, and has a generous free tier. It handles the 640×480 Street View images without issue.

### Fallback Path: Business Photos + Website Crawler

When Street View fails QA or doesn't exist, the system attempts two fallback sources:

1. **Google Business Photos:** Fetched from the Places API `photos` field. These are curated by the venue owner — often interiors, food shots, or logo graphics. We rank them by size (largest first) and run Vision QA on each.

2. **Website Crawler:** Scrapes the venue's website (from `websiteUri` in Places API) for image tags (`<img>`). Downloads all images, filters by aspect ratio (landscape > 1.2:1) and min size (300px), deduplicates by perceptual hash, then runs Vision QA on the remainder.

**Union ranking:** Candidates from both sources are merged and ranked by:
- Vision QA confidence (highest first)
- Source (Business Photos preferred over website crawl for quality)
- Stop on first pass

### Why This Two-Tier Architecture

| Aspect | Primary (Street View) | Fallback (Business Photos + Website) |
|---|---|---|
| **Coverage** | ~70% of London venues have usable Street View | Fills the ~30% gap where Street View is absent or faces the wrong way |
| **Quality** | Unfiltered, shows real current state | Curated, may be outdated or interior-focused |
| **Speed** | One API call per venue | 1–3 Places API calls + 1 HTTP crawl per venue |
| **Rights** | Google-captured, publicly displayed | Venue-published, lower risk |
| **Failure mode** | Might show interiors (fixed by Vision QA) | Might have no exterior candidates at all |

**Why not just use Business Photos as primary?**  
Business Photos are curated by the venue owner. A café might upload 20 photos of latte art and zero of the front door. Street View is unfiltered — it always shows the actual street-facing state. The Vision QA gate filters out the bad Street View images (interiors, side alleys), leaving us with genuine exterior shots.

**Why not skip the Vision QA and trust Street View blindly?**  
~30% of Street View images near London cafés face the wrong way, show interiors through windows, or are obscured by parked vans. Without QA, we'd composite planters onto useless images.

---

## 4. Phase 3: Planter Compositing

### Goal
Take the validated frontage image and the client's planter product photo, and produce a believable visual of the planter installed outside the entrance.

### Dual-Mode Architecture

The system implements **generative-first with deterministic fallback** — the most resilient approach for a production prospecting engine.

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 3: COMPOSITING                                  │
│                    Generative-First + CV-Fallback                            │
└─────────────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────┐
   │  Validated Frontage      │
   │  + Planter Product Photo │
   └──────────┬──────────────┘
              │
              │
              ▼
    ┌───────────────────────┐
    │  use_generative_ai?   │
    │  AND token exists?    │
    └──────────┬────────────┘
               │
         ┌─────┴─────┐
         │           │
       YES         NO
         │           │
         │           ▼
         │    ┌───────────────────────┐
         │    │   CV MODE ONLY       │
         │    │   (Deterministic)    │
         │    │                      │
         │    │   rembg extraction    │
         │    │   resize → 15% width  │
         │    │   perspective skew    │
         │    │   shadow + paste      │
         │    │   at 3 anchors        │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │   OUTPUT: 3 JPGs      │
         │    │   data/composites/    │
         │    └───────────────────────┘
         │
         ▼
    ┌───────────────────────┐
    │   GENERATIVE MODE     │
    │   (Primary Attempt)   │
    │                       │
    │   Replicate           │
    │   FLUX Kontext Pro    │
    │                       │
    │   input_image:        │
    │   frontage.jpg        │
    │                       │
    │   prompt:             │
    │   "Add [description]  │
    │   to the left of      │
    │   the entrance..."    │
    └──────────┬────────────┘
               │
               │
         ┌─────┴─────┐
         │           │
       SUCCESS    ANY ERROR
         │       (402, 429, etc.)
         │           │
         │           ▼
         │    ┌───────────────────────┐
         │    │   FALLBACK TRIGGER    │
         │    │   Log error           │
         │    │   → try CV for same   │
         │    │     venue             │
         │    └──────────┬────────────┘
         │               │
         │               ▼
         │    ┌───────────────────────┐
         │    │   CV FALLBACK         │
         │    │   (Same venue)        │
         │    └──────────┬────────────┘
         │               │
         └───────────────┘
                         │
                         ▼
              ┌───────────────────────┐
              │   OUTPUT              │
              │   data/composites/    │
              │   or                  │
              │   data/composites_    │
              │   generative/         │
              └───────────────────────┘
```

### Mode A: Generative Compositing (Approach A — Hybrid Refinement with Product Fidelity)

**The problem with text-only generative:**

FLUX Kontext Pro with only a text prompt generates its own interpretation of "light-mint ceramic pot." The model may change the shade, pot shape, plant type, or proportions. This violates the requirement: *"must use the client's actual products — not a generic AI approximation."*

**Our solution — Hybrid Refinement (Approach A):**

1. **CV places the real product first:** `rembg` extracts the actual planter photo into a clean RGBA cutout. The CV compositor (using Scene Analysis scale/position/shadow data) places the real planter onto the frontage at the correct size and location. The result is a physically accurate but visually rough composite — the planter is the exact real product, but the edges look pasted-on.

2. **Generative model refines only the boundary region:** The frontage image + the rough composite are sent to the generative model with a mask covering only the pixels around the planter base and edges (a 20% dilation around the planter bounding box). The prompt instructs the model to:
   - Blend the planter naturally into the ground
   - Match local lighting and color temperature
   - Add realistic ground contact and micro-shadows
   - Leave all building elements completely unchanged

3. **Product identity is preserved:** Because the mask only covers the boundary region, the generative model never redraws the planter itself — it only touches the surroundings. The pot shape, plant leaves, and colors remain 100% faithful to the client's product photo.

4. **Implementation path:** On Replicate, this uses a model that supports `input_image` + `mask` + `prompt` (e.g., Stable Diffusion XL inpainting or FLUX inpainting variants). The mask is built automatically from the planter alpha channel + 20% dilation.

> **Implementation Note:** The full hybrid generative refinement (Approach A) is documented in this design but **not yet implemented in the current codebase**. The Replicate free tier does not expose a reliable inpainting model that accepts both a scene image, a binary mask, and a product reference image in a single call. The current production path uses the CV compositor with scene-aware placement (real-world scale, scene-matched shadows, and Gemini-detected ground plane), which is deterministic, costs $0, and preserves the exact product photo. The generative hybrid is the planned upgrade path once an appropriate inpainting API becomes available.

5. **Rate limiting:** Replicate free tier allows ~6 requests/minute with burst of 1. The service implements a global 15-second cooldown between all API calls (`self._last_api_call` tracker), preventing 429 errors across multiple venues.

### Mode B: CV Compositing (OpenCV + PIL)

**How it works:**

1. **Scene Analysis (Gemini Vision):** Before any pixels are moved, Gemini Vision analyzes the validated frontage and returns a JSON scene profile:
   - **Scale:** Identifies a reference object (door, pavement slab, A-board) and returns `pixels_per_meter`. The planter's real-world dimensions (provided by client or auto-estimated from product photo EXIF) are converted to exact pixel size. No more 15% width heuristic.
   - **Lighting:** Detects direction of existing shadows in the scene ("shadows cast to the left"). Returns `shadow_angle_deg` and `shadow_softness` (hard vs. diffuse).
   - **Placement:** Recommends the best bare-ground spot near the entrance that does not block the door, steps, or pavement access. Returns a ranked list of `(x, y)` anchor candidates instead of fixed left/center/right.

2. **Background removal:** `rembg` (U²Net model, ~170MB) strips the background from the product photo, returning a clean RGBA cutout.

3. **Resize:** Planter is scaled to its real-world size using `pixels_per_meter` from Scene Analysis. A 0.6m wide planter at 3m from camera becomes the correct pixel width for that specific photo geometry.

4. **Placement (door-relative):** The planter is positioned relative to the detected door bounding box for accuracy:
   - **Left:** `door_left - door_gap - planter_width/2`
   - **Center:** centered on the door
   - **Right:** `door_right + door_gap - planter_width/2`
   The `door_gap` is user-configurable (default 30cm) and represents the real-world distance from the door frame to the planter edge.

5. **Perspective warp:** OpenCV `warpPerspective` maps the planter into the detected ground-plane perspective. The planter follows the same converging perspective as the pavement.

6. **Scene-matched shadow:** A directional elliptical shadow is cast in the direction and softness reported by Scene Analysis (`shadow_angle_deg`). A tight contact shadow (ambient occlusion) is added where the planter base meets the ground. The shadow is wider than the planter so it peeks out from under the base, visually grounding the object.

7. **Composite:** Paste shadow layers first, then planter, save as JPG at quality 92.

### Resilient Fallback Loop

```python
for each venue:
    if generative enabled and token exists:
        try:
            result = generative_compositor.compose(...)
            count as "generative_success"
        except ANY_ERROR:
            log failure
            try:
                result = cv_compositor.compose(...)
                count as "cv_fallback"
            except:
                skip venue entirely
    else:
        result = cv_compositor.compose(...)
```

**Why this matters:** If Replicate credits run out (402), rate limits hit (429), or the model is temporarily down, the pipeline does not skip venues — it silently falls back to CV and continues. The output is always 3 variations per venue, one way or another.

### Why Not Pure Generative or Pure CV?

| Approach | Why We Rejected It |
|---|---|
| **Pure generative** | Credit exhaustion = zero output. At 5,000 venues/week, a $0.03/venue model costs $150/week. The hybrid mode lets us use generative for high-value pitches and CV for bulk campaigns. |
| **Pure CV** | Cut-and-paste looks "stuck on." The shadow is a generic blur, not scene-matched. For a final client pitch, generative produces dramatically more convincing results. |
| **3D reconstruction** | Requires monocular depth (MiDaS) + meshing + rendering. 10× the code of CV, 5–10s per image, and depth models fail on glass doors and reflective pavement — common in London frontages. The visual gain over CV is invisible at 640×480 thumbnail size. |
| **Stable Diffusion inpainting with mask** | Requires building a precise mask of the ground zone. The mask-building logic is as complex as CV placement anyway. FLUX Kontext Pro eliminates the mask entirely by understanding spatial language. |

---

## 5. Imagery Rights & Ethics

### The Question

We are capturing and reusing photographs of real commercial properties (cafés, restaurants) at commercial scale, without the venue owner's involvement or consent, to create sales materials sent to them unsolicited.

### Our Position

**Primary source: Google Street View**
- Street View imagery is publicly captured by Google and already displayed publicly on maps.google.com
- We are transforming it (adding a planter overlay) for a commercial proposal
- The transformation is non-defamatory and does not misrepresent the venue — it shows a hypothetical improvement
- **Risk:** Untested in UK courts for commercial prospecting use

**Fallback source: Business Photos + Website images**
- These are images the venue has already chosen to publish publicly
- Reusing them in a commercial proposal is lower-risk than Street View

**Mitigations in the design:**
1. **No persistent storage of raw Street View at scale:** Images are cached locally during processing but can be purged post-campaign
2. **Transformation is additive, not distortive:** We don't remove signage, change logos, or misrepresent the business
3. **Opt-out path:** Any venue that objects can be blacklisted in `chain_blacklist.json` and excluded from future scans
4. **Legal review gate:** Before production deployment at 5,000 venues/week, we would seek a UK intellectual property lawyer's opinion on the specific use case

**Alternative if Street View is deemed too risky:**
Shift to **Business Photos only** as the primary source. This reduces coverage (many venues have no Business Photos) but eliminates the Street View rights question entirely.

---

## 6. Rejection Criteria

### What Makes a Venue Candidate Bad Enough to Discard?

| Criterion | Rejection Trigger | Why |
|---|---|---|
| **Chain detection** | Name matches `chain_blacklist.json` | Client targets independents |
| **No street frontage** | `road_proximity_meters > 50` | Can't see the entrance from the pavement |
| **Closed** | `business_status != "OPERATIONAL"` | Waste of outreach effort |
| **Too new** | `user_ratings_total < 10` | Likely unestablished or ghost listing |
| **Too big** | `user_ratings_total > 2,000` | Likely a chain or tourist trap |
| **Non-café name** | Contains "Hotel", "Ltd", "Express", "Drive Thru" | Misclassified by Google Places |

### What Makes a Frontage Image Bad Enough to Discard?

| Criterion | Rejection Trigger | Detected By |
|---|---|---|
| **Interior shot** | Shows tables, chairs, shelves, not exterior | Vision QA checklist #1 |
| **Wrong angle** | Satellite view, side alley, back door | Vision QA checklist #2 |
| **Obscured entrance** | Van, scaffolding, A-board blocks door | Vision QA checklist #3 |
| **Not a photo** | Sketch, logo, menu graphic | Vision QA checklist #4 |
| **Already has planter** | Large flower box or greenery at front | Vision QA checklist #5 |
| **No candidates** | Fallback union produces zero usable images | Pipeline logic |

### What Makes a Composite Bad Enough to Discard?

All outputs run through an automated **Composite Quality Gate** before they ever reach a venue owner. Gates are checked in order; any failure aborts the composite and triggers a retry (different position, different model, or CV-only).

| # | Gate | Metric | Rejection Trigger | Why |
|---|---|---|---|---|
| 1 | **Scale sanity** | Planter width vs. door width | `< 40%` or `> 120%` of door width | A 0.3m planter is too small; a 1.2m planter blocks the entrance |
| 2 | **Placement safety** | Bounding box overlap with door region | Any overlap with detected door mask | Blocking the entrance is unacceptable |
| 3 | **Lighting consistency** | Shadow direction vs. scene shadows | `|angle_diff| > 30°` | Planter shadow pointing left while building shadow points right looks fake |
| 4 | **Product fidelity** | Color histogram distance (reference vs. output) | `Δ > 20%` in HSV space | Generative model changed the pot color or plant type |
| 5 | **Scene integrity** | ORB feature match (input frontage vs. output) | `< 70%` feature retention | Building was altered — windows bricked, signage removed, color changed |
| 6 | **Grounding check** | Planter base vs. detected ground plane | Base is `> 15px` above ground line | Floating planter |
| 7 | **Realism check** | Edge sharpness around planter boundary | Cutout edge > 2px sharp transition | Looks like a sticker, not a real object in the scene |
| 8 | **Invention check** | Object count (planters) in output | `> 1` planter detected | Model invented extra planters |

**Retry policy:**
- Gate 1/2/6 failure → retry with different position from Scene Analysis ranking
- Gate 3 failure → retry with corrected shadow angle from Scene Analysis
- Gate 4 failure → force CV mode (guarantees real product photo)
- Gate 5/7/8 failure → retry generative with tighter mask; if still failing, fall back to CV
- If all retries exhaust → skip venue, log failure reason

---

## 7. Three Selected Venues

These venues were selected **automatically** by the pipeline with no human curation. They passed all heuristic filters and have `street_view_score=100`.

| # | Name | Address | Postcode | Selection Logic |
|---|---|---|---|---|
| 1 | **Lion Gate Café** | Hampton Ct Rd, Molesey, East Molesey | KT8 9BZ | Independent café, operational, Street View within 30m, unobstructed frontage |
| 2 | **Mada Deli** | 11-13 Bridge Rd, Molesey, East Molesey | KT8 9EU | Independent deli/café, operational, clear street-facing entrance, no existing planter |
| 3 | **Cravings Cafe** | 47 Upper Green E, Mitcham | CR4 2PF | Independent café, operational, frontage visible from pavement, bare entrance suitable for planter enhancement |

---

## 8. Rejected Venues & Why

From the same scan session (50 candidates, 27 with SV=100), these venues were rejected by automated filters:

| Venue | Rejected By | Reason |
|---|---|---|
| **GAIL's Bakery Kingston-upon-Thames** | Chain blacklist | "GAIL's" is a UK chain with 50+ locations |
| **Deer Cafe Bistro Kingston** | Vision QA | Street View image shows the interior rather than the street-facing exterior |
| **Chaachi's** | Vision QA | Image shows an indoor retail display of plumbing parts, not a café frontage |
| **Fortunella Café** | Fallback exhaustion | Street View failed QA; fallback had no Business Photos or website candidates |
| **Eleana's** | Vision QA | Image shows a blank wall and street view, not the clear entrance |
| **The French Tarte** | Vision QA | Image taken from inside looking out through glass door and reflections |
| **Surbeanton** | Vision QA | Image shows interior view of wine bottle shelves, not exterior |

**Key point:** One or two rejected attempts before falling back is a perfectly good outcome. The system is designed to fail fast and move on.

---

## 9. Key Technical Decisions

### Decision 1: SQLite Cache Instead of Pure API Calls

**Why:** Re-running the pipeline dozens of times during development would burn through API quotas. SQLite lets us resume, audit, and compare scan sessions over time. The cache is keyed by query hash + category, so "London + cafe" always returns the same cached session unless `force_refresh=True`.

**Trade-off:** Slightly more disk I/O. Benefit: $0 API cost for all development re-runs.

### Decision 2: Vision QA Instead of Blind Trust

**Why:** ~30% of Street View images near London cafés face the wrong way or show interiors. Without QA, we'd waste generative/CV compute on useless images.

**Trade-off:** Adds ~1s per image and requires a Gemini API key. Benefit: Filters out garbage before expensive compositing.

### Decision 3: Generative-First + CV-Fallback Instead of Pure Generative

**Why:** Pure generative is fragile — credits run out, models change, rate limits hit. Pure CV looks fake. The hybrid gives us photorealism when affordable and determinism when it's not.

**Trade-off:** Two code paths to maintain. Benefit: Pipeline never produces zero output due to a vendor issue.

### Decision 4: rembg Instead of Chroma-Key

**Why:** The client provides product photos with varied backgrounds — white, cream, indoor scenes. A simple chroma-key (remove pixels where R>240, G>240, B>240) fails on cream walls and shadow edges. rembg uses a trained U²Net model specifically for product-photo background removal.

**Trade-off:** ~170MB model download on first run. Benefit: Handles all 3 sample planter images correctly.

### Decision 5: Scene Analysis Sub-Phase Before Compositing

**Why:** CV compositing with hardcoded constants (15% width, 78% ground line, generic shadow) produces physically incorrect results — planters float, shadows point the wrong way, scale is arbitrary. Moving these constants into variables derived from the actual photo (via Gemini Vision) makes every composite grounded in the real geometry of the scene.

**What it replaces:**
- Fixed ground line percentage → detected sidewalk edge
- Fixed shadow blur → detected scene shadow softness and direction
- Fixed 15% width heuristic → real-world scale from reference objects
- Fixed left/center/right anchors → ranked placement candidates from detected bare ground

**Trade-off:** Adds ~1–2s per frontage image (one extra Gemini call). Benefit: Composites look physically plausible instead of pasted-on.

**Why not run it in the same Vision QA call:** The QA call is optimized for pass/fail speed with a simple checklist. Scene Analysis requires a more detailed prompt with structured JSON output. We run it only on images that pass QA, so the cost is only incurred for images that will actually be composited.

### Decision 6: No 3D Reconstruction

**Why:** The visual gain of a full depth-estimate + mesh + render pipeline is invisible at 640×480 thumbnail size. The engineering cost (10× code, 5–10s per image) does not justify the marginal improvement over a well-skewed 2D paste.

**When it would make sense:** If the client later wants a real-time 3D configurator where the venue owner rotates the planter with their mouse.

---

## 10. Cost & Scale Estimates

### Per-Venue Cost (Full Pipeline)

| Phase | Operation | Cost | Notes |
|---|---|---|---|
| **Phase 1** | Google Places API | $0.00 | Within $200/mo free tier |
| **Phase 2A** | Street View Static API | $0.00 | Within $200/mo free tier |
| **Phase 2B** | Gemini Vision QA | ~$0.0005 | Flash-Lite is very cheap |
| **Phase 2 Fallback** | Business Photos + Crawl | $0.00 | Places API photos are free |
| **Phase 3 (Gen)** | Replicate FLUX | ~$0.01–$0.03 | Per position; 3 positions = ~$0.03–$0.09 |
| **Phase 3 (CV)** | Local CPU | $0.00 | OpenCV + PIL |
| **Total (Gen)** | | **~$0.04–$0.10** | Per venue |
| **Total (CV)** | | **~$0.0005** | Per venue |

### Scale Projection

| Volume | Generative Cost | CV Cost |
|---|---|---|
| 100 venues/week | ~$4–$10 | ~$0.05 |
| 1,000 venues/week | ~$40–$100 | ~$0.50 |
| 5,000 venues/week | ~$200–$500 | ~$2.50 |

**Recommendation for production:** Use **generative for Tier 1 venues** (high-value, bespoke pitch) and **CV for Tier 2 venues** (bulk email campaign). The `use_generative_ai` toggle makes this trivial to implement.

### Deployment

The project deploys to [Render.com](https://render.com) free tier **without API keys** — the hosted instance runs in Demo Mode and serves cached London results with $0 cost.

**No Render Disk required.** The SQLite database, Street View cache, and composite outputs all live in `planter_app/data/` alongside the static file mounts. The database is ephemeral (resets on redeploy), but the demo cache is repopulated automatically.

**To run Live mode**, clone the repo locally, add `GOOGLE_PLACES_API_KEY` and `GOOGLE_GEMINI_API_KEY` to `.env`, and start the FastAPI server. See [`README.md`](README.md) for full instructions.

---

## Appendix: File Structure

```
planter_app/
  main.py                    # FastAPI entry point + run_pipeline() helper
  config.py                  # Settings from .env (all keys optional for demo)
  orchestrator.py            # PipelineOrchestrator (Phase 1→2→2B→2C→3)
  api/routes.py              # HTTP endpoints
  services/
    venue_discovery_service.py
    image_acquisition_service.py
    fallback_image_service.py
    vision_qa_service.py
    scene_analysis_service.py        # Gemini Vision scene geometry extraction
    compositing_service.py           # CV mode (OpenCV + PIL + rembg)
    generative_compositing_service.py  # Generative mode (Replicate FLUX)
  utils/cache_db.py          # SQLite persistence
  data/
    venue_cache.db           # Cached scan sessions + candidates
    images/                  # Street View downloads
    business_photos/         # Fallback images
    composites/              # CV outputs
    composites_generative/   # Generative outputs
    vision_qa_cache/         # Gemini QA results
    scene_analysis_cache/    # Gemini scene analysis results
    uploads/                 # User-uploaded planter images

sample_plants/               # 3 real planter product images
  plant1.png
  plant2.png
  plant3.jpg

static/                      # Tailwind CSS landing page
  index.html

implementation_doc/          # Phase-by-phase implementation journey
  phase1.md                  # Venue Discovery Design
  phase1_outcome.md          # Phase 1 Results
  phase2.md                  # Image Acquisition & Fallback Design
  phase2_outcome.md          # Phase 2 Results
  phase2_step1_outcome.md    # Street View Outcome
  phase3.md                  # Compositing Design
  orchestration.md           # Pipeline Orchestration Decisions

design.md                    # This document
README.md                    # Setup & run instructions
.env.example               # API key template
```
