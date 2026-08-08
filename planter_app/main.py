"""FastAPI entry point for the planter prospecting engine.

Also exports a convenience function `run_pipeline()` for direct invocation
(e.g. from a CLI script or Jupyter notebook) without starting the HTTP server.
"""

import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from planter_app.api.routes import router
from planter_app.config import Settings
from planter_app.orchestrator import PipelineOrchestrator, PipelineConfig, PipelineResult

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Create FastAPI app
app = FastAPI(
    title="Planter Prospecting Engine",
    description="Automated venue discovery and frontage image acquisition for planter prospecting.",
    version="0.1.0",
)

# Register API routes
app.include_router(router)

# Serve static files (frontend landing page + composite images)
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Serve composite images directly
composites_dir = Path(__file__).parent / "data" / "composites"
if composites_dir.exists():
    app.mount("/composites", StaticFiles(directory=str(composites_dir)), name="composites")

gen_composites_dir = Path(__file__).parent / "data" / "composites_generative"
if gen_composites_dir.exists():
    app.mount("/composites_generative", StaticFiles(directory=str(gen_composites_dir)), name="composites_generative")


@app.get("/")
def root():
    """Serve the frontend landing page."""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {
        "service": "Planter Prospecting Engine",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
        "pipeline": "POST /api/pipeline/run",
    }


# ------------------------------------------------------------------
# Convenience entry-point for direct (non-HTTP) usage
# ------------------------------------------------------------------

def run_pipeline(
    city: str = "London, UK",
    categories: list[str] | None = None,
    quantity: int = 50,
    max_venues_for_images: int = 20,
    mode: str = "demo",
    use_cache: bool = True,
    planter_image_path: Path | str | None = None,
    use_generative_ai: bool = False,
    force_refresh_phase1: bool = False,
    force_refresh_phase2: bool = False,
    force_refresh_fallback: bool = False,
    force_refresh_vision_qa: bool = False,
    planter_width_cm: float = 35.0,
    planter_height_cm: float = 45.0,
    door_gap_cm: float = 30.0,
) -> PipelineResult:
    """
    Run the full pipeline directly (no HTTP server required).

    Args:
        city: City to search in.
        categories: Google Places types to include (default: cafe, restaurant).
        quantity: Target number of candidates.
        max_venues_for_images: How many top venues to acquire images for.
        mode: "demo" (cache-first) or "production" (live APIs).
        use_cache: If True, prefer cached data unless force_refresh is set.
        planter_image_path: Path to planter product image for Phase 3 compositing.
        use_generative_ai: If True and REPLICATE_API_TOKEN is set, use Replicate SDXL
            inpainting instead of CV compositing.
        force_refresh_phase1: Re-run venue discovery (ignore SQLite cache).
        force_refresh_phase2: Re-download Street View images (ignore disk cache).
        force_refresh_fallback: Re-call Business Photos + re-crawl websites.
        force_refresh_vision_qa: Re-run Gemini Vision QA (ignore disk cache).
        planter_width_cm: Real-world width of the planter in cm.
        planter_height_cm: Real-world height of the planter in cm.
        door_gap_cm: Distance from door frame to planter in cm.

    Returns:
        PipelineResult with full summary.
    """
    settings = Settings.from_env()
    orchestrator = PipelineOrchestrator(settings=settings)

    if planter_image_path and isinstance(planter_image_path, str):
        planter_image_path = Path(planter_image_path)

    config = PipelineConfig(
        city=city,
        categories=categories or ["cafe", "restaurant"],
        quantity=quantity,
        max_venues_for_images=max_venues_for_images,
        mode=mode,
        use_cache=use_cache,
        planter_image_path=planter_image_path,
        use_generative_ai=use_generative_ai,
        force_refresh_phase1=force_refresh_phase1,
        force_refresh_phase2=force_refresh_phase2,
        force_refresh_fallback=force_refresh_fallback,
        force_refresh_vision_qa=force_refresh_vision_qa,
        planter_width_cm=planter_width_cm,
        planter_height_cm=planter_height_cm,
        door_gap_cm=door_gap_cm,
    )

    return orchestrator.run(config)
