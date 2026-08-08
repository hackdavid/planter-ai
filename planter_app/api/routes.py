"""FastAPI routes for the planter prospecting pipeline."""

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from planter_app.config import Settings
from planter_app.orchestrator import PipelineOrchestrator, PipelineConfig, PipelineResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pipeline"])

# ------------------------------------------------------------------
# Request / Response schemas
# ------------------------------------------------------------------

class RunPipelineRequest(BaseModel):
    """Request body for running the full pipeline."""

    city: str = Field(default="London, UK", description="City to discover venues in")
    categories: list[str] = Field(
        default=["cafe", "restaurant"],
        description="Google Places types to include",
    )
    quantity: int = Field(default=50, ge=1, le=500, description="Target number of candidates")
    max_api_calls: int = Field(default=250, ge=10, le=2000, description="API safety cap")
    max_venues_for_images: int = Field(
        default=20, ge=1, le=100, description="How many venues to acquire images for"
    )

    # Mode and cache control
    mode: str = Field(
        default="demo",
        description='Pipeline mode: "demo" (cache-first, minimal API calls) or "production" (live APIs)',
    )
    use_cache: bool = Field(
        default=True,
        description="If True, all phases prefer cached data unless force_refresh is set",
    )

    # Phase 3 compositing
    planter_image_path: Optional[str] = Field(
        default=None,
        description="Absolute or relative path to the planter product image (PNG with alpha or JPG). If provided, Phase 3 compositing runs.",
    )

    # Force refresh flags
    force_refresh_phase1: bool = Field(
        default=False,
        description="If True, re-run Phase 1 venue discovery (bypass SQLite cache)",
    )
    force_refresh_phase2: bool = Field(
        default=False,
        description="If True, re-download Street View images (bypass disk cache)",
    )
    force_refresh_fallback: bool = Field(
        default=False,
        description="If True, re-call Business Photos API and re-crawl websites",
    )
    force_refresh_vision_qa: bool = Field(
        default=False,
        description="If True, re-run Gemini Vision QA (ignore disk cache)",
    )


class CompositeVariation(BaseModel):
    """One composited image variation."""

    position: str
    path: str


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _path_to_url(fs_path: str) -> str | None:
    """
    Convert an on-disk composite path to a browser-servable URL.

    Appends a cache-busting query param (?t=<mtime>) so the browser
    fetches the latest version after the compositor overwrites a file.

    The FastAPI app mounts:
        /composites            → data/composites/
        /composites_generative → data/composites_generative/
    """
    if not fs_path:
        return None
    p = Path(fs_path)
    parts = [part.lower() for part in p.parts]

    url: str | None = None
    if "composites_generative" in parts:
        idx = parts.index("composites_generative")
        rel_parts = p.parts[idx + 1 :]
        url = "/composites_generative/" + "/".join(rel_parts).replace("\\", "/")
    elif "composites" in parts:
        idx = parts.index("composites")
        rel_parts = p.parts[idx + 1 :]
        url = "/composites/" + "/".join(rel_parts).replace("\\", "/")
    elif fs_path.startswith("/"):
        url = fs_path

    if url and p.exists():
        mtime = int(p.stat().st_mtime)
        url = f"{url}?t={mtime}"

    return url


class VenueComposite(BaseModel):
    """Composites for a single venue."""

    venue_id: str
    variations: list[CompositeVariation]


class RunPipelineResponse(BaseModel):
    """Response from a pipeline run."""

    status: str
    city: str
    scan_session_id: Optional[int]
    candidates_found: int
    candidates_with_sv100: int
    streetview_images_acquired: int
    sv_images_passed_qa: int
    sv_images_failed_qa: int
    fallback_images_acquired: int
    fallback_images_passed_qa: int
    venues_with_website: int
    venues_unusable: int
    composites: list[VenueComposite]
    details: dict


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.get("/health")
def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "service": "planter-prospecting"}


@router.get("/healthz")
def healthz_check() -> dict:
    """Kubernetes-style health probe."""
    return {"status": "ok", "service": "planter-prospecting"}


@router.post("/pipeline/run", response_model=RunPipelineResponse)
def run_pipeline(
    city: str = Form(default="London, UK"),
    categories: list[str] = Form(default=["cafe", "restaurant"]),
    quantity: int = Form(default=50),
    max_api_calls: int = Form(default=250),
    max_venues_for_images: int = Form(default=20),
    mode: str = Form(default="demo"),
    use_cache: bool = Form(default=True),
    planter_image_path: Optional[str] = Form(default=None),
    planter_image: UploadFile | None = File(None),
    use_generative_ai: bool = Form(default=False),
    force_refresh_phase1: bool = Form(default=False),
    force_refresh_phase2: bool = Form(default=False),
    force_refresh_fallback: bool = Form(default=False),
    force_refresh_vision_qa: bool = Form(default=False),
    planter_width_cm: float = Form(default=35.0),
    planter_height_cm: float = Form(default=45.0),
    door_gap_cm: float = Form(default=30.0),
) -> RunPipelineResponse:
    """
    Run the full prospecting pipeline: venue discovery → image acquisition → fallback → compositing.

    Upload a planter image file via `planter_image` (multipart) **or** pass `planter_image_path`
    as a string pointing to an existing file on disk. The uploaded file takes precedence.
    Set `force_refresh_*` flags to True to bypass cache and hit live APIs.
    """
    logger.info("[API] /pipeline/run | city=%s | mode=%s | planter_image=%s | planter_path=%s",
                city, mode, planter_image.filename if planter_image else None, planter_image_path)

    try:
        settings = Settings.from_env()
        orchestrator = PipelineOrchestrator(settings=settings)

        # Handle uploaded file (takes precedence over planter_image_path)
        planter_path: Path | None = None
        if planter_image and planter_image.filename:
            upload_dir = Path(__file__).parent.parent / "data" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = planter_image.filename.replace("..", "").replace("/", "").replace("\\", "")
            dest = upload_dir / safe_name
            with open(dest, "wb") as f:
                f.write(planter_image.file.read())
            planter_path = dest
            logger.info("[API] Saved uploaded planter image to %s", dest)
        elif planter_image_path:
            planter_path = Path(planter_image_path)

        config = PipelineConfig(
            city=city,
            categories=categories,
            quantity=quantity,
            max_api_calls=max_api_calls,
            max_venues_for_images=max_venues_for_images,
            mode=mode,
            use_cache=use_cache,
            planter_image_path=planter_path,
            use_generative_ai=use_generative_ai,
            force_refresh_phase1=force_refresh_phase1,
            force_refresh_phase2=force_refresh_phase2,
            force_refresh_fallback=force_refresh_fallback,
            force_refresh_vision_qa=force_refresh_vision_qa,
            planter_width_cm=planter_width_cm,
            planter_height_cm=planter_height_cm,
            door_gap_cm=door_gap_cm,
        )

        result = orchestrator.run(config)

        return RunPipelineResponse(
            status=result.status,
            city=result.city,
            scan_session_id=result.scan_session_id,
            candidates_found=result.candidates_found,
            candidates_with_sv100=result.candidates_with_sv100,
            streetview_images_acquired=result.streetview_images_acquired,
            sv_images_passed_qa=result.sv_images_passed_qa,
            sv_images_failed_qa=result.sv_images_failed_qa,
            fallback_images_acquired=result.fallback_images_acquired,
            fallback_images_passed_qa=result.fallback_images_passed_qa,
            venues_with_website=result.venues_with_website,
            venues_unusable=result.venues_unusable,
            composites=[
                VenueComposite(
                    venue_id=c["venue_id"],
                    variations=[
                        CompositeVariation(position=v["position"], path=_path_to_url(v["path"]) or "")
                        for v in c["variations"]
                    ],
                )
                for c in result.composites
            ],
            details=result.details,
        )

    except Exception as exc:
        logger.exception("[API] Pipeline run failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/candidates")
def list_candidates(city: str = "London, UK", limit: int = 50) -> list[dict]:
    """List cached candidates for a given city."""
    from planter_app.utils.cache_db import CacheDB

    settings = Settings.from_env()
    db = CacheDB(settings.cache_db_path)

    with db._connection() as conn:
        row = conn.execute(
            "SELECT id FROM scan_sessions WHERE query = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            (city,),
        ).fetchone()
        if not row:
            return []

        rows = conn.execute(
            """
            SELECT place_id, name, address, street_view_score, road_proximity_meters
            FROM candidate_venues
            WHERE scan_session_id = ? AND is_candidate = 1
            ORDER BY street_view_score DESC, user_ratings_total DESC
            LIMIT ?
            """,
            (row["id"], limit),
        ).fetchall()

    return [dict(r) for r in rows]


@router.get("/images")
def list_images() -> dict:
    """List all locally cached image directories."""
    from pathlib import Path

    base = Path(__file__).parent.parent / "data"
    sources = {
        "streetview": len(list((base / "images").glob("*/metadata.json"))),
        "business_photos": len(list((base / "business_photos").glob("*/metadata.json"))),
        "fallback_union": len(list((base / "fallback_images").glob("*/metadata.json"))),
        "composites": len(list((base / "composites").glob("*"))),
    }
    return {"cached_venues": sources}


@router.get("/demo")
def demo() -> dict:
    """
    Zero-key demo endpoint.

    Returns pre-computed pipeline results for the 3 demo venues
    without making any API calls. Reviewers can test this instantly
    without providing Google Places, Gemini, or Replicate keys.
    """
    from pathlib import Path

    data_dir = Path(__file__).parent.parent / "data"

    # Detect which composites exist (CV or generative)
    venues = [
        {
            "venue_id": "ChIJA5PfGEALdkgRmSwijMUi3_8",
            "venue_name": "Lion Gate Café",
            "address": "Hampton Ct Rd, Molesey, East Molesey KT8 9BZ, UK",
            "positions": ["left", "center", "right"],
        },
        {
            "venue_id": "ChIJR6kvFRELdkgR7deUVt8nLws",
            "venue_name": "Mada Deli",
            "address": "11-13 Bridge Rd, Molesey, East Molesey KT8 9EU, UK",
            "positions": ["left", "center", "right"],
        },
        {
            "venue_id": "ChIJp_wEtEAGdkgRev5b74Ls4N8",
            "venue_name": "Cravings Cafe",
            "address": "47 Upper Green E, Mitcham CR4 2PF, UK",
            "positions": ["left", "center", "right"],
        },
    ]

    # Build composite paths based on what actually exists on disk
    composites = []
    for v in venues:
        variations = []
        for pos in v["positions"]:
            # Check generative output first, then CV
            gen_path = data_dir / "composites_generative" / v["venue_id"] / f"planter_{pos}.jpg"
            cv_path = data_dir / "composites" / v["venue_id"] / f"planter_{pos}.jpg"
            if gen_path.exists():
                mtime = int(gen_path.stat().st_mtime)
                url = f"/composites_generative/{v['venue_id']}/planter_{pos}.jpg?t={mtime}"
                mode = "GENERATIVE"
            elif cv_path.exists():
                mtime = int(cv_path.stat().st_mtime)
                url = f"/composites/{v['venue_id']}/planter_{pos}.jpg?t={mtime}"
                mode = "CV"
            else:
                url = None
                mode = "MISSING"
            variations.append({
                "position": pos,
                "path": url,
                "mode": mode,
            })
        composites.append({
            "venue_id": v["venue_id"],
            "venue_name": v["venue_name"],
            "address": v["address"],
            "variations": variations,
        })

    return {
        "message": "Zero-key demo — no API calls were made to produce this response.",
        "pipeline_summary": {
            "city": "London, UK",
            "candidates_found": 50,
            "candidates_with_sv100": 27,
            "sv_images_acquired": 10,
            "sv_passed_qa": 3,
            "sv_failed_qa": 7,
            "fallback_processed": 7,
            "composites_produced": sum(1 for v in composites if any(vv["path"] for vv in v["variations"])),
        },
        "composites": composites,
        "next_steps": {
            "add_keys": "Set GOOGLE_PLACES_API_KEY, GOOGLE_GEMINI_API_KEY, and optionally REPLICATE_API_TOKEN in .env",
            "run_full_pipeline": "POST /api/pipeline/run with planter_image_path set",
            "run_demo_mode": "POST /api/pipeline/run with mode='demo' and use_cache=true",
        },
    }
