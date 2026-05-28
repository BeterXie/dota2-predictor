from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import queries
from .routers import heroes, leagues, matches, teams
from .schemas import H2HComparison, MatchSummary, PrematchRequest, PredictionRequest, TeamBase

# Resolve paths for the prediction module
_WEB_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _WEB_DIR.parent
_DB_PATH = os.environ.get("DATABASE_PATH", str(_PROJECT_DIR / "data" / "dota2.db"))
_MODELS_DIR = os.environ.get("MODELS_DIR", str(_PROJECT_DIR / "data" / "models"))
_PREDICTIONS_DIR = os.environ.get("PREDICTIONS_DIR", str(_PROJECT_DIR / "data" / "predictions"))

_prediction_module = None


def _get_prediction_module():
    global _prediction_module
    if _prediction_module is None:
        sys.path.insert(0, str(_PROJECT_DIR))
        from predict import feature_builder, output, predictor
        _prediction_module = (feature_builder, output, predictor)
    return _prediction_module


app = FastAPI(
    title="Dota 2 Predictor API",
    description="REST API for Dota 2 match data, team stats, and predictions.",
    version="0.1.0",
)

app.include_router(matches.router)
app.include_router(teams.router)
app.include_router(heroes.router)
app.include_router(leagues.router)

# ---- Hero grid endpoint (for pre-match hero picker) ----

@app.get("/api/hero-grid", tags=["heroes"])
def hero_grid():
    """Return heroes grouped by primary attribute with image URLs."""
    return queries.get_hero_grid()

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dota 2 Predictor</h1><p>index.html not found.</p>")


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

    # Validate model exists
    if not Path(_MODELS_DIR, "latest.pkl").exists():
        raise HTTPException(
            status_code=503,
            detail="Model not found. Train the model first (python -m train.main).",
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
    index_path = STATIC_DIR / "prematch.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Pre-Match Prediction</h1><p>prematch.html not found.</p>")


_prematch_builder = None


def _get_prematch_builder():
    global _prematch_builder
    if _prematch_builder is None:
        sys.path.insert(0, str(_PROJECT_DIR))
        from prematch.feature_builder import build_prematch_features
        from predict import output, predictor
        _prematch_builder = (build_prematch_features, output, predictor)
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

    # Validate pre-match model exists
    if not Path(_MODELS_DIR, "prematch_latest.pkl").exists():
        raise HTTPException(
            status_code=503,
            detail="Pre-match model not found. Train it first: python -m prematch.train",
        )

    try:
        build_prematch_features, output, predictor = _get_prematch_builder()

        # Load model bundle
        import pickle
        with open(Path(_MODELS_DIR) / "prematch_latest.pkl", "rb") as f:
            bundle = pickle.load(f)

        features = build_prematch_features(
            request.radiant_id, request.dire_id,
            request.league_id or 0,
            request.radiant_heroes, request.dire_heroes,
            _DB_PATH, bundle["feature_names"],
        )
        result = predictor.predict(bundle, features)
        prediction_output = output.format_output(
            result, request.radiant_id, request.dire_id,
            request.league_id or 0, bundle, _DB_PATH,
        )
        file_path = output.save_prediction(prediction_output, _PREDICTIONS_DIR)
        prediction_output["file_path"] = file_path
        return output._sanitize(prediction_output)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")
