# Phase 3: Planter Compositing

## Overview

Phase 3 takes a validated frontage image (from Phase 2) and a user-uploaded planter product image, and composites the planter onto the frontage photograph so it appears to be sitting on the pavement in front of the entrance.

The system supports **two compositing modes**:
1. **Generative AI** (Replicate FLUX Kontext Pro) — edits the real frontage photo using natural-language instructions
2. **Deterministic CV** (OpenCV + PIL) — cuts out the planter and pastes it onto the frontage with perspective skew and shadow

Both modes produce 2–3 composed variations per venue (left / center / right of entrance). The pipeline automatically falls back from generative to CV if the generative service fails for any reason (rate limits, credit exhaustion, model errors).

---

## The Two Compositing Modes

### Mode A: Generative AI — Replicate FLUX Kontext Pro

**How it works:**

1. **Load inputs:** Frontage JPG + Planter product photo (used for description)
2. **Build spatial prompt:** Map the requested position (left/center/right) to a natural-language instruction:
   - *"Add a photorealistic outdoor potted plant with [description] placed on the ground to the left of the storefront entrance. Keep the building facade, signage, windows, street, and all existing objects completely unchanged."*
3. **Send to Replicate:** Pass the frontage image as `input_image` + the prompt to `black-forest-labs/flux-kontext-pro`
4. **Model edits the scene:** FLUX Kontext Pro is an image-editing model. It renders the planter directly into the scene with matching lighting, shadows, and scale — no mask or alpha channel needed.
5. **Output:** 1 edited JPG per position

**Why we built this:**

| Criterion | Generative Mode |
|---|---|
| **Realism** | Photorealistic — the planter is natively rendered into the scene, not cut-and-pasted |
| **Lighting** | Automatically matches scene daylight, shadow direction, and ambient color |
| **Scale** | The model interprets "on the sidewalk" and renders correct proportions relative to the door/pavement |
| **No masking** | No need to manually mask the ground zone; the model understands spatial language |
| **Product fidelity** | Prompt includes the exact product description from the reference photo filename mapping |

**Limitations:**
- Requires a Replicate API token with available credits
- Free tier is rate-limited (~6 requests/minute with burst of 1)
- Credits can be exhausted on heavy batch runs
- The model may subtly reinterpret the exact product look (not pixel-perfect)
- ~5–10 seconds per image (network + GPU inference time)

---

### Mode B: Deterministic Computer Vision — OpenCV + PIL

**How it works:**

1. **Extract planter:** Use `rembg` (U²Net model) to remove the background from the product photo, returning a clean RGBA cutout
2. **Resize planter:** Scale to ~15% of frontage width based on real-world proportions
3. **Placement zones:** Define 3 anchor points along the bottom edge of the frontage image:
   - Left: ~22% from left edge
   - Center: ~50% from left edge
   - Right: ~78% from left edge
4. **Perspective skew:** Apply a perspective transform (OpenCV `warpPerspective`) to tilt the planter's bottom edge so it matches the ground plane angle
5. **Shadow:** Add a soft elliptical shadow beneath the planter using PIL (80 opacity black, Gaussian blur)
6. **Paste:** Composite the planter + shadow onto the frontage at each anchor point
7. **Output:** 3 composed JPGs

**Why we keep this as the fallback:**

| Criterion | CV Mode |
|---|---|
| **Cost** | $0.00 per image — runs locally on CPU |
| **Speed** | < 1 second for 3 variations |
| **Deterministic** | Same input = same output every time |
| **Scalable** | Batch 1,000 venues in minutes on a single CPU |
| **Offline capable** | No internet required after images are downloaded |
| **Pixel-perfect product** | The exact product photo is preserved, not reinterpreted |

**Limitations:**
- Cut-and-paste look — the planter can appear "stuck on" rather than "in" the scene
- Generic shadow — not physically matched to the scene's actual light direction
- Fixed placement — does not avoid obstacles like cars, A-boards, or existing planters

---

## Hybrid Architecture: Generative-First with CV Fallback

The orchestrator implements a **resilient two-tier strategy**:

```
for each usable venue:
    if generative mode is enabled AND token exists:
        try:
            call Replicate FLUX Kontext Pro
            → if success: use generative result, increment gen_count
        except ANY error (402, 429, timeout, network):
            log failure
            → try CV compositor for same venue
            → if CV succeeds: increment cv_fallback_count
            → if CV also fails: skip venue
    else:
        use CV compositor directly
```

**Why this architecture:**

- **Never lose a venue** — if generative fails (credits, rate limits, downtime), CV guarantees output
- **Cost-aware** — generative is used when affordable; CV is the safety net
- **Metrics tracked** — `generative_success` and `cv_fallback` are reported in pipeline results
- **User-configurable** — toggle `use_generative_ai` in `PipelineConfig` or `.env`

---

## Key Technical Discoveries

### 1. Replicate API Parameter Name

Replicate's `flux-kontext-pro` model expects `input_image` (not `image`) as the parameter key. Passing `image` causes the model to ignore the frontage photo entirely and generate random unrelated scenes. This was discovered by introspecting the model's OpenAPI schema via the Replicate API.

### 2. Product Description Mapping

Since the generative model uses text prompts rather than pixel-perfect cutouts, we map each planter filename to a detailed description:

| Filename | Prompt Description |
|---|---|
| `plant1.png` | lush green leaves in a smooth light-mint ceramic pot |
| `plant2.png` | compact green foliage in a modern white cylindrical planter |
| `plant3.jpg` | tall snake plant in a minimalist white pot with wooden stand |

This keeps the generated planter visually close to the client's actual product without requiring IP-Adapter or reference-image conditioning.

### 3. Rate Limiting

Replicate's free tier enforces:
- ~6 predictions per minute
- Burst of 1 request
- Reduced limits until a payment method is added

The service implements **global rate-limit tracking** (`self._last_api_call`) with a 15-second cooldown between every API call, preventing 429 errors across multiple venues.

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **Generative-first, CV-fallback** | Best of both worlds: photorealism when credits allow, determinism when they don't |
| **3 variations per venue** | Left, center, right. Gives options without overwhelming the client |
| **rembg for CV extraction** | Far superior to simple chroma-key on white backgrounds. Handles indoor scenes, cream walls, and complex edges |
| **No mask for generative** | FLUX Kontext Pro understands spatial language. A mask adds complexity without benefit for this model |
| **Natural-language positions** | "to the left of the entrance" is more reliable than pixel coordinates for a generative editor |
| **Per-venue fallback** | If generative fails for Venue A, CV still runs for Venue A. Venue B is not affected |

---

## Expected Input / Output

### Input

```json
{
  "venue_id": "ChIJA5PfGEALdkgRmSwijMUi3_8",
  "frontage_image": "data/images/ChIJA5PfGEALdkgRmSwijMUi3_8/streetview_primary_90.jpg",
  "planter_image": "sample_plants/plant1.png",
  "use_generative_ai": true
}
```

### Output — Generative Mode

```json
{
  "venue_id": "ChIJA5PfGEALdkgRmSwijMUi3_8",
  "venue_name": "The Lion Gate Cafe",
  "model": "black-forest-labs/flux-kontext-pro",
  "composites": [
    {
      "position": "left",
      "path": "data/composites_generative/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_left.jpg"
    },
    {
      "position": "center",
      "path": "data/composites_generative/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_center.jpg"
    },
    {
      "position": "right",
      "path": "data/composites_generative/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_right.jpg"
    }
  ]
}
```

### Output — CV Mode

```json
{
  "venue_id": "ChIJA5PfGEALdkgRmSwijMUi3_8",
  "venue_name": "The Lion Gate Cafe",
  "composites": [
    {
      "position": "left",
      "path": "data/composites/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_left.jpg",
      "scale_ratio": 0.15,
      "anchor_x": 96,
      "anchor_y": 420
    },
    {
      "position": "center",
      "path": "data/composites/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_center.jpg",
      "scale_ratio": 0.15,
      "anchor_x": 320,
      "anchor_y": 420
    },
    {
      "position": "right",
      "path": "data/composites/ChIJA5PfGEALdkgRmSwijMUi3_8/planter_right.jpg",
      "scale_ratio": 0.15,
      "anchor_x": 544,
      "anchor_y": 420
    }
  ]
}
```

---

## Cost Estimate

### Generative Mode (Replicate)

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| FLUX Kontext Pro inference | 3 | ~$0.01–$0.03 | ~$0.03–$0.09 per venue |
| **Total per venue** | | | **~$0.03–$0.09** |
| **Total per 100 venues** | | | **~$3–$9** |

### CV Mode (Local)

| Item | Count | Unit Cost | Total |
|---|---|---|---|
| rembg background removal | 1 | $0.00 | $0.00 |
| OpenCV perspective transform | 3 | $0.00 | $0.00 |
| PIL compositing + shadow | 3 | $0.00 | $0.00 |
| **Total** | | | **$0.00** |

---

## File Structure

### Generative outputs

```
data/composites_generative/
  {venue_id}/
    planter_left.jpg
    planter_center.jpg
    planter_right.jpg
    metadata.json
```

### CV outputs

```
data/composites/
  {venue_id}/
    planter_left.jpg
    planter_center.jpg
    planter_right.jpg
    metadata.json
```

---

## Environment Configuration

```bash
# .env
REPLICATE_API_TOKEN=r8_YourTokenHere          # Required for generative mode
REPLICATE_MODEL=black-forest-labs/flux-kontext-pro  # Switchable per model
COMPOSITING_MODE=cv                             # "cv" or "generative"
```

Toggle at runtime:
```python
run_pipeline(
    planter_image_path="sample_plants/plant1.png",
    use_generative_ai=True,   # tries generative, falls back to CV on error
)
```

---

## Next Steps (Post-Prototype)

1. **IP-Adapter / product lock-in:** For stricter product fidelity, switch to a model that supports image-conditioning (e.g., IP-Adapter on SDXL) so the exact planter pixels are preserved while the scene is still generatively edited
2. **Object-aware placement:** Use YOLOv8-nano to detect cars, A-boards, and existing planters in the frontage, then avoid those regions in both generative prompts and CV anchor selection
3. **Ground-plane detection:** Use Canny + Hough line detection to find the actual curb/sidewalk edge instead of hardcoded bottom-percentage anchors
4. **Single-position mode:** For high-volume campaigns with tight credit budgets, generate only the "center" position instead of 3, tripling venue coverage per dollar
5. **Local diffusion:** Run Stable Diffusion + ControlNet + IP-Adapter on a local GPU or Google Colab to eliminate per-image API costs entirely
