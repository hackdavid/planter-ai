# Phase 2 Step 1 Outcome: Image Acquisition

**Date:** 2026-08-08
**Test Query:** London, UK
**Test Categories:** `cafe`, `restaurant`
**Venues Processed:** 20 (top candidates with `street_view_score = 100`)
**API Calls:** 40 (2 images per venue)
**Cost:** ~$0.28

---

## What happened

### Phase 1 recap (prerequisite)

With `max_api_calls=250`, Phase 1 completed a partial scan of London and produced:

- **1,658 raw venues**
- **474 candidates** after deterministic filtering
- **~120 candidates** scored for Street View coverage before the API cap
- **20 candidates** with `street_view_score = 100` (panorama within 20m)

The 20 `score=100` candidates were selected for Phase 2 Step 1.

### Phase 2 Step 1 — Cold Cache

For each of the 20 candidates:

1. Loaded `panorama_lat`, `panorama_lng`, and `road_proximity_meters` from the Phase 1 SQLite cache
2. Computed the bearing from the panorama to the venue using the haversine formula
3. Requested a **primary image** from Street View Static API (`heading = computed_bearing`, `pitch=0`, `fov=60`)
4. Requested a **validation image** (`heading = computed_bearing + 20°`, `pitch=0`, `fov=60`)
5. Saved both images and a `metadata.json` to local disk

**All 20 venues downloaded successfully.**

### Phase 2 Step 1 — Warm Cache

Running the same `acquire()` call a second time:

- **0 API calls**
- **0.02 seconds**
- All 20 venues returned from local disk instantly

---

## Important design note: the image is NOT guaranteed to be street-facing

A `street_view_score = 100` only guarantees that a Street View panorama exists within 20 meters of the venue. It does **not** guarantee that:

- The camera is facing the venue's entrance
- The entrance is unobstructed (parked vehicles, scaffolding, etc.)
- The image is daytime / well-lit / usable

**The computed bearing aims the camera at the venue, but the actual image might show:**
- The side wall of the building (if the venue is on a corner)
- The back of the building (if the panorama is in an alley)
- A tree, bus, or lorry blocking the facade
- A dark or blurry frame

**This is expected and acceptable.** Phase 2 Step 2 (Vision QA) is the filter that catches these cases.

---

## Pipeline flow from here

```
Phase 2 Step 1: Images downloaded
  ↓
Phase 2 Step 2: Vision QA on both images
  ↓
├─ Both angles PASS
│     ↓
│   Mark "accepted" → proceed to Phase 3 (compositing)
│
├─ One angle PASS, one angle FAIL
│     ↓
│   Accept the passing angle → proceed to Phase 3
│
└─ Both angles FAIL
      ↓
  → Trigger Fallback Strategy (one last try)
      ↓
  Fallback 1: Google Business Photos exterior image
      ↓
  ├─ PASS → proceed to Phase 3
  └─ FAIL → Fallback 2
      ↓
  Fallback 2: Venue website exterior image
      ↓
  ├─ PASS → proceed to Phase 3
  └─ FAIL → Mark "unusable", skip venue entirely
```

**Key principle:** A venue gets **one last try** through the fallback chain if Vision QA rejects both Street View angles. If the fallback also fails, the venue is permanently rejected. There is no retry loop.

---

## Sample acquired images

| Venue | Primary Heading | Validation Heading | Road Proximity | Images |
|---|---|---|---|---|
| Deer Cafe Bistro Kingston | 199° | 219° | 0.0 m | ✅ 2 images |
| Chaachi's | 345° | 5° | 0.0 m | ✅ 2 images |
| Fortunella Café | 198° | 218° | 0.0 m | ✅ 2 images |
| Eleana's | 16° | 36° | 0.0 m | ✅ 2 images |
| The French Tarte | 36° | 56° | 0.0 m | ✅ 2 images |
| Surbeanton | 180° | 200° | 0.0 m | ✅ 2 images |
| GAIL's Bakery Kingston | 144° | 164° | 0.0 m | ✅ 2 images |
| Lion Gate Café | 210° | 230° | 0.0 m | ✅ 2 images |
| Mada Deli | 158° | 178° | 0.0 m | ✅ 2 images |
| Cravings Cafe | 340° | 0° | 0.0 m | ✅ 2 images |

(10 more venues also acquired successfully.)

---

## File structure on disk

```
planter_app/data/images/
  ChIJK8Bb9WcLdkgRrUZC97T0XWQ/
    streetview_primary_199.jpg        (64 KB)
    streetview_validation_219.jpg     (64 KB)
    metadata.json                     (670 bytes)
  ChIJA3NOoqsHdkgReHOikJIlCXg/
    streetview_primary_345.jpg        (64 KB)
    streetview_validation_5.jpg       (64 KB)
    metadata.json
  ... (18 more venues)
```

Each `metadata.json` contains:
- `venue_id`, `place_id`, `name`, `address`
- `lat`, `lng` (venue location)
- `panorama_lat`, `panorama_lng` (Street View car location)
- `computed_bearing`, `primary_heading`, `validation_heading`
- `road_proximity_meters`
- `requested_at` (ISO timestamp)
- `primary_image_exists`, `validation_image_exists`

---

## Cost summary

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| Street View Static images (primary) | 20 | ~$0.007 | ~$0.14 |
| Street View Static images (validation) | 20 | ~$0.007 | ~$0.14 |
| **Total Phase 2 Step 1 cost** | | | **~$0.28** |

---

## Production readiness note

For the production system at 5,000/week:

- `max_api_calls` in Phase 1 would be increased to 800–1,000 for full London coverage
- This would yield ~150–250 candidates with `street_view_score = 100`
- Phase 2 Step 1 would process all of them, producing ~300–500 images
- Estimated weekly cost for image acquisition: **~$2–4**
- File-system caching ensures that any re-run or QA retry uses 0 API calls

---

## Next step

Phase 2 Step 2: **Vision QA** — run the 40 acquired images through a multimodal model to verify that each one actually shows the venue's entrance, has usable ground space, and is suitable for planter compositing.
