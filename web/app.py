from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.responses import JSONResponse
import psutil

from live_betting.milestone_revocation import (
    MilestoneRevocationConfig,
    MilestoneRevocationIntegrityError,
)
from live_betting.rosh_parity import (
    ExactByteArtifactStore,
    RoshAnalysisError,
    RoshParityOrchestrator,
)
from live_betting.rosh_parity_storage import RoshRunRepository, StoredRoshRun
from live_betting.stratz_rosh_client import StratzRoshClient, StratzRoshError
from prematch.stratz_official_profile import ProfileError, get_profile

from live_betting.process_control import (
    ProcessIdentity,
    TerminationResult,
    terminate_process_tree,
    terminate_subprocess_tree,
)

from . import queries
from .routers import (
    control,
    heroes,
    intelligence,
    leagues,
    mappings,
    matches,
    monitor,
    teams,
)
from .schemas import (
    H2HComparison,
    MatchSummary,
    PrematchRequest,
    PredictionRequest,
    RoshAnalysisRequest,
    RoshAnalysisRunResponse,
    TeamBase,
)

# Resolve paths for the prediction module
_WEB_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _WEB_DIR.parent
_MODELS_DIR = os.environ.get("MODELS_DIR", str(_PROJECT_DIR / "data" / "models"))
_PREDICTIONS_DIR = os.environ.get("PREDICTIONS_DIR", str(_PROJECT_DIR / "data" / "predictions"))
_ROSH_ARTIFACTS_DIR = os.environ.get(
    "ROSH_ANALYSIS_ARTIFACTS_DIR",
    str(_PROJECT_DIR / "data" / "rosh-analysis-artifacts"),
)
logger = logging.getLogger(__name__)

# Ensure project root is on sys.path (once at startup, not lazily)
_project_root_str = str(_PROJECT_DIR)
if _project_root_str not in sys.path:
    sys.path.insert(0, _project_root_str)

_prediction_module = None
_fetch_process: subprocess.Popen[bytes] | None = None
_fetch_process_identity: ProcessIdentity | None = None
_fetch_process_lock = threading.Lock()
_fetch_poll_task: asyncio.Task[None] | None = None
_FETCH_POLL_INTERVAL_SECONDS = 1.0


def _get_prediction_module():
    global _prediction_module
    if _prediction_module is None:
        from predict import feature_builder, output, predictor
        _prediction_module = (feature_builder, output, predictor)
    return _prediction_module


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _fetch_poll_task
    _fetch_poll_task = asyncio.create_task(_fetch_process_poll_loop())
    try:
        yield
    finally:
        try:
            _fetch_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _fetch_poll_task
            _fetch_poll_task = None
        finally:
            try:
                _shutdown_fetch_process()
            finally:
                try:
                    control.control_sessions.clear()
                finally:
                    control.control_service.close()


app = FastAPI(
    title="Dota 2 Predictor API",
    description="REST API for Dota 2 match data, team stats, and predictions.",
    version="0.1.0",
    lifespan=_lifespan,
)
app.state.milestone_revocation_config = None


@app.exception_handler(RequestValidationError)
async def _request_validation_error(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    if request.url.path == "/api/prematch/rosh-analysis":
        _rosh_analysis_event(
            {
                "event": "rosh_analysis_failed",
                "stage": "pre_draft",
                "error_code": "invalid_request",
                "mode": None,
                "profile_id": None,
                "request_hash_prefix": None,
                "run_id_prefix": None,
            }
        )
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "error_code": "invalid_request",
                    "message": "Rosh analysis request is invalid",
                }
            },
        )
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(error.errors())},
    )


def configure_milestone_revocation(
    application: FastAPI,
    config: MilestoneRevocationConfig | None,
) -> None:
    if config is not None and not isinstance(config, MilestoneRevocationConfig):
        raise ValueError("milestone revocation configuration is incomplete")
    application.state.milestone_revocation_config = config


@app.exception_handler(MilestoneRevocationIntegrityError)
async def _milestone_revocation_integrity_error(
    _request: Request, error: MilestoneRevocationIntegrityError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": f"Milestone governance integrity failure: {error}"},
    )

app.include_router(matches.router)
app.include_router(teams.router)
app.include_router(heroes.router)
app.include_router(leagues.router)
app.include_router(monitor.router)
app.include_router(control.router)
app.include_router(mappings.router)
app.include_router(intelligence.router)


def _note_fetch_cleanup_error(
    primary: BaseException,
    cleanup: BaseException,
    *,
    label: str,
) -> None:
    primary.add_note(f"{label}: {type(cleanup).__name__}: {cleanup}")


def _clear_fetch_process() -> None:
    global _fetch_process, _fetch_process_identity
    _fetch_process = None
    _fetch_process_identity = None


def _poll_fetch_process_once() -> None:
    """Reap one completed fetch without weakening an unverifiable handle."""

    with _fetch_process_lock:
        if _fetch_process is None:
            return
        try:
            completed = _fetch_process.poll() is not None
        except (AttributeError, OSError):
            return
        if completed:
            _clear_fetch_process()


async def _fetch_process_poll_loop() -> None:
    while True:
        await asyncio.sleep(_FETCH_POLL_INTERVAL_SECONDS)
        _poll_fetch_process_once()


def _terminate_fetch_handle(
    process_handle: subprocess.Popen[bytes],
    identity: ProcessIdentity | None,
) -> TerminationResult:
    try:
        if process_handle.poll() is not None:
            return TerminationResult(True)
    except (AttributeError, OSError) as error:
        return TerminationResult(
            False,
            f"fetch_process_poll_failed:{type(error).__name__}",
        )
    if identity is None:
        return terminate_subprocess_tree(
            process_handle,
            process_factory=psutil.Process,
        )
    try:
        process = psutil.Process(identity.pid)
    except (psutil.NoSuchProcess, KeyError):
        try:
            return TerminationResult(process_handle.poll() is not None)
        except (AttributeError, OSError):
            return TerminationResult(False, "fetch_process_identity_missing")
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
        return TerminationResult(
            False,
            f"fetch_process_identity_unverifiable:{type(error).__name__}",
        )
    return terminate_process_tree(
        process,
        process_factory=psutil.Process,
        expected_root=identity,
    )


def _shutdown_fetch_process() -> None:
    with _fetch_process_lock:
        if _fetch_process is None:
            return
        result = _terminate_fetch_handle(
            _fetch_process,
            _fetch_process_identity,
        )
        if not result.ok:
            raise RuntimeError(
                f"fetch process shutdown incomplete: {result.detail}"
            )
        _clear_fetch_process()

# ---- Hero grid endpoint (for pre-match hero picker) ----

@app.get("/api/hero-grid", tags=["heroes"])
def hero_grid():
    """Return heroes grouped by primary attribute with image URLs."""
    return queries.get_hero_grid()


# ---- Recent matches (for pre-match page) ----

@app.get("/api/recent-matches", tags=["matches"])
def recent_matches(limit: int = Query(30, ge=1, le=100)):
    """Return the most recent matches with team names, for quick lookup."""
    conn = queries.get_db()
    try:
        rows = conn.execute(
            """SELECT m.match_id, m.radiant_team_id, m.dire_team_id,
                      m.radiant_win, m.start_time, m.leagueid,
                      rt.name AS radiant_name, dt.name AS dire_name,
                      l.name AS league_name
               FROM matches m
               LEFT JOIN teams rt ON m.radiant_team_id = rt.team_id
               LEFT JOIN teams dt ON m.dire_team_id = dt.team_id
               LEFT JOIN leagues l ON m.leagueid = l.leagueid
               ORDER BY m.start_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        conn.close()
        raise


# ---- Trigger data fetch ----

@app.post("/api/fetch-latest", tags=["admin"])
def trigger_fetch(
    request: Request,
    match_id: int | None = Query(default=None, gt=0),
    force: bool = False,
    admin_action: str | None = Header(default=None, alias="X-Dota2-Admin-Action"),
):
    """Trigger fetching latest matches from OpenDota (runs in background).

    If match_id is provided, fetches only that match.
    If force is True, re-fetches even if already in DB.
    """
    client_host = request.client.host if request.client is not None else ""
    if client_host not in {"127.0.0.1", "::1"} or admin_action != "fetch":
        raise HTTPException(status_code=403, detail="Local admin request required")
    global _fetch_process, _fetch_process_identity
    try:
        cmd = [
            sys.executable,
            "-m",
            "fetch.main",
        ]
        if match_id is not None:
            cmd.extend(["--match-id", str(match_id)])
        if force:
            cmd.append("--force")
        with _fetch_process_lock:
            if _fetch_process is not None:
                try:
                    running = _fetch_process.poll() is None
                except (AttributeError, OSError):
                    running = True
                if running:
                    raise HTTPException(
                        status_code=409,
                        detail="A fetch is already running or unverifiable",
                    )
                _clear_fetch_process()
            try:
                _fetch_process = subprocess.Popen(
                    cmd,
                    cwd=str(_PROJECT_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    ),
                    env=os.environ.copy(),
                )
            except BaseException as error:
                try:
                    _clear_fetch_process()
                except BaseException as cleanup_error:
                    _note_fetch_cleanup_error(
                        error,
                        cleanup_error,
                        label="fetch process cleanup failed",
                    )
                raise
            _fetch_process_identity = None
            try:
                process = psutil.Process(int(_fetch_process.pid))
                created_at = float(process.create_time())
                actual_command = list(process.cmdline())
                if _fetch_process.poll() is not None:
                    raise RuntimeError("fetch process exited during registration")
                if actual_command != cmd:
                    raise RuntimeError("fetch process command identity changed")
                _fetch_process_identity = ProcessIdentity(
                    int(_fetch_process.pid),
                    created_at,
                )
            except BaseException as error:
                try:
                    cleanup = terminate_subprocess_tree(
                        _fetch_process,
                        process_factory=psutil.Process,
                    )
                except BaseException as cleanup_error:
                    _note_fetch_cleanup_error(
                        error,
                        cleanup_error,
                        label="fetch process cleanup failed",
                    )
                    raise error
                if cleanup.ok:
                    try:
                        _clear_fetch_process()
                    except BaseException as cleanup_error:
                        _note_fetch_cleanup_error(
                            error,
                            cleanup_error,
                            label="fetch process cleanup failed",
                        )
                        if not isinstance(error, Exception):
                            raise error
                        raise RuntimeError(
                            "fetch process registration failed; cleanup is incomplete"
                        ) from error
                else:
                    error.add_note(
                        "fetch process cleanup failed: "
                        f"{cleanup.detail or 'unknown error'}"
                    )
                if not isinstance(error, Exception):
                    raise error
                raise RuntimeError(
                    "fetch process registration failed: "
                    f"{type(error).__name__}:{error}; cleanup={cleanup.detail}"
                ) from error
        parts = []
        if match_id:
            parts.append(f"match {match_id}")
        else:
            parts.append("latest matches")
        if force:
            parts.append("with --force")
        return {"status": "started", "message": f"Fetching {' '.join(parts)} in background..."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start fetch: {e}")

STATIC_DIR = Path(__file__).parent / "static"
MONITOR_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


@app.get("/", include_in_schema=False)
def serve_index(request: Request) -> RedirectResponse:
    """Open the operator console while preserving legacy query deep links."""
    target = "/monitor"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


@app.get("/matches", include_in_schema=False)
def serve_legacy_matches() -> RedirectResponse:
    """Retire the legacy table in favor of the console history view."""
    return RedirectResponse("/monitor?view=replay", status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/monitor", include_in_schema=False)
@app.get("/monitor/{asset_path:path}", include_in_schema=False)
def serve_monitor(asset_path: str = ""):
    """Serve the built local monitoring console and its hashed assets."""
    root = MONITOR_DIST_DIR.resolve()
    if asset_path:
        candidate = (root / asset_path).resolve()
        if root in candidate.parents and candidate.is_file():
            return FileResponse(
                candidate,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
    index_path = root / "index.html"
    if index_path.is_file():
        return FileResponse(
            index_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return Response(
        "Monitor frontend is not built. Run: cd web/frontend && npm install && npm run build",
        status_code=503,
        media_type="text/plain",
    )


@app.get("/api/stats/head-to-head", response_model=H2HComparison, tags=["stats"])
def head_to_head(
    team_a: int = Query(...),
    team_b: int = Query(...),
):
    data = queries.get_head_to_head(team_a, team_b)
    return H2HComparison(
        team_a=TeamBase(**data["team_a"]),
        team_b=TeamBase(**data["team_b"]),
        total_matches=data["total_matches"],
        team_a_wins=data["team_a_wins"],
        team_b_wins=data["team_b_wins"],
        team_a_win_rate=data["team_a_win_rate"],
        avg_duration=data["avg_duration"],
        recent_encounters=[MatchSummary(**m) for m in data.get("recent_encounters", [])],
    )


@app.get("/api/predictions", tags=["predictions"])
def list_predictions():
    pred_dir = Path(_PREDICTIONS_DIR)
    if not pred_dir.exists():
        return {"data": [], "count": 0}
    files = sorted(pred_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    results = []
    for f in files[:50]:
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return {"data": results, "count": len(results)}


@app.post("/api/predict", tags=["predictions"])
def create_prediction(request: PredictionRequest):
    if request.team_a == request.team_b:
        raise HTTPException(status_code=400, detail="Teams must be different")

    # Validate teams exist
    conn = queries.get_db()
    try:
        for tid in (request.team_a, request.team_b):
            row = conn.execute("SELECT 1 FROM teams WHERE team_id = ?", (tid,)).fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Team {tid} not found in database. Fetch match data first.",
                )
    finally:
        conn.close()

    # Validate pre-match model exists
    if not Path(_MODELS_DIR, "prematch_latest.pkl").exists():
        raise HTTPException(
            status_code=503,
            detail="Pre-match model not found. Train it first: python -m prematch.train",
        )

    try:
        feature_builder, output, predictor = _get_prediction_module()

        bundle = predictor.load_model(_MODELS_DIR)
        features = feature_builder.build_features(
            request.team_a, request.team_b,
            request.league_id or 0, queries.DATABASE_URL, bundle["feature_names"],
        )
        result = predictor.predict(bundle, features)
        prediction_output = output.format_output(
            result, request.team_a, request.team_b,
            request.league_id or 0, bundle, queries.DATABASE_URL,
        )
        file_path = output.save_prediction(prediction_output, _PREDICTIONS_DIR)
        prediction_output["file_path"] = file_path
        return output._sanitize(prediction_output)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.get("/prematch", include_in_schema=False)
def serve_prematch_page():
    """Serve the integrated console with the prematch view selected."""
    return serve_monitor()


_prematch_builder = None


def _get_prematch_builder():
    global _prematch_builder
    if _prematch_builder is None:
        from predict import output
        _prematch_builder = (StratzRoshClient(), output)
    return _prematch_builder


def _rosh_analysis_event(event: dict) -> None:
    logger.info("official_rosh_analysis %s", json.dumps(event, sort_keys=True))


def _get_rosh_analysis_orchestrator(connection) -> RoshParityOrchestrator:
    return RoshParityOrchestrator(
        transport=StratzRoshClient(),
        artifacts=ExactByteArtifactStore(_ROSH_ARTIFACTS_DIR),
        repository=RoshRunRepository(connection),
        event_hook=_rosh_analysis_event,
    )


def _rosh_run_response(stored: StoredRoshRun) -> dict:
    run = stored.run
    return {
        "schema": "rosh-analysis-run/v1",
        "run_id": run.run_id,
        "status": run.status,
        "mode": run.mode,
        "match_id": run.match_id,
        "date_time": run.date_time,
        "draft_hash": run.draft_hash,
        "rosh_profile_id": run.rosh_profile_id,
        "formula_version": run.formula_version,
        "request_profile_hash": run.request_profile_hash,
        "upstream_bundle_hash": run.upstream_bundle_hash,
        "scorer_source_hash": run.scorer_source_hash,
        "canonical_profile_hash": run.canonical_profile_hash,
        "serialization_version": run.serialization_version,
        "evidence_hash": run.evidence_hash,
        "collected_at": run.collected_at,
        "radiant_team_score": run.radiant_team_score,
        "dire_team_score": run.dire_team_score,
        "relative_advantage": run.relative_advantage,
        "hero_components": [
            {
                "team_side": row.team_side,
                "position_id": row.position_id,
                "hero_id": row.hero_id,
                **dict(row.components),
                "raw_score": row.raw_score,
                "display_score": row.display_score,
            }
            for row in stored.hero_scores
        ],
        "minute_points": [
            {
                "minute": row.minute,
                "radiant_time_delta": row.radiant_time_delta,
                "dire_time_delta": row.dire_time_delta,
                "synergy_delta": row.synergy_delta,
                "raw_score": row.raw_score,
                "display_score": row.display_score,
                **dict(row.source_audit),
            }
            for row in stored.minute_points
        ],
        "error_code": run.error_code,
    }


_ROSH_ERROR_STATUS = {
    "invalid_request": 400,
    "source_match_not_found": 404,
    "source_draft_mismatch": 409,
    "profile_drift": 409,
    "source_data_incomplete": 422,
    "upstream_rate_limited": 429,
    "upstream_unavailable": 503,
}


def _raise_rosh_http(error: RoshAnalysisError) -> None:
    detail = {
        "error_code": error.error_code,
        "message": str(error),
    }
    if error.run_id is not None:
        detail["run_id"] = error.run_id
    raise HTTPException(
        status_code=_ROSH_ERROR_STATUS[error.error_code],
        detail=detail,
    ) from None


@app.post(
    "/api/prematch/rosh-analysis",
    response_model=RoshAnalysisRunResponse,
    tags=["predictions"],
)
def create_rosh_analysis(request: RoshAnalysisRequest):
    connection = queries.get_db()
    try:
        try:
            profile = get_profile(request.rosh_profile_id)
        except ProfileError:
            _rosh_analysis_event(
                {
                    "event": "rosh_analysis_failed",
                    "stage": "pre_draft",
                    "error_code": "profile_drift",
                    "mode": request.mode,
                    "profile_id": request.rosh_profile_id,
                    "request_hash_prefix": None,
                    "run_id_prefix": None,
                }
            )
            _raise_rosh_http(RoshAnalysisError("profile_drift"))
        analysis_input = request.model_dump(exclude={"rosh_profile_id"})
        stored = _get_rosh_analysis_orchestrator(connection).execute(
            analysis_input,
            profile,
        )
        return _rosh_run_response(stored)
    except RoshAnalysisError as error:
        _raise_rosh_http(error)
    finally:
        connection.close()


@app.get(
    "/api/prematch/rosh-analysis/{run_id}",
    response_model=RoshAnalysisRunResponse,
    tags=["predictions"],
)
def get_rosh_analysis(run_id: str):
    if len(run_id) != 64 or any(character not in "0123456789abcdef" for character in run_id):
        raise HTTPException(
            status_code=404,
            detail={"error_code": "analysis_not_found", "message": "Rosh analysis was not found"},
        )
    connection = queries.get_db()
    try:
        stored = RoshRunRepository(connection).get(run_id)
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "analysis_not_found",
                    "message": "Rosh analysis was not found",
                },
            )
        return _rosh_run_response(stored)
    finally:
        connection.close()


def _validated_source_match(request: PrematchRequest) -> dict:
    assert request.source_match_id is not None
    draft = queries.get_match_draft(request.source_match_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="Source match is not available locally")
    if (
        draft.get("radiant_team_id") != request.radiant_id
        or draft.get("dire_team_id") != request.dire_id
    ):
        raise HTTPException(status_code=400, detail="Source match teams do not match the request")

    radiant = draft.get("radiant_heroes") or []
    dire = draft.get("dire_heroes") or []
    if (
        {row.get("hero_id") for row in radiant} != set(request.radiant_heroes)
        or {row.get("hero_id") for row in dire} != set(request.dire_heroes)
    ):
        raise HTTPException(status_code=400, detail="Source match draft does not match the request")

    if request.radiant_players is not None and request.dire_players is not None:
        submitted = {
            hero_id: account_id
            for hero_id, account_id in zip(
                request.radiant_heroes + request.dire_heroes,
                request.radiant_players + request.dire_players,
            )
        }
        observed = {
            row.get("hero_id"): row.get("account_id")
            for row in (*radiant, *dire)
        }
        if observed != submitted:
            raise HTTPException(
                status_code=400,
                detail="Source match player identities do not match the request",
            )
    return draft


def _validate_stratz_source_lineup(context: dict, request: PrematchRequest) -> None:
    radiant = {pick.get("heroId") for pick in context.get("radiant_picks", [])}
    dire = {pick.get("heroId") for pick in context.get("dire_picks", [])}
    if radiant != set(request.radiant_heroes) or dire != set(request.dire_heroes):
        raise HTTPException(
            status_code=502,
            detail="STRATZ source match lineup differs from the local draft",
        )


def _rosh_prediction(score, minute_table: list[dict], source_match_id: int | None) -> dict:
    pure_score = float(score.pure_lineup_score)
    adjusted_score = getattr(score, "player_adjusted_lineup_score", None)
    if adjusted_score is None:
        adjusted_score = getattr(score, "current_player_adjusted_lineup_score", None)
    adjusted_score = None if adjusted_score is None else float(adjusted_score)
    effective_score = float(score.effective_lineup_score)
    player_coverage = int(score.player_coverage_count)
    adjusted_mode = adjusted_score is not None and player_coverage == 10
    confidence_score = 1.0 if adjusted_mode else 0.5
    probability = min(1.0 - 1e-6, max(1e-6, (50.0 + effective_score) / 100.0))

    factors = []
    if effective_score != 0.0:
        factors.append({
            "factor": "stratz_rosh_lineup",
            "impact": round(abs(effective_score) / 100.0, 4),
            "direction": "radiant" if effective_score > 0 else "dire",
        })
    if adjusted_score is not None and adjusted_score != pure_score:
        delta = adjusted_score - pure_score
        factors.append({
            "factor": "stratz_player_highlights",
            "impact": round(abs(delta) / 100.0, 4),
            "direction": "radiant" if delta > 0 else "dire",
        })

    evidence = dict(score.evidence)
    return {
        "radiant_win_prob": round(probability, 4),
        "confidence": "high" if adjusted_mode else "medium",
        "confidence_score": confidence_score,
        "scoring_mode": str(score.scoring_mode),
        "player_coverage_count": player_coverage,
        "source_match_id": source_match_id,
        "top_factors": factors,
        "hero_matrix": [],
        "radiant_heroes": [],
        "dire_heroes": [],
        "components": {
            "stratz_rosh": {
                "pure_lineup_score": pure_score,
                "player_adjusted_lineup_score": adjusted_score,
                "effective_lineup_score": effective_score,
                "scoring_mode": str(score.scoring_mode),
                "player_coverage_count": player_coverage,
                "stake_multiplier": float(getattr(score, "stake_multiplier", confidence_score)),
                "formula_version": str(score.formula_version),
                "source_name": str(score.source_name),
                "source_week": int(score.source_week),
                "source_as_of": score.source_as_of.isoformat(),
                "evidence_hash": str(score.evidence_hash),
                "minute_table": minute_table,
                "evidence": evidence,
            }
        },
        "weights_used": {"stratz_rosh": 1.0},
        "raw_score": effective_score,
    }


@app.post("/api/prematch-predict", tags=["predictions"])
def create_prematch_prediction(request: PrematchRequest):
    if request.radiant_id == request.dire_id:
        raise HTTPException(status_code=400, detail="Teams must be different")
    if len(request.radiant_heroes) != 5 or len(request.dire_heroes) != 5:
        raise HTTPException(
            status_code=400,
            detail="Exactly 5 heroes required per side.",
        )

    # Validate teams exist
    conn = queries.get_db()
    try:
        for tid in (request.radiant_id, request.dire_id):
            row = conn.execute("SELECT 1 FROM teams WHERE team_id = ?", (tid,)).fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Team {tid} not found in database.",
                )
    finally:
        conn.close()

    try:
        client, output = _get_prematch_builder()
        if request.source_match_id is not None:
            _validated_source_match(request)
            fetched = client.fetch_historical_match_score(
                request.source_match_id,
                include_current_player_adjustment=request.radiant_players is not None,
            )
            _validate_stratz_source_lineup(fetched.context, request)
            if fetched.score is None:
                raise HTTPException(
                    status_code=503,
                    detail="STRATZ Rosh score is unavailable for the source match",
                )
            score = fetched.score
            minute_table = [dict(row) for row in fetched.minute_table]
        else:
            score = client.fetch_lineup_score(
                request.radiant_heroes,
                request.dire_heroes,
                as_of=datetime.now(timezone.utc),
            )
            table_key = (
                "minute_table"
                if score.player_adjusted_lineup_score is not None
                else "pure_minute_table"
            )
            minute_table = [dict(row) for row in score.evidence.get(table_key, [])]

        prediction = _rosh_prediction(score, minute_table, request.source_match_id)

        prediction_output = output.format_output(
            prediction, request.radiant_id, request.dire_id,
            request.league_id or 0,
            {"timestamp": score.formula_version, "metrics": {}},
            queries.DATABASE_URL,
        )
        file_path = output.save_prediction(prediction_output, _PREDICTIONS_DIR)
        prediction_output["file_path"] = file_path
        return output._sanitize(prediction_output)
    except HTTPException:
        raise
    except StratzRoshError as e:
        raise HTTPException(
            status_code=503,
            detail=f"STRATZ Rosh is unavailable: {e}",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")
