from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Cookie, Header, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from vision.calibration import VisionCalibrationService

from .. import queries
from ..match_identity import observation_file_metadata
from .control import _COOKIE_NAME, _require_loopback
from .mappings import _require_control


router = APIRouter(prefix="/api/vision-calibration", tags=["vision-calibration"])
calibration_service = VisionCalibrationService(Path(__file__).resolve().parents[2])


class SaveCalibrationLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hero_ids: list[int] = Field(min_length=10, max_length=10)
    raybet_match_id: str | None = Field(default=None, min_length=1, max_length=64)
    map_number: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("hero_ids")
    @classmethod
    def unique_positive_heroes(cls, value: list[int]) -> list[int]:
        if any(hero_id <= 0 for hero_id in value) or len(set(value)) != 10:
            raise ValueError("hero_ids must contain ten unique positive IDs")
        return value


class BuildCalibrationCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_id: str = Field(min_length=20, max_length=20, pattern=r"^[a-f0-9]+$")


class RunCalibrationEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_id: str = Field(min_length=20, max_length=20, pattern=r"^[a-f0-9]+$")
    candidate_id: str = Field(min_length=1, max_length=200)
    observation_file: str = Field(min_length=6, max_length=255)
    layout_profile: str = Field(min_length=1, max_length=100)
    mode: Literal["perception", "runtime"] = "perception"
    captured_after: datetime | None = None
    captured_before: datetime | None = None


@router.get("/bootstrap")
def bootstrap(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, object]:
    _require_loopback(request)
    try:
        result = calibration_service.bootstrap(limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    observation_files = result.get("observation_files")
    if not isinstance(observation_files, list) or not observation_files:
        return result
    connection = queries.get_db()
    try:
        try:
            for item in observation_files:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                metadata = observation_file_metadata(connection, str(item["name"]))
                if metadata is not None:
                    item.update(metadata)
        except SQLAlchemyError:
            pass
    finally:
        connection.close()
    return result


@router.get("/events/{event_id}/assets/{asset_name}", include_in_schema=False)
def event_asset(
    request: Request,
    event_id: str = ApiPath(pattern=r"^[a-f0-9]{20}$"),
    asset_name: str = ApiPath(pattern=r"^(frame|hero_slot_[0-9]{2})\.jpg$"),
) -> FileResponse:
    _require_loopback(request)
    try:
        path = calibration_service.read_event_asset(event_id, asset_name)
    except (FileNotFoundError, KeyError, ValueError):
        raise HTTPException(status_code=404, detail="Vision asset not found") from None
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.post("/events/{event_id}/label")
def save_label(
    payload: SaveCalibrationLabelRequest,
    request: Request,
    event_id: str = ApiPath(pattern=r"^[a-f0-9]{20}$"),
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    try:
        return calibration_service.save_label(
            event_id,
            hero_ids=tuple(payload.hero_ids),
            raybet_match_id=payload.raybet_match_id,
            map_number=payload.map_number,
            note=payload.note,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Vision event not found") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/candidates")
def build_candidate(
    payload: BuildCalibrationCandidateRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    try:
        return calibration_service.build_candidate(payload.label_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Calibration label not found") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/evaluations")
def run_evaluation(
    payload: RunCalibrationEvaluationRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_control(request, session_id, csrf_token)
    try:
        return calibration_service.run_evaluation(
            label_id=payload.label_id,
            candidate_id=payload.candidate_id,
            observation_file=payload.observation_file,
            layout_profile=payload.layout_profile,
            mode=payload.mode,
            captured_after=payload.captured_after,
            captured_before=payload.captured_before,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Calibration input missing: {error}") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


__all__ = ["calibration_service", "router"]
