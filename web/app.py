from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import alerts, monitoring, queries
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
from .schemas import H2HComparison, MatchSummary, PrematchRequest, PredictionRequest, TeamBase

# Resolve paths for the prediction module
_WEB_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _WEB_DIR.parent
_DB_PATH = os.environ.get("DATABASE_PATH", str(_PROJECT_DIR / "data" / "dota2.db"))
_MODELS_DIR = os.environ.get("MODELS_DIR", str(_PROJECT_DIR / "data" / "models"))
_PREDICTIONS_DIR = os.environ.get("PREDICTIONS_DIR", str(_PROJECT_DIR / "data" / "predictions"))

# Ensure project root is on sys.path (once at startup, not lazily)
_project_root_str = str(_PROJECT_DIR)
if _project_root_str not in sys.path:
    sys.path.insert(0, _project_root_str)

_prediction_module = None
_fetch_process: subprocess.Popen[bytes] | None = None
_fetch_process_lock = threading.Lock()
_alert_task: asyncio.Task[None] | None = None


def _get_prediction_module():
    global _prediction_module
    if _prediction_module is None:
        from predict import feature_builder, output, predictor
        _prediction_module = (feature_builder, output, predictor)
    return _prediction_module


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _alert_task
    connection = queries.get_db()
    try:
        alerts.init_alert_schema(connection)
    finally:
        connection.close()
    _alert_task = asyncio.create_task(_alert_reconciliation_loop())
    try:
        yield
    finally:
        _alert_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _alert_task
        _alert_task = None


app = FastAPI(
    title="Dota 2 Predictor API",
    description="REST API for Dota 2 match data, team stats, and predictions.",
    version="0.1.0",
    lifespan=_lifespan,
)

app.include_router(matches.router)
app.include_router(teams.router)
app.include_router(heroes.router)
app.include_router(leagues.router)
app.include_router(monitor.router)
app.include_router(control.router)
app.include_router(mappings.router)
app.include_router(intelligence.router)


async def _alert_reconciliation_loop() -> None:
    await asyncio.sleep(1)
    while True:
        try:
            connection = queries.get_db()
            try:
                health = monitoring.derive_health(connection)
                alerts.reconcile_alerts(connection, health=health)
            finally:
                connection.close()
        except Exception:
            logging.getLogger("web.alerts").exception("alert reconciliation failed")
        await asyncio.sleep(5)

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
    global _fetch_process
    try:
        cmd = [sys.executable, "-m", "fetch.main"]
        if match_id is not None:
            cmd.extend(["--match-id", str(match_id)])
        if force:
            cmd.append("--force")
        with _fetch_process_lock:
            if _fetch_process is not None and _fetch_process.poll() is None:
                raise HTTPException(status_code=409, detail="A fetch is already running")
            _fetch_process = subprocess.Popen(
                cmd,
                cwd=str(_PROJECT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
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


@app.get("/matches", response_class=HTMLResponse)
def serve_legacy_matches():
    """Serve the legacy searchable match table at an explicit secondary route."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dota 2 Predictor</h1><p>index.html not found.</p>")


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
            request.league_id or 0, _DB_PATH, bundle["feature_names"],
        )
        result = predictor.predict(bundle, features)
        prediction_output = output.format_output(
            result, request.team_a, request.team_b,
            request.league_id or 0, bundle, _DB_PATH,
        )
        file_path = output.save_prediction(prediction_output, _PREDICTIONS_DIR)
        prediction_output["file_path"] = file_path
        return output._sanitize(prediction_output)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.get("/prematch", response_class=HTMLResponse)
def serve_prematch_page():
    from fastapi.responses import Response
    index_path = STATIC_DIR / "prematch.html"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        return Response(
            content=content,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return HTMLResponse("<h1>Pre-Match Prediction</h1><p>prematch.html not found.</p>")


_prematch_builder = None


def _get_prematch_builder():
    global _prematch_builder
    if _prematch_builder is None:
        from prematch.scorer import predict_match
        from predict import output
        _prematch_builder = (predict_match, output)
    return _prematch_builder


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
        predict_match, output = _get_prematch_builder()

        result = predict_match(
            _DB_PATH,
            request.radiant_id, request.dire_id,
            request.radiant_heroes, request.dire_heroes,
            radiant_players=request.radiant_players,
            dire_players=request.dire_players,
        )

        # Wrap result to match expected format
        prediction = {
            "radiant_win_prob": result["radiant_win_prob"],
            "confidence": result["confidence"],
            "confidence_score": result["confidence_score"],
            "top_factors": _format_scorer_factors(result),
            "hero_matrix": result["components"]["hero_matchup"].get("matrix", []),
            "radiant_heroes": result["components"]["hero_matchup"].get("radiant_heroes", []),
            "dire_heroes": result["components"]["hero_matchup"].get("dire_heroes", []),
            # Full component breakdowns for team comparison view
            "components": result["components"],
            "weights_used": result["weights_used"],
            "raw_score": result["raw_score"],
        }

        prediction_output = output.format_output(
            prediction, request.radiant_id, request.dire_id,
            request.league_id or 0,
            {"timestamp": "scorer", "metrics": {}}, _DB_PATH,
        )
        file_path = output.save_prediction(prediction_output, _PREDICTIONS_DIR)
        prediction_output["file_path"] = file_path
        return output._sanitize(prediction_output)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


def _format_scorer_factors(result: dict) -> list[dict]:
    factors = []
    for name, comp in result.get("components", {}).items():
        score = comp.get("score", 0)
        if abs(score) < 0.01:
            continue
        direction = "radiant" if score > 0 else "dire"
        factors.append({
            "factor": name,
            "impact": round(abs(score), 4),
            "direction": direction,
        })
    factors.sort(key=lambda x: x["impact"], reverse=True)
    return factors
