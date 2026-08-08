"""FastAPI entry point for the retained live-event operator console."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import queries
from .routers import control as control_router
from .routers import mappings, monitor, vision_calibration


MONITOR_DIST_DIR = Path(__file__).parent / "frontend" / "dist"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        yield
    finally:
        control_router.control_sessions.clear()
        control_router.control_service.close()


app = FastAPI(
    title="Dota 2 Live Prediction Console",
    description="RayBet live events, HUD evidence, locked drafts, and immutable P0/P1 predictions.",
    version="1.0.0",
    lifespan=_lifespan,
)
app.include_router(monitor.router)
app.include_router(control_router.router)
app.include_router(mappings.router)
app.include_router(vision_calibration.router)


@app.get("/", include_in_schema=False)
def serve_index(request: Request) -> RedirectResponse:
    target = "/monitor"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/hero-grid", tags=["monitor"])
def hero_grid() -> dict[str, object]:
    return queries.get_hero_grid()


@app.get("/api/team-grid", tags=["monitor"])
def team_grid() -> list[dict[str, object]]:
    return queries.get_team_grid()


@app.get("/monitor", include_in_schema=False)
@app.get("/monitor/{asset_path:path}", include_in_schema=False)
def serve_monitor(asset_path: str = "") -> Response:
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
        "Monitor frontend is not built. Run npm install and npm run build in web/frontend.",
        status_code=503,
        media_type="text/plain",
    )
