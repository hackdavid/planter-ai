"""Pipeline orchestrator: runs Phase 1 → Phase 2 → Fallback sequentially."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB
from planter_app.services import (
    VenueDiscoveryService,
    ImageAcquisitionService,
    FallbackImageService,
    VisionQAService,
    SceneAnalysisService,
    CompositingService,
    GenerativeCompositingService,
)

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """Configuration for a full pipeline run."""

    city: str = "London, UK"
    categories: list[str] = field(default_factory=lambda: ["cafe", "restaurant"])
    quantity: int = 50
    max_api_calls: int = 250
    max_venues_for_images: int = 20

    # Mode: "demo" (cache-first, no API calls unless cache miss) or "production" (live APIs)
    mode: str = "demo"

    # Master cache flag — when True, all phases prefer cache unless force_refresh is explicitly set
    use_cache: bool = True

    # Force refresh flags — bypass cache for each phase (overrides use_cache)
    force_refresh_phase1: bool = False
    force_refresh_phase2: bool = False
    force_refresh_fallback: bool = False
    force_refresh_vision_qa: bool = False

    # Phase 3: Compositing
    planter_image_path: Path | None = None

    # Compositing mode: False = CV-based (OpenCV/PIL), True = generative (Replicate SDXL)
    use_generative_ai: bool = False

    # Planter real-world dimensions (in cm) for accurate scaling in the scene
    planter_width_cm: float = 35.0
    planter_height_cm: float = 45.0
    door_gap_cm: float = 30.0

    # Rate limiting
    rate_limit_delay: float = 0.3


@dataclass
class PipelineResult:
    """Structured result from a full pipeline run."""

    status: str  # "success" | "partial" | "failed"
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
    composites: list[dict]
    details: dict


class PipelineOrchestrator:
    """
    Orchestrates the full planter prospecting pipeline:
      Phase 1 → Venue Discovery
      Phase 2 → Street View Image Acquisition
      Phase 2 Fallback → Business Photos + Website Crawler
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = CacheDB(settings.cache_db_path)
        self._discovery = VenueDiscoveryService(settings=settings, db=self.db)
        self._images = ImageAcquisitionService(settings=settings, db=self.db)
        self._fallback: FallbackImageService | None = None
        self._vision: VisionQAService | None = None
        self._scene: SceneAnalysisService | None = None
        self._compositor = CompositingService()
        self._gen_compositor: GenerativeCompositingService | None = None

        # Initialize API-backed services only if keys are present
        if settings.google_places_api_key:
            self._fallback = FallbackImageService(api_key=settings.google_places_api_key)
        else:
            logger.info("[ORCHESTRATOR] Fallback image service unavailable — no GOOGLE_PLACES_API_KEY")

        if settings.google_gemini_api_key:
            try:
                self._vision = VisionQAService(
                    api_key=settings.google_gemini_api_key,
                    model="gemini-3.5-flash-lite",
                )
                self._scene = SceneAnalysisService(
                    api_key=settings.google_gemini_api_key,
                    model="gemini-3.5-flash-lite",
                )
            except Exception as exc:
                logger.warning("[ORCHESTRATOR] Vision QA / Scene Analysis service failed to initialize: %s", exc)
        else:
            logger.info("[ORCHESTRATOR] Vision QA and Scene Analysis unavailable — no GOOGLE_GEMINI_API_KEY")

        if settings.replicate_api_token:
            try:
                self._gen_compositor = GenerativeCompositingService(
                    api_token=settings.replicate_api_token,
                    model=settings.replicate_model,
                )
                logger.info("[ORCHESTRATOR] Generative compositing available (Replicate model=%s)", settings.replicate_model)
            except Exception as exc:
                logger.warning("[ORCHESTRATOR] Generative compositing failed to initialize: %s", exc)
        else:
            logger.info("[ORCHESTRATOR] Generative compositing unavailable — no REPLICATE_API_TOKEN")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, config: PipelineConfig) -> PipelineResult:
        """
        Run the full pipeline with the given configuration.

        Args:
            config: PipelineConfig with city, categories, quantity, mode, and flags.

        Returns:
            PipelineResult with counts, paths, and status.
        """
        # Resolve mode and cache semantics
        mode = config.mode.lower()
        use_cache = config.use_cache

        # In demo mode with cache enabled, default all phases to cache-first
        # unless an explicit force_refresh flag is set
        force_p1 = config.force_refresh_phase1 or (not use_cache and mode == "production")
        force_p2 = config.force_refresh_phase2 or (not use_cache and mode == "production")
        force_fb = config.force_refresh_fallback or (not use_cache and mode == "production")
        force_qa = config.force_refresh_vision_qa or (not use_cache and mode == "production")

        logger.info("=" * 70)
        logger.info("PIPELINE START | mode=%s | city=%s | quantity=%s", mode, config.city, config.quantity)
        logger.info(
            "Resolved force_refresh: phase1=%s | phase2=%s | fallback=%s | vision_qa=%s",
            force_p1, force_p2, force_fb, force_qa,
        )
        logger.info("=" * 70)

        details: dict = {"phases": {}}

        # ------------------------------------------------------------------
        # Phase 1: Venue Discovery
        # ------------------------------------------------------------------
        try:
            candidates = self._discovery.discover(
                query=config.city,
                categories=config.categories,
                quantity=config.quantity,
                force_refresh=force_p1,
            )
            details["phases"]["phase1"] = {
                "status": "success",
                "candidates_found": len(candidates),
            }
            logger.info("[PHASE 1] Discovered %s candidates", len(candidates))
        except Exception as exc:
            logger.error("[PHASE 1] Failed: %s", exc)
            return PipelineResult(
                status="failed",
                city=config.city,
                scan_session_id=None,
                candidates_found=0,
                candidates_with_sv100=0,
                streetview_images_acquired=0,
                fallback_images_acquired=0,
                venues_with_website=0,
                venues_unusable=0,
                details={"phases": {"phase1": {"status": "failed", "error": str(exc)}}},
            )

        # Get the best scan_session_id for this city (most candidates with SV=100)
        with self.db._connection() as conn:
            # Find the session with the most street_view_score=100 candidates
            row = conn.execute(
                """
                SELECT s.id, COUNT(c.id) as sv100_count
                FROM scan_sessions s
                LEFT JOIN candidate_venues c ON s.id = c.scan_session_id AND c.street_view_score = 100 AND c.is_candidate = 1
                WHERE s.query = ? AND s.status = 'completed'
                GROUP BY s.id
                ORDER BY sv100_count DESC, s.created_at DESC
                LIMIT 1
                """,
                (config.city,),
            ).fetchone()
            scan_session_id = row["id"] if row else None

        if not scan_session_id:
            logger.error("[PIPELINE] No scan session found after Phase 1")
            return PipelineResult(
                status="failed",
                city=config.city,
                scan_session_id=None,
                candidates_found=len(candidates),
                candidates_with_sv100=0,
                streetview_images_acquired=0,
                fallback_images_acquired=0,
                venues_with_website=0,
                venues_unusable=0,
                details=details,
            )

        # Count SV=100 candidates
        sv100_count = sum(1 for c in candidates if c.get("street_view_score") == 100)
        logger.info("[PIPELINE] Candidates with street_view_score=100: %s", sv100_count)

        # ------------------------------------------------------------------
        # Phase 2: Street View Image Acquisition
        # ------------------------------------------------------------------
        try:
            sv_results = self._images.acquire(
                scan_session_id=scan_session_id,
                max_venues=config.max_venues_for_images,
                force_refresh=force_p2,
            )
            details["phases"]["phase2"] = {
                "status": "success",
                "images_acquired": len(sv_results),
            }
            logger.info("[PHASE 2] Acquired %s Street View images", len(sv_results))
        except Exception as exc:
            logger.error("[PHASE 2] Failed: %s", exc)
            details["phases"]["phase2"] = {"status": "failed", "error": str(exc)}
            sv_results = []

        # ------------------------------------------------------------------
        # Phase 3: Vision QA on Street View images
        # ------------------------------------------------------------------
        sv_passed = 0
        sv_failed = 0
        sv_failed_venues: list[str] = []

        for r in sv_results:
            img_path = Path(r["primary_image"])
            if self._vision is None:
                # No Vision QA available (missing Gemini key) — assume pass
                sv_passed += 1
                r["qa_passed"] = True
                logger.info("[VISION_QA] SKIPPED (no key) | %s | assumed PASS", r["venue_id"])
                continue
            try:
                v = self._vision.evaluate_image(img_path, force_refresh=force_qa)
                if v.pass_:
                    sv_passed += 1
                    r["qa_passed"] = True
                    logger.info("[VISION_QA] SV PASS | %s | %s", r["venue_id"], v.reason)
                else:
                    sv_failed += 1
                    sv_failed_venues.append(r["venue_id"])
                    r["qa_passed"] = False
                    logger.info("[VISION_QA] SV FAIL | %s | %s", r["venue_id"], v.reason)
            except Exception as exc:
                sv_failed += 1
                sv_failed_venues.append(r["venue_id"])
                r["qa_passed"] = False
                logger.warning("[VISION_QA] SV ERROR | %s | %s", r["venue_id"], exc)

        details["phases"]["vision_qa_sv"] = {
            "status": "success",
            "passed": sv_passed,
            "failed": sv_failed,
        }

        # ------------------------------------------------------------------
        # Phase 3 Fallback: Business Photos + Website for failed / missing venues
        # ------------------------------------------------------------------
        fallback_results: list[dict] = []
        fb_passed = 0
        venues_with_website = 0
        venues_unusable = 0

        # Target: fill up to max_venues_for_images, prioritising SV-failed venues
        needed = config.max_venues_for_images - sv_passed
        fallback_target_ids = sv_failed_venues[:needed]

        if needed > 0:
            # If SV-failed venues don't fill quota, add top candidates without SV images
            sv_place_ids = {r["venue_id"] for r in sv_results}
            with self.db._connection() as conn:
                extra_candidates = conn.execute(
                    """
                    SELECT place_id, name FROM candidate_venues
                    WHERE scan_session_id = ? AND is_candidate = 1
                    AND place_id NOT IN ({placeholders})
                    AND place_id NOT IN ({fb_placeholders})
                    ORDER BY street_view_score DESC, user_ratings_total DESC, name ASC
                    LIMIT ?
                    """.replace(
                        "{placeholders}", ",".join("?" * len(sv_place_ids)) if sv_place_ids else "''"
                    ).replace(
                        "{fb_placeholders}", ",".join("?" * len(fallback_target_ids)) if fallback_target_ids else "''"
                    ),
                    (scan_session_id,
                     *list(sv_place_ids),
                     *list(fallback_target_ids),
                     needed - len(fallback_target_ids)),
                ).fetchall()

            fallback_target_ids.extend([c["place_id"] for c in extra_candidates])

            logger.info("[FALLBACK] Processing %s venues via fallback", len(fallback_target_ids))

            for place_id in fallback_target_ids[:needed]:
                if self._fallback is None:
                    logger.info("[FALLBACK] Skipped %s — no GOOGLE_PLACES_API_KEY", place_id)
                    continue
                try:
                    fb = self._fallback.fetch_images(
                        place_id=place_id,
                        force_refresh=force_fb,
                    )
                    fallback_results.append(fb)
                    if fb.get("websiteUri"):
                        venues_with_website += 1
                    if fb["status"] == "no_candidates":
                        venues_unusable += 1
                        continue

                    # Run Vision QA on fallback candidates (union ranked)
                    union_candidates = fb.get("union_candidates", [])
                    if union_candidates and self._vision is not None:
                        qa_input = [{"path": c["path"], "source": c.get("source", "fallback")} for c in union_candidates]
                        qa_result = self._vision.evaluate_candidates(qa_input, stop_on_first_pass=True, force_refresh=force_qa)
                        if qa_result["best_image"]:
                            fb_passed += 1
                            fb["qa_passed"] = True
                            fb["best_image_path"] = qa_result["best_image"]
                            logger.info("[VISION_QA] FB PASS | %s | best=%s", place_id, qa_result["best_image"])
                        else:
                            fb["qa_passed"] = False
                            logger.info("[VISION_QA] FB FAIL | %s | no candidate passed", place_id)
                    else:
                        fb["qa_passed"] = False
                        logger.info("[VISION_QA] FB SKIP | %s | no candidates or no vision key", place_id)

                except Exception as exc:
                    logger.warning("[FALLBACK] Failed for %s: %s", place_id, exc)

        details["phases"]["fallback"] = {
            "status": "success",
            "venues_processed": len(fallback_results),
            "venues_with_website": venues_with_website,
            "venues_unusable": venues_unusable,
        }
        details["phases"]["vision_qa_fallback"] = {
            "status": "success",
            "passed": fb_passed,
        }

        # ------------------------------------------------------------------
        # Phase 3: Compositing
        # ------------------------------------------------------------------
        composites: list[dict] = []
        total_usable = sv_passed + fb_passed
        if config.planter_image_path and config.planter_image_path.exists():
            logger.info("[PHASE 3] Compositing %s venues with planter=%s", total_usable, config.planter_image_path.name)
            # Collect all usable venues (SV-passed + fallback-passed)
            usable_venues: list[tuple[str, Path]] = []
            for r in sv_results:
                if r.get("qa_passed"):
                    usable_venues.append((r["venue_id"], Path(r["primary_image"])))
            for fb in fallback_results:
                if fb.get("qa_passed") and fb.get("best_image_path"):
                    usable_venues.append((fb["place_id"], Path(fb["best_image_path"])))

            # Decide which compositor to use
            use_gen = config.use_generative_ai and self._gen_compositor is not None
            compositor = self._gen_compositor if use_gen else self._compositor
            mode_label = "GENERATIVE" if use_gen else "CV"
            logger.info("[PHASE 3] Using %s compositor", mode_label)

            gen_count = 0
            cv_fallback_count = 0

            for venue_id, frontage_path in usable_venues:
                comp_results = None
                used_mode = mode_label

                # --- Scene Analysis (before compositing) ---
                scene_analysis: dict | None = None
                if self._scene is not None and frontage_path.exists():
                    try:
                        scene_result = self._scene.analyze(frontage_path)
                        if scene_result:
                            scene_analysis = {
                                "pixels_per_meter": scene_result.pixels_per_meter,
                                "ground_plane_y": scene_result.ground_plane_y,
                                "shadow_angle_deg": scene_result.shadow_angle_deg,
                                "shadow_softness": scene_result.shadow_softness,
                                "placement_candidates": scene_result.placement_candidates,
                                "door_bbox": scene_result.door_bbox,
                                "image_width": scene_result.image_width,
                                "image_height": scene_result.image_height,
                            }
                            logger.info("[PHASE 3] Scene analysis OK %s | ppm=%.1f | ground_y=%d",
                                        venue_id, scene_result.pixels_per_meter, scene_result.ground_plane_y)
                    except Exception as exc:
                        logger.warning("[PHASE 3] Scene analysis failed %s: %s — falling back to heuristics", venue_id, exc)

                # 1. Try generative compositor (if enabled)
                if use_gen:
                    try:
                        comp_results = self._gen_compositor.compose(
                            venue_id=venue_id,
                            frontage_path=frontage_path,
                            planter_path=config.planter_image_path,
                        )
                        gen_count += 1
                        logger.info("[PHASE 3] GENERATIVE OK %s | %s variations", venue_id, len(comp_results))
                    except Exception as exc:
                        logger.warning("[PHASE 3] GENERATIVE FAILED %s: %s — falling back to CV", venue_id, exc)

                # 2. Fallback to CV if generative failed or was not enabled
                if comp_results is None:
                    try:
                        comp_results = self._compositor.compose(
                            venue_id=venue_id,
                            frontage_path=frontage_path,
                            planter_path=config.planter_image_path,
                            scene_analysis=scene_analysis,
                            planter_width_cm=config.planter_width_cm,
                            planter_height_cm=config.planter_height_cm,
                            door_gap_cm=config.door_gap_cm,
                        )
                        used_mode = "CV"
                        if use_gen:
                            cv_fallback_count += 1
                            logger.info("[PHASE 3] CV FALLBACK OK %s | %s variations", venue_id, len(comp_results))
                        else:
                            logger.info("[PHASE 3] CV OK %s | %s variations", venue_id, len(comp_results))
                    except Exception as exc:
                        logger.warning("[PHASE 3] CV ALSO FAILED %s: %s", venue_id, exc)
                        continue

                composites.append({
                    "venue_id": venue_id,
                    "variations": [
                        {"position": c.position, "path": str(c.path)}
                        for c in comp_results
                    ],
                })
                logger.info("[PHASE 3] Composited %s | %s variations | mode=%s", venue_id, len(comp_results), used_mode)

            details["phases"]["compositing"] = {
                "status": "success",
                "venues_composited": len(composites),
                "mode": mode_label,
                "generative_success": gen_count,
                "cv_fallback": cv_fallback_count,
            }
        else:
            logger.info("[PHASE 3] Skipped — no planter_image_path provided")
            details["phases"]["compositing"] = {"status": "skipped", "reason": "no planter image"}

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        total_status = "success"
        if total_usable == 0:
            total_status = "failed"
        elif total_usable < config.max_venues_for_images:
            total_status = "partial"

        logger.info("=" * 70)
        logger.info("PIPELINE COMPLETE | status=%s | usable=%s (SV=%s FB=%s) | composites=%s",
                    total_status, total_usable, sv_passed, fb_passed, len(composites))
        logger.info("=" * 70)

        return PipelineResult(
            status=total_status,
            city=config.city,
            scan_session_id=scan_session_id,
            candidates_found=len(candidates),
            candidates_with_sv100=sv100_count,
            streetview_images_acquired=len(sv_results),
            sv_images_passed_qa=sv_passed,
            sv_images_failed_qa=sv_failed,
            fallback_images_acquired=len(fallback_results),
            fallback_images_passed_qa=fb_passed,
            venues_with_website=venues_with_website,
            venues_unusable=venues_unusable,
            composites=composites,
            details=details,
        )
