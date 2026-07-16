from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import queries
from ..alerts import acknowledge_alert
from ..control import ACTIONS, COMPONENTS, ControlService


router = APIRouter(prefix="/api/monitor/control", tags=["monitor-control"])
control_service = ControlService(project_dir=Path(__file__).resolve().parents[2])

_COOKIE_NAME = "monitor_control_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ControlActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class AlertAcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="local-operator", min_length=1, max_length=100)


class ControlSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self) -> tuple[str, str, int]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = time.time() + _SESSION_TTL_SECONDS
        with self._lock:
            self._purge_locked(time.time())
            self._sessions[session_id] = (csrf_token, expires_at)
        return session_id, csrf_token, _SESSION_TTL_SECONDS

    def valid(self, session_id: str | None, csrf_token: str | None) -> bool:
        if not session_id or not csrf_token:
            return False
        with self._lock:
            self._purge_locked(time.time())
            record = self._sessions.get(session_id)
        return record is not None and secrets.compare_digest(record[0], csrf_token)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _purge_locked(self, now: float) -> None:
        expired = [key for key, (_, expires_at) in self._sessions.items() if expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)


control_sessions = ControlSessionStore()


@router.get("/session")
def create_session(request: Request, response: Response) -> dict[str, object]:
    client_host = _require_loopback(request)
    session_id, csrf_token, max_age = control_sessions.issue()
    response.set_cookie(
        _COOKIE_NAME,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-cache, no-store"
    connection = queries.get_db()
    try:
        components = control_service.statuses(
            connection,
            database_path=Path(queries.DB_PATH),
        )
    finally:
        connection.close()
    return {
        "csrf_token": csrf_token,
        "expires_in": max_age,
        "client_host": client_host,
        "components": components,
    }


@router.get("/components")
def components(
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_loopback(request)
    if not control_sessions.valid(session_id, csrf_token):
        raise HTTPException(
            status_code=403,
            detail="Valid control session and CSRF token required",
        )
    connection = queries.get_db()
    try:
        return {
            "components": control_service.statuses(
                connection,
                database_path=Path(queries.DB_PATH),
            )
        }
    finally:
        connection.close()


@router.post("/{component}/{action}", response_model=None)
def control_component(
    component: str,
    action: str,
    payload: ControlActionRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
):
    client_host = _require_loopback(request)
    if component not in COMPONENTS:
        raise HTTPException(status_code=404, detail="Unknown managed component")
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown control action")
    if not control_sessions.valid(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Valid control session and CSRF token required")

    connection = queries.get_db()
    try:
        result = control_service.execute(
            connection,
            database_path=Path(queries.DB_PATH),
            component=component,
            action=action,
            request_id=payload.request_id,
            client_host=client_host,
        )
    finally:
        connection.close()
    if not bool(result["ok"]):
        return JSONResponse(result, status_code=409)
    return result


@router.post("/alerts/{incident_id}/acknowledge")
def acknowledge_incident(
    incident_id: int,
    payload: AlertAcknowledgeRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=_COOKIE_NAME),
    csrf_token: str | None = Header(default=None, alias="X-Monitor-CSRF"),
) -> dict[str, object]:
    _require_loopback(request)
    if not control_sessions.valid(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Valid control session and CSRF token required")
    connection = queries.get_db()
    try:
        changed = acknowledge_alert(
            connection,
            incident_id=incident_id,
            actor=payload.actor,
        )
    finally:
        connection.close()
    return {"incident_id": incident_id, "acknowledged": changed}


def _require_loopback(request: Request) -> str:
    client_host = request.client.host if request.client is not None else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Local loopback request required")

    request_host = _normalized_loopback_host(request.url.hostname)
    if request_host is None:
        raise HTTPException(status_code=403, detail="Loopback Host header required")

    origin = request.headers.get("origin")
    if origin:
        try:
            parsed_origin = urlsplit(origin)
            origin_host = _normalized_loopback_host(parsed_origin.hostname)
            origin_port = parsed_origin.port or _default_port(parsed_origin.scheme)
            request_port = request.url.port or _default_port(request.url.scheme)
        except ValueError as error:
            raise HTTPException(status_code=403, detail="Same-origin control request required") from error
        if (
            parsed_origin.scheme != request.url.scheme
            or origin_host != request_host
            or origin_port != request_port
        ):
            raise HTTPException(status_code=403, detail="Same-origin control request required")
    return client_host


def _normalized_loopback_host(host: str | None) -> str | None:
    if host is None:
        return None
    normalized = host.rstrip(".").lower()
    return normalized if normalized in _LOOPBACK_HOSTS else None


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.lower())
