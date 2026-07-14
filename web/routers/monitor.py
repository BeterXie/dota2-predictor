from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import monitoring, queries


router = APIRouter(prefix="/api/monitor", tags=["monitor"])


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


@router.get("/matches/{raybet_match_id}")
def match_detail(
    raybet_match_id: str,
    max_points: int = Query(1200, ge=100, le=5000),
) -> dict[str, object]:
    connection = queries.get_db()
    try:
        detail = monitoring.monitor_match_detail(
            connection,
            raybet_match_id,
            max_points=max_points,
        )
    finally:
        connection.close()
    if detail is None:
        raise HTTPException(status_code=404, detail="RayBet match not found")
    return detail


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
            connection = queries.get_db()
            try:
                snapshot = monitoring.build_monitor_snapshot(connection)
            finally:
                connection.close()
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
