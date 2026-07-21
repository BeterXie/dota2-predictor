from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response, StreamingResponse

from live_betting.vision_frame_registry import (
    read_registered_vision_frame_bytes,
    vision_frame_ref,
)
from shared.sqlite import classify_sqlite_error

from .. import intelligence, monitoring, queries


router = APIRouter(prefix="/api/monitor", tags=["monitor"])


def _build_snapshot() -> dict[str, object]:
    """Build one monitor snapshot off the async event loop."""
    connection = queries.get_db()
    try:
        return monitoring.build_monitor_snapshot(connection)
    finally:
        connection.close()


@router.get("/bootstrap")
def bootstrap() -> dict[str, object]:
    connection = queries.get_db()
    try:
        return monitoring.build_monitor_snapshot(connection)
    finally:
        connection.close()


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
        except sqlite3.Error as error:
            kind = classify_sqlite_error(error)
            if kind == "busy":
                raise HTTPException(
                    status_code=503,
                    detail="Monitor database is busy",
                    headers={"Retry-After": "1"},
                ) from None
            if kind == "schema_missing":
                raise HTTPException(
                    status_code=503,
                    detail="Monitor database schema is unavailable",
                ) from None
            raise
    finally:
        connection.close()
    if detail is None:
        raise HTTPException(status_code=404, detail="RayBet match not found")
    return detail


@router.get("/matches/{raybet_match_id}/maps/{map_number}/postmatch")
def postmatch_detail(
    raybet_match_id: str,
    map_number: int = Path(ge=1),
    max_points: int = Query(1200, ge=100, le=5000),
) -> dict[str, object]:
    detail = intelligence.get_raybet_postmatch(
        raybet_match_id,
        map_number,
        max_points=max_points,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="RayBet match not found")
    return detail


@router.get("/matches/{raybet_match_id}/vision-frames/{frame_digest}.jpg")
def vision_frame(raybet_match_id: str, frame_digest: str) -> Response:
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
        except sqlite3.Error:
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
        except (RuntimeError, TypeError, ValueError, sqlite3.Error):
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
            snapshot = await asyncio.to_thread(_build_snapshot)
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
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
