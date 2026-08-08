# Planter Prospecting Engine

Automated venue discovery and visual prospecting for design-led outdoor planters.

Finds independent cafés and restaurants with bare frontages, captures a photo of the actual entrance, and produces a realistic "planter installed" visual — convincing enough to send to the venue owner as a cold-outreach asset.

> **⚡ Two Pipeline Modes**
>
> | Mode | Cost | What it does |
> |---|---|---|
> | **Demo** (default) | **$0 API calls** | Reads from SQLite + disk cache. Phase 1–2 use cached data. Phase 3 still runs live compositing with your uploaded planter image. |
> | **Live** | ~$0.05–$0.10 per venue | Hits Google Places API, Street View API, Gemini Vision, and optionally Replicate. |
>
> **To run Live mode, add your API keys to `.env`** (see [API Keys](#api-keys-required) below). Without keys, the pipeline runs in Demo mode automatically and returns cached results.

---

## What It Does

| Phase | Technology | Purpose |
|---|---|---|
| **Phase 1 — Venue Discovery** | Google Places API + SQLite cache | Searches independent cafés/restaurants, filters out chains, scores by road proximity |
| **Phase 2 — Image Acquisition** | Street View Static API (primary) + Business Photos API + Website Crawler (fallback) | Captures a street-facing photo of each entrance |
| **Phase 2B — Vision QA** | Gemini 3.5 Flash-Lite | Validates every image: rejects interiors, wrong angles, obscured entrances |
| **Phase 2C — Scene Analysis** | Gemini 3.5 Flash-Lite | Extracts real-world scale (`pixels_per_meter`), ground plane, shadow direction, and door location for physically accurate placement |
| **Phase 3 — Compositing** | **CV Mode** (OpenCV + PIL + rembg) with optional **Generative Mode** (Replicate FLUX) fallback | Places the client's actual planter photo onto the frontage at correct scale, perspective, and lighting |

**Read the full technical design in [`design.md`](design.md).**

**Read the phase-by-phase implementation journey in [`implementation_doc/`](implementation_doc/):**
- [`phase1.md`](implementation_doc/phase1.md) — Venue Discovery Design
- [`phase2.md`](implementation_doc/phase2.md) — Image Acquisition & Fallback Design
- [`phase3.md`](implementation_doc/phase3.md) — Compositing Design
- [`orchestration.md`](implementation_doc/orchestration.md) — Pipeline Orchestration Decisions

---

## Quick Start

### Prerequisites

- Python 3.10+
- `pip`
- API keys (only needed for **Live** mode — Demo mode works zero-key)

### 1. Clone & Install

```bash
git clone <repo-url>
cd planter
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys (only needed for Live mode)
```

### 3. Run the Pipeline

```python
from planter_app.main import run_pipeline

result = run_pipeline(
    city="London, UK",
    max_venues_for_images=10,
    planter_image_path="sample_plants/plant1.png",
    use_generative_ai=False,   # CV mode is deterministic and free
)

print(f"Status: {result.status}")
print(f"Composites: {len(result.composites)} venues")
```

Or run via the standalone test scripts:

```bash
# Test CV compositing with scene analysis
python tests/test_improved_compose.py

# Test generative compositing (requires Replicate token)
python tests/test_generative_standalone.py
```

---

## API Keys Required

> **You only need these for Live mode.** Demo mode runs entirely from cache with $0 API cost.

| Key | Source | Free Tier | Required For |
|---|---|---|---|
| `GOOGLE_PLACES_API_KEY` | [Google Cloud Console](https://console.cloud.google.com) | $200/month credit | Venue discovery + Street View |
| `GOOGLE_GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com) | Generous free tier | Vision QA + Scene Analysis |
| `REPLICATE_API_TOKEN` | [Replicate Account](https://replicate.com/account/api-tokens) | ~$5 credit + rate limits | **Optional** — enables generative compositing |

**Note:** The pipeline works fully with only the Google keys. Replicate is optional — if omitted, the system falls back to deterministic CV compositing automatically.

---

## Three Demo Venues

These venues were selected **automatically** by the pipeline with zero human curation:

| Venue | Address | Postcode | Why Selected |
|---|---|---|---|
| **Lion Gate Café** | Hampton Ct Rd, Molesey, East Molesey | KT8 9BZ | Independent, street-facing, Street View score = 100 |
| **Mada Deli** | 11-13 Bridge Rd, Molesey, East Molesey | KT8 9EU | Independent deli, clear entrance, no existing planter |
| **Cravings Cafe** | 47 Upper Green E, Mitcham | CR4 2PF | Independent café, bare frontage, suitable for enhancement |

---

## Project Structure

```
planter_app/
  main.py              # FastAPI entry point + run_pipeline() helper
  config.py            # Settings from .env (all keys optional for demo)
  orchestrator.py      # Full pipeline: Phase 1 → 2 → 2B → 2C → 3
  api/routes.py        # HTTP endpoints (/api/pipeline/run, /api/demo, /api/health)
  services/
    venue_discovery_service.py
    image_acquisition_service.py
    fallback_image_service.py
    vision_qa_service.py
    scene_analysis_service.py     # Gemini Vision scene geometry extraction
    compositing_service.py        # CV mode (OpenCV + PIL + rembg)
    generative_compositing_service.py  # Generative mode (Replicate FLUX)
  utils/cache_db.py    # SQLite persistence + disk cache
  data/                # Cache, images, composites, uploads

sample_plants/         # 3 real planter product images
static/                # Tailwind CSS landing page
implementation_doc/    # Phase-by-phase design docs
  phase1.md
  phase1_outcome.md
  phase2.md
  phase2_outcome.md
  phase2_step1_outcome.md
  phase3.md
  orchestration.md
design.md              # Full technical design document (40KB)
```

---

## Key Features

- **Fully automated** — No manual eyeballing of candidates or frontage images
- **Cache-first** — SQLite + disk cache means re-running costs $0 in API calls in Demo mode
- **Scene-aware CV compositing** — Uses Gemini Vision to extract real-world scale, ground plane, and shadow direction for physically accurate placement
- **Dual-mode compositing** — Generative AI (FLUX) for photorealism, CV for determinism and zero cost
- **Resilient fallback** — If generative fails (credits, rate limits), CV compositor runs automatically for the same venue
- **Vision QA gate** — Gemini checks every frontage image before compositing; rejects interiors, wrong angles, and obscured entrances
- **Rate-limit aware** — Global cooldown between Replicate API calls prevents 429 errors

---

## Assumptions & Limitations

1. **Google Places API coverage** — Assumes the target city has sufficient café/restaurant listings in Google Places. Rural areas may have sparse coverage.
2. **Street View availability** — ~70% of London venues have usable Street View; the remaining 30% rely on fallback sources (Business Photos + Website Crawler). Coverage varies by city.
3. **Planter photos** — Assumes the client provides product photos on a neutral or transparent background. `rembg` handles most cases automatically, but very complex backgrounds may need manual cleanup.
4. **Scene Analysis accuracy** — Gemini Vision estimates pixels-per-meter, door location, and ground plane from a single photo. These are approximations, not survey-grade measurements.
5. **Imagery rights** — See `design.md` Section 5 for our position on reusing Street View and Business Photos commercially. Before production deployment at scale, seek legal counsel.
6. **Scale** — CV mode scales to thousands of venues on a single CPU. Generative mode is gated by API credits and rate limits.
7. **Demo mode** — Demo mode returns cached results from a previous London scan. To test a new city, switch to **Live** mode with valid API keys.

---

## Deployment

### Render.com (Recommended)

This project is configured for deployment on [Render](https://render.com) with a free-tier web service.

**1. Create a new Web Service**
- Connect your GitHub repo
- Runtime: `Python 3`
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn planter_app.main:app --host 0.0.0.0 --port $PORT`

**2. No API keys needed on Render**

The deployed instance runs **Demo Mode only** — it returns cached London results with $0 API cost. No environment variables are required.

If you want to run **Live mode** on Render, add these optional variables:

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_PLACES_API_KEY` | Optional | Google Places + Street View (for Live mode) |
| `GOOGLE_GEMINI_API_KEY` | Optional | Vision QA + Scene Analysis (for Live mode) |
| `REPLICATE_API_TOKEN` | Optional | Generative compositing (optional even in Live) |
| `PYTHON_VERSION` | No | `3.10` |

**3. Static files & images**

The FastAPI app automatically mounts:
- `/static` → `static/` directory (landing page, CSS, JS)
- `/composites` → `planter_app/data/composites/` (CV output images)
- `/composites_generative` → `planter_app/data/composites_generative/` (generative output images)

Images and the SQLite database live in the same `planter_app/data/` directory — no Render Disk needed. The database is ephemeral (resets on redeploy) but the demo cache is repopulated automatically.

**4. Zero-key demo**

Reviewers can test the pipeline instantly without API keys:
```bash
curl https://your-app.onrender.com/api/demo
```

Or open the landing page and click **Run Pipeline** with **Demo** mode selected.

**5. Run Live mode locally**

To query new cities in real time, clone the repo locally and add your keys:

```bash
cp .env.example .env
# Add GOOGLE_PLACES_API_KEY and GOOGLE_GEMINI_API_KEY
pip install -r requirements.txt
uvicorn planter_app.main:app --reload
```

Then select **Live** mode in the landing page form.

---

## License

This is a technical hiring test submission. Not licensed for production use.
