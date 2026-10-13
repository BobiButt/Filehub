"""HTTP/WS API package for FileHub."""

from __future__ import annotations

from fastapi import Request

from app.core.hub import hub
from app.core.security import SESSION_COOKIE, security
from app.models.schemas import DeviceInfo, DeviceState


class ApiError(Exception):
    """Application error rendered as ``{"error": "..."}`` with a status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def current_device(request: Request) -> DeviceInfo | None:
    """Device attached to this request's session cookie, if any (RAM only)."""
    device_id = request.cookies.get(SESSION_COOKIE, "")
    if not device_id:
        return None
    return hub.get_device(device_id)


def require_member(request: Request) -> DeviceInfo:
    """Approved devices only — pending/blocked/anonymous get 401/403."""
    device = current_device(request)
    if device is None:
        raise ApiError(401, "not joined")
    if device.state == DeviceState.PENDING:
        raise ApiError(403, "waiting for host approval")
    if device.state == DeviceState.BLOCKED:
        raise ApiError(403, "this device is blocked")
    return device


def require_host(request: Request) -> str:
    """Host-only gate (§12). Returns the acting name for the traffic log."""
    if not security.is_host(request, hub.admin_token):
        raise ApiError(403, "host only")
    device = current_device(request)
    return device.name if device is not None else "Host"


def clean_name(raw: str) -> str:
    """Sanitize a user-chosen display name."""
    name = " ".join((raw or "").split())
    if not name:
        raise ApiError(401, "device name is required")
    return name[:40]
