from __future__ import annotations

import json
from datetime import datetime, timezone
from fastapi import APIRouter, Cookie, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from live_betting.strict_eligibility import (
    StrictMappingError,
    accept_strict_live_map_mapping,
    approve_automatic_exact_evidence,
    get_strict_live_map_mapping,
    invalidate_strict_live_map_mapping,
)

from .. import queries
from .control import _COOKIE_NAME, _require_loopback, control_sessions


router = APIRouter(prefix="/api/monitor/mappings", tags=["monitor-mappings"])


class ActorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(default="local-operator", min_length=1, max_length=100)


class InvalidateRequest(ActorRequest):
    reason: str = Field(min_length=5, max_length=500)


@router.get("/{raybet_match_id}")
def mapping_evidence(raybet_match_id: str, request: Request) -> dict[str, object]:
    _require_loopback(request)
    connection = queries.get_db()
    try:
        rows = connection.execute(
            """SELECT mapping.mapping_id, mapping.map_number, mapping.event_id,
                      mapping.team_one_id, mapping.team_two_id,
                      mapping.canonical_team_one_id, mapping.canonical_team_one_name,
                      mapping.canonical_team_two_id, mapping.canonical_team_two_name,
                      mapping.acceptance_mode, mapping.automatic_approval_id,
                      mapping.accepted_by, mapping.accepted_at, mapping.recorded_at,
                      mapping.evidence_json, mapping.evidence_hash,
                      invalidation.invalidation_id, invalidation.reason AS invalidation_reason,
                      invalidation.invalidated_by, invalidation.invalidated_at,
                      approval.approval_id AS evidence_approval_id
                 FROM strict_live_map_mappings AS mapping
                 LEFT JOIN strict_live_map_mapping_invalidations AS invalidation
                   ON invalidation.mapping_id=mapping.mapping_id
                 LEFT JOIN strict_live_automatic_evidence_approvals AS approval
                   ON approval.source_mapping_id=mapping.mapping_id
                WHERE mapping.raybet_match_id=?
                ORDER BY mapping.map_number, mapping.mapping_id""",
            (raybet_match_id,),
        ).fetchall()
    except Exception as error:
        if "no such table" in str(error):
            rows = []
        else:
            raise
    finally:
        connection.close()
    mappings = []
    for row in rows:
        try:
            evidence = json.loads(str(row[14]))
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        mappings.append(
            {
                "mapping_id": int(row[0]),
                "map_number": int(row[1]),
                "event_id": str(row[2]),
                "raybet_team_ids": [int(row[3]), int(row[4])],
                "canonical_teams": [
                    {"id": int(row[5]), "name": str(row[6])},
                    {"id": int(row[7]), "name": str(row[8])},
                ],
                "acceptance_mode": str(row[9]),
                "automatic_approval_id": row[10],
                "accepted_by": str(row[11]),
                "accepted_at": str(row[12]),
                "recorded_at": str(row[13]),
                "evidence": evidence,
                "evidence_hash": str(row[15]),
                "invalidation": (
                    {
                        "invalidation_id": int(row[16]),
                        "reason": str(row[17]),
                        "invalidated_by": str(row[18]),
                        "invalidated_at": str(row[19]),
                    }
                    if row[16] is not None
                    else None
                ),
                "evidence_approval_id": row[20],
            }
        )
    return {"raybet_match_id": raybet_match_id, "mappings": mappings}


@router.post("/{mapping_id}/approve-automatic")
def approve_automatic(
    mapping_id: int,
    payload: ActorRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    connection = queries.get_db()
    try:
        approval_id = approve_automatic_exact_evidence(
            connection,
            source_mapping_id=mapping_id,
            approved_by=payload.actor,
            approved_at=datetime.now(timezone.utc),
        )
    except StrictMappingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        connection.close()
    return {"mapping_id": mapping_id, "approval_id": approval_id}


@router.post("/{mapping_id}/invalidate")
def invalidate_mapping(
    mapping_id: int,
    payload: InvalidateRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    connection = queries.get_db()
    try:
        invalidation_id = invalidate_strict_live_map_mapping(
            connection,
            mapping_id=mapping_id,
            reason=payload.reason,
            invalidated_by=payload.actor,
            invalidated_at=datetime.now(timezone.utc),
        )
    except StrictMappingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        connection.close()
    return {"mapping_id": mapping_id, "invalidation_id": invalidation_id}


@router.post("/{source_mapping_id}/automatic/{map_number}")
def create_automatic_mapping(
    source_mapping_id: int,
    map_number: int,
    payload: ActorRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    connection = queries.get_db()
    try:
        source = get_strict_live_map_mapping(connection, source_mapping_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source mapping not found")
        mapping = accept_strict_live_map_mapping(
            connection,
            raybet_match_id=source.raybet_match_id,
            map_number=map_number,
            event_id=source.event_id,
            team_one_id=source.team_one_id,
            team_two_id=source.team_two_id,
            canonical_team_one_id=source.canonical_team_one_id,
            canonical_team_two_id=source.canonical_team_two_id,
            source=source.source,
            evidence=json.loads(source.evidence_json),
            accepted_by=payload.actor,
            accepted_at=datetime.now(timezone.utc),
            acceptance_mode="automatic_exact",
        )
    except StrictMappingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        connection.close()
    return {"mapping_id": mapping.mapping_id, "map_number": mapping.map_number}


def _require_control(
    request: Request,
    session_id: str | None,
    csrf_token: str | None,
) -> None:
    _require_loopback(request)
    if not control_sessions.valid(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Valid control session and CSRF token required")
