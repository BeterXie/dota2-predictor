from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Header, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from live_betting.vision_frame_registry import (
    read_registered_vision_frame_bytes,
    vision_frame_ref,
)
from live_betting.live_match_state import (
    DraftSlotInput,
    append_live_game_snapshot,
    save_live_draft_mapping,
)
from live_betting.raybet import DOTA2_GAME_ID, SITE_URL, RayBetClient
from live_betting.sanitize import verified_ephemeral_stream_url
from event_intelligence.live_draft_prospective_bridge import (
    LOCK_CONFIRMATION,
    LiveDraftProspectiveBridgeRepository,
    generate_live_draft_prediction,
)

from .. import monitoring, queries
from .control import _COOKIE_NAME
from .mappings import _require_control


router = APIRouter(prefix="/api/monitor", tags=["monitor"])
logger = logging.getLogger(__name__)
_SNAPSHOT_CACHE_TTL_SECONDS = 5.0
_SNAPSHOT_CACHE_LOCK = threading.Lock()
_snapshot_cache: tuple[float, dict[str, object]] | None = None

_WATCHABLE_PROVIDER_STATUSES = {
    "1",
    "2",
    "active",
    "open",
    "running",
    "scheduled",
    "upcoming",
}


class DraftSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: int = Field(gt=0)
    side: str
    position: int = Field(ge=1, le=5)
    hero_id: int = Field(gt=0)
    player_id: int | None = Field(default=None, gt=0)


class SaveDraftMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: list[DraftSlotRequest] = Field(min_length=10, max_length=10)
    is_locked: bool = False
    actor: str = Field(default="local-operator", min_length=1, max_length=100)


class CorrectGameSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_time_seconds: int = Field(ge=0)
    radiant_networth: int = Field(ge=0)
    dire_networth: int = Field(ge=0)
    radiant_kills: int | None = Field(default=None, ge=0)
    dire_kills: int | None = Field(default=None, ge=0)
    actor: str = Field(default="local-operator", min_length=1, max_length=100)


class LiveDraftPredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping_version: int = Field(gt=0)
    operator_identity: str = Field(default="local-operator", min_length=1, max_length=100)
    confirmation_text: str
    game_clock_seconds: int | None = Field(default=None, ge=0)
    vision_frame_timestamp: datetime | None = None
    draft_state_marker: str | None = Field(default=None, max_length=64)
    live_state_input_used: bool = False


def _build_snapshot() -> dict[str, object]:
    """Build one monitor snapshot off the async event loop."""
    connection = queries.get_db()
    try:
        with connection.transaction():
            return monitoring.build_monitor_snapshot(connection)
    finally:
        connection.close()


def _cached_snapshot() -> dict[str, object]:
    """Share one bounded database snapshot across simultaneous SSE clients."""

    global _snapshot_cache
    now = time.monotonic()
    with _SNAPSHOT_CACHE_LOCK:
        if (
            _snapshot_cache is not None
            and now - _snapshot_cache[0] < _SNAPSHOT_CACHE_TTL_SECONDS
        ):
            return _snapshot_cache[1]
        snapshot = _build_snapshot()
        _snapshot_cache = (time.monotonic(), snapshot)
        return snapshot


@router.get("/bootstrap")
def bootstrap() -> dict[str, object]:
    return _cached_snapshot()


@router.get("/health")
def health() -> dict[str, object]:
    connection = queries.get_db()
    try:
        return {"data": monitoring.derive_health(connection)}
    finally:
        connection.close()


@router.get("/matches")
def matches() -> dict[str, object]:
    connection = queries.get_db()
    try:
        data = monitoring.monitor_matches(connection)
        return {"data": data, "count": len(data)}
    finally:
        connection.close()


@router.get("/matches/{raybet_match_id}/live-stream", include_in_schema=False)
def live_stream(raybet_match_id: str) -> RedirectResponse:
    connection = queries.get_db()
    try:
        row = connection.execute(
            "SELECT status FROM raybet_matches WHERE raybet_match_id=?",
            (raybet_match_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="RayBet match not found")
    if (
        str(row["status"] or "").strip().casefold()
        not in _WATCHABLE_PROVIDER_STATUSES
    ):
        raise HTTPException(status_code=409, detail="RayBet match is not watchable")

    target = SITE_URL
    try:
        target = _fresh_live_stream_url(raybet_match_id)
    except Exception as error:
        logger.warning(
            "RayBet live stream fallback for match %s (%s)",
            raybet_match_id,
            type(error).__name__,
        )
    return RedirectResponse(
        target,
        status_code=307,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def _fresh_live_stream_url(raybet_match_id: str) -> str:
    with RayBetClient() as client:
        response = client.match_odds_response(raybet_match_id)
    result = response.payload.get("result")
    if (
        not isinstance(result, dict)
        or str(result.get("id") or "") != raybet_match_id
        or type(result.get("game_id")) is not int
        or int(result["game_id"]) != DOTA2_GAME_ID
    ):
        raise ValueError("RayBet stream identity mismatch")
    stream_url = verified_ephemeral_stream_url(result.get("live_url"))
    if stream_url is None:
        raise ValueError("RayBet live stream unavailable")
    return stream_url


@router.get("/history")
def history(
    cursor: str | None = Query(None, max_length=768),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, object]:
    connection = queries.get_db()
    try:
        try:
            return monitoring.monitor_history_page(
                connection,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
    finally:
        connection.close()


@router.get("/matches/{raybet_match_id}")
def match_detail(
    raybet_match_id: str,
    max_points: int = Query(1200, ge=100, le=5000),
) -> dict[str, object]:
    connection = queries.get_db()
    try:
        try:
            detail = monitoring.monitor_match_detail(
                connection,
                raybet_match_id,
                max_points=max_points,
            )
        except SQLAlchemyError as error:
            if _sqlstate(error) in {"42P01", "42703"}:
                raise HTTPException(
                    status_code=503,
                    detail="Monitor database schema is unavailable",
                ) from None
            if isinstance(error, DBAPIError) and error.connection_invalidated:
                raise HTTPException(
                    status_code=503,
                    detail="Monitor database is unavailable",
                ) from None
            raise
    finally:
        connection.close()
    if detail is None:
        raise HTTPException(status_code=404, detail="RayBet match not found")
    return detail


@router.post("/matches/{raybet_match_id}/maps/{map_number}/draft-mapping")
def save_draft_mapping(
    raybet_match_id: str,
    payload: SaveDraftMappingRequest,
    request: Request,
    map_number: int = Path(ge=1, le=5),
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    connection = queries.get_db()
    try:
        if connection.execute(
            "SELECT 1 FROM raybet_matches WHERE raybet_match_id=?",
            (raybet_match_id,),
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="RayBet match not found")
        try:
            return save_live_draft_mapping(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                slots=(
                    DraftSlotInput(
                        team_id=slot.team_id,
                        side=slot.side,
                        position=slot.position,
                        hero_id=slot.hero_id,
                        player_id=slot.player_id,
                    )
                    for slot in payload.slots
                ),
                is_locked=payload.is_locked,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        connection.close()


@router.get("/matches/{raybet_match_id}/maps/{map_number}/draft-prediction")
def get_live_draft_prediction(
    raybet_match_id: str,
    map_number: int = Path(ge=1, le=5),
    mapping_version: int = Query(gt=0),
) -> dict[str, object]:
    connection = queries.get_db()
    try:
        prediction = LiveDraftProspectiveBridgeRepository(connection).load_prediction(
            raybet_match_id,
            map_number,
            mapping_version,
        )
        return {
            "status": "available" if prediction is not None else "not_found",
            "prediction": prediction,
        }
    finally:
        connection.close()


@router.post("/matches/{raybet_match_id}/maps/{map_number}/draft-prediction")
def create_live_draft_prediction(
    raybet_match_id: str,
    payload: LiveDraftPredictionRequest,
    request: Request,
    map_number: int = Path(ge=1, le=5),
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    if payload.confirmation_text != LOCK_CONFIRMATION:
        raise HTTPException(status_code=422, detail="Frozen lock confirmation is required")
    connection = queries.get_db()
    try:
        try:
            return generate_live_draft_prediction(
                LiveDraftProspectiveBridgeRepository(connection),
                artifact_root=os.environ.get(
                    "LIVE_DRAFT_PROSPECTIVE_ARTIFACT_ROOT",
                    "dogfood-output/live-draft-prospective-artifacts",
                ),
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                mapping_version=payload.mapping_version,
                operator_identity=payload.operator_identity,
                confirmation_text=payload.confirmation_text,
                confirmed_at=datetime.now(timezone.utc),
                game_clock_seconds=payload.game_clock_seconds,
                vision_frame_timestamp=payload.vision_frame_timestamp,
                draft_state_marker=payload.draft_state_marker,
                live_state_input_used=payload.live_state_input_used,
            )
        except ValueError as error:
            status = 409 if "immutable" in str(error) else 422
            raise HTTPException(status_code=status, detail=str(error)) from error
    finally:
        connection.close()


@router.post("/matches/{raybet_match_id}/maps/{map_number}/game-snapshots")
def correct_game_snapshot(
    raybet_match_id: str,
    payload: CorrectGameSnapshotRequest,
    request: Request,
    map_number: int = Path(ge=1, le=5),
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    connection = queries.get_db()
    try:
        if connection.execute(
            "SELECT 1 FROM raybet_matches WHERE raybet_match_id=?",
            (raybet_match_id,),
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="RayBet match not found")
        try:
            return append_live_game_snapshot(
                connection,
                raybet_match_id=raybet_match_id,
                map_number=map_number,
                game_time_seconds=payload.game_time_seconds,
                radiant_networth=payload.radiant_networth,
                dire_networth=payload.dire_networth,
                radiant_kills=payload.radiant_kills,
                dire_kills=payload.dire_kills,
                vision_confidence=1.0,
                screenshot_path=None,
                source="manual_correction",
                captured_at=datetime.now(timezone.utc),
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        connection.close()


@router.get("/matches/{raybet_match_id}/vision-frames/{frame_digest}.jpg")
def vision_frame(
    raybet_match_id: str, frame_digest: str
) -> Response:
    try:
        frame_ref = vision_frame_ref(frame_digest)
    except ValueError:
        raise HTTPException(status_code=404, detail="Vision frame not found") from None

    connection = queries.get_db()
    try:
        try:
            observation = monitoring.valid_vision_frame_observation(
                connection,
                raybet_match_id,
                frame_ref,
            )
        except SQLAlchemyError:
            raise HTTPException(
                status_code=409,
                detail="Vision frame authority is unavailable",
            ) from None
        if observation is None or observation.get("frame_digest") != frame_digest:
            raise HTTPException(status_code=404, detail="Vision frame not found")
        try:
            encoded = read_registered_vision_frame_bytes(
                connection,
                frame_ref,
                expected_sha256=frame_digest,
            )
        except (RuntimeError, TypeError, ValueError, SQLAlchemyError):
            raise HTTPException(
                status_code=409,
                detail="Vision frame integrity check failed",
            ) from None
    finally:
        connection.close()

    return Response(
        content=encoded,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{frame_digest}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/matches/{raybet_match_id}/captures/{frame_digest}.jpg")
def capture_frame(
    raybet_match_id: str,
    frame_digest: str,
) -> Response:
    try:
        frame_ref = vision_frame_ref(frame_digest)
    except ValueError:
        raise HTTPException(status_code=404, detail="Capture frame not found") from None

    connection = queries.get_db()
    try:
        try:
            observation = monitoring.valid_capture_frame_observation(
                connection,
                raybet_match_id,
                frame_ref,
            )
        except SQLAlchemyError:
            raise HTTPException(
                status_code=409,
                detail="Capture frame authority is unavailable",
            ) from None
        if observation is None or observation.get("frame_digest") != frame_digest:
            raise HTTPException(status_code=404, detail="Capture frame not found")
        try:
            encoded = read_registered_vision_frame_bytes(
                connection,
                frame_ref,
                expected_sha256=frame_digest,
            )
        except (RuntimeError, TypeError, ValueError, SQLAlchemyError):
            raise HTTPException(
                status_code=409,
                detail="Capture frame integrity check failed",
            ) from None
    finally:
        connection.close()

    return Response(
        content=encoded,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{frame_digest}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/events")
async def events(
    request: Request,
    cursor: str | None = Query(None),
) -> StreamingResponse:
    previous = request.headers.get("last-event-id") or cursor

    async def stream():
        nonlocal previous
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            snapshot = await asyncio.to_thread(_cached_snapshot)
            current = str(snapshot["cursor"])
            if current != previous:
                payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {current}\nevent: snapshot\ndata: {payload}\n\n"
                previous = current
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(_SNAPSHOT_CACHE_TTL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sqlstate(error: SQLAlchemyError) -> str | None:
    cause = getattr(error, "orig", error)
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
